"""IndexManager（stage 3）：消费文档索引任务队列（Redis BRPOP）。

启动即消费 Redis 队列积压任务，并扫描 rag_document.status=0 的残留（上轮崩溃未消费）
重新入队，保证索引最终完成。无 Redis 时不启动消费者（调用方降级为进程内 create_task）。
"""
import asyncio
import json

from sqlalchemy import select

from app.app_state import app_state
from app.server import Manager
from common.logger import logger
from core.rag_indexer.pipeline import run_index_job
from store.postgres_store import get_sessionmaker
from store.orm_models.file import File
from store.orm_models.knowledge import Doc
from store.redis_keys import index_queue_key


class IndexManager(Manager):
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = False

    async def start(self) -> None:
        redis = app_state.redis
        if redis is None:
            logger.warning('Redis 不可用，IndexManager 不启动消费者（索引降级为进程内执行）')
            return
        await self._requeue_pending()
        self._stop = False
        self._task = asyncio.create_task(self._consume())
        logger.info('IndexManager 消费者已启动')

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _requeue_pending(self) -> None:
        """崩溃恢复：status=0 的文档重新入队。"""
        try:
            factory = get_sessionmaker()
            async with factory() as session:
                rows = (await session.execute(
                    select(Doc.id, Doc.tenant_id, Doc.file_upload_id, Doc.doc_type)
                    .where(Doc.status == 0)
                )).all()
            for doc_id, tenant_id, file_id, doc_type in rows:
                oss_key, _ = await self._oss_key_of(tenant_id, file_id)
                if oss_key:
                    await app_state.redis.lpush(index_queue_key(), json.dumps(
                        {'tenant_id': tenant_id, 'doc_id': doc_id, 'oss_key': oss_key,
                         'file_name': '', 'doc_type': doc_type}, ensure_ascii=False))
            if rows:
                logger.info(f'索引恢复：重新入队 {len(rows)} 个未完成文档')
        except Exception:  # noqa: BLE001
            logger.exception('索引残留任务扫描失败（忽略，后续可手动触发）')

    async def _oss_key_of(self, tenant_id: int, file_id: int) -> tuple[str | None, str | None]:
        try:
            factory = get_sessionmaker()
            async with factory() as session:
                row = (await session.execute(
                    select(File.current_oss_key, File.file_name).where(
                        File.id == file_id, File.tenant_id == tenant_id)
                )).first()
            return (row.current_oss_key, row.file_name) if row else (None, None)
        except Exception:  # noqa: BLE001
            return (None, None)

    async def _consume(self) -> None:
        redis = app_state.redis
        while not self._stop:
            try:
                res = await redis.brpop(index_queue_key(), timeout=2)
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception('索引队列消费异常')
                await asyncio.sleep(2)
                continue
            if not res:
                continue
            _, payload = res
            try:
                job = json.loads(payload)
                await run_index_job(
                    job['tenant_id'], job['doc_id'], job['oss_key'],
                    job.get('file_name', ''), job.get('doc_type', 0))
            except Exception:  # noqa: BLE001
                logger.exception('索引任务执行失败（已回写 status=3）')

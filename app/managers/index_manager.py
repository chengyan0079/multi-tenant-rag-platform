"""IndexManager（stage 3）：Redis BRPOP 消费索引任务；启动扫描 status∈{0,1} 残留重新入队保证最终完成；无 Redis 时不启动（降级进程内 create_task）。"""
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
from store.redis_keys import KeyTTL, index_queue_key, index_recover_key


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
        """崩溃恢复：把未完成（待解析/解析中）的文档重新入队。

        status=1 也要扫：任务已被 BRPOP 弹出，worker 硬崩溃时异常分支不执行，
        文档会永久停在"解析中"。多 worker 各自扫描时用 SETNX 认领，避免重复重投。
        """
        try:
            factory = get_sessionmaker()
            async with factory() as session:
                rows = (await session.execute(
                    select(Doc.id, Doc.tenant_id, Doc.file_upload_id, Doc.doc_type)
                    .where(Doc.status.in_((0, 1)))
                )).all()
            requeued = 0
            for doc_id, tenant_id, file_id, doc_type in rows:
                # 认领锁 TTL 到期后允许再次重投，避免持锁 worker 崩溃导致永久遗漏
                claimed = await app_state.redis.set(
                    index_recover_key(doc_id), '1', nx=True, ex=KeyTTL.INDEX_RECOVER)
                if not claimed:
                    continue
                oss_key, file_name = await self._oss_key_of(tenant_id, file_id)
                if oss_key:
                    await app_state.redis.lpush(index_queue_key(), json.dumps(
                        {'tenant_id': tenant_id, 'doc_id': doc_id, 'oss_key': oss_key,
                         'file_name': file_name or '', 'doc_type': doc_type}, ensure_ascii=False))
                    requeued += 1
            if requeued:
                logger.info(f'索引恢复：重新入队 {requeued} 个未完成文档')
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

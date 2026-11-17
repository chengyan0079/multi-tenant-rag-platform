"""GraphBuildManager（stage 3）：定时从 PG/Nacos 结构化数据重建故障图谱。

每轮遍历库中有数据的租户逐个建图；图路未启用或定时关闭时不启动。
Nacos 依赖取长生命周期实例，避免像离线 CLI 那样每次采集后 shutdown。
"""
from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.app_state import app_state
from app.server import Manager
from common.config import config
from common.logger import logger
from store.postgres_store import get_sessionmaker


class GraphBuildManager(Manager):
    def __init__(self, interval_sec: int | None = None) -> None:
        self._interval = interval_sec or config.GRAPH_SCHEDULE_INTERVAL_SEC
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._naming: Any = None
        self._config_client: Any = None

    async def start(self) -> None:
        if not config.GRAPH_ENABLED or not config.GRAPH_SCHEDULE_ENABLED:
            logger.info('定时建图未启动（GRAPH_ENABLED 或 GRAPH_SCHEDULE_ENABLED=False）')
            return
        if app_state.db_engine is None or app_state.neo4j_store is None:
            logger.warning('数据库或图库未就绪，定时建图不启动')
            return
        self._stop.clear()
        await self._init_deps()
        self._task = asyncio.create_task(self._run())
        logger.info(f'定时建图已启动，周期 {self._interval}s')

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self._close_deps()

    async def _init_deps(self) -> None:
        """建立长生命周期的 Nacos 依赖，供各轮采集复用。"""
        if config.NACOS_ENABLED:
            try:
                self._naming = await self._make_naming()
            except Exception as e:  # noqa: BLE001
                logger.warning(f'Nacos 实例列表依赖初始化失败，采集时跳过实例数据: {e}')
        if config.NACOS_CONFIG_ENABLED:
            try:
                from core.config_center.nacos_config import get_config_center_client
                self._config_client = get_config_center_client()
            except Exception as e:  # noqa: BLE001
                logger.warning(f'Nacos 配置中心依赖初始化失败，采集时跳过配置变更: {e}')

    async def _make_naming(self) -> Any:
        from common.nacos_compat import normalize_namespace_id
        from v2.nacos import ClientConfig, NacosNamingService

        cc = ClientConfig(
            server_addresses=config.NACOS_SERVER_ADDR,
            namespace_id=normalize_namespace_id(config.NACOS_NAMESPACE),
        )
        return await NacosNamingService.create_naming_service(cc)

    async def _list_instances(self) -> list[dict[str, Any]]:
        if self._naming is None:
            return []
        from v2.nacos import ListInstanceParam
        instances = await self._naming.list_instances(ListInstanceParam(
            service_name=config.SERVICE_NAME,
            group_name=config.NACOS_GROUP,
            healthy_only=False,
        ))
        return [i.model_dump() for i in instances or []]

    async def _close_deps(self) -> None:
        if self._naming is not None:
            try:
                await self._naming.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._naming = None
        self._config_client = None

    async def _tenant_ids(self) -> list[int]:
        """从文档表取有数据的租户，自动覆盖新增租户。"""
        from store.orm_models.knowledge import Doc
        factory = get_sessionmaker()
        async with factory() as session:
            rows = (await session.execute(select(Doc.tenant_id).distinct())).all()
        return [r[0] for r in rows if r[0] is not None]

    async def _run_once(self) -> None:
        from offline.graph_build import build_graph

        tenant_ids = await self._tenant_ids()
        if not tenant_ids:
            logger.info('定时建图：库中暂无租户数据，跳过')
            return
        factory = async_sessionmaker(app_state.db_engine, class_=AsyncSession)
        for tid in tenant_ids:
            async with factory() as session:
                try:
                    stats = await build_graph(
                        tid, app_state.neo4j_store, session, app_state.db_engine,
                        self._list_instances, self._config_client)
                    logger.info(f'定时建图完成 [tenant={tid}]: {stats}')
                except Exception:  # noqa: BLE001
                    logger.exception(f'定时建图失败 [tenant={tid}]')

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                logger.exception('定时建图轮询异常')
            # 用 Event 等待，stop() 置位可立即唤醒退出，不必等满一个周期
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

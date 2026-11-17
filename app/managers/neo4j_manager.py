"""Neo4jManager（stage 2）：GRAPH_ENABLED 时连接图库并注入 app_state，失败只告警不阻断启动。"""
from app.server import Manager
from app.app_state import app_state
from common.config import config
from common.logger import logger
from store.neo4j_store import Neo4jStore


class Neo4jManager(Manager):
    async def start(self) -> None:
        if not config.GRAPH_ENABLED:
            logger.info('图路未启用（GRAPH_ENABLED=False），跳过 Neo4j 连接')
            return
        store = Neo4jStore()
        try:
            await store.connect()
            await store.ensure_schema()
        except Exception as e:  # noqa: BLE001  图是侧车，不可用不影响主链路
            logger.warning(f'Neo4j 连接/初始化失败，图路降级为不可用: {e}')
            await store.close()
            return
        app_state.neo4j_store = store
        logger.info(f'Neo4j 就绪: {config.NEO4J_URI}')

    async def stop(self) -> None:
        if app_state.neo4j_store is not None:
            await app_state.neo4j_store.close()
            app_state.neo4j_store = None

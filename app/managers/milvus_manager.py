"""MilvusManager（order/stage：stage 2，依赖 Model）：启动时确保数据库与集合就绪。

已合并原 scripts/init_milvus.py 的职责：启动即"确保数据库存在（缺失则创建）→ 确保集合
存在（缺失则创建 + 索引）→ 加载到内存"，已存在则直接加载，全程幂等。无需再单独运行初始化脚本。
"""
from pymilvus import AsyncMilvusClient

from app.server import Manager
from app.app_state import app_state
from common.config import config
from common.logger import logger
from store.milvus_store import MilvusStore


async def _ensure_database(uri: str, db_name: str) -> None:
    """确保 Milvus 数据库存在（连默认库做管理操作；缺失则创建）。"""
    admin = AsyncMilvusClient(uri=uri)
    try:
        existing = await admin.list_databases()
        if db_name in existing:
            logger.info(f'Milvus 数据库已存在: {db_name}')
        else:
            await admin.create_database(db_name)
            logger.info(f'已创建 Milvus 数据库: {db_name}')
    finally:
        await admin.close()


class MilvusManager(Manager):
    async def start(self) -> None:
        uri = config.MILVUS_URI
        db_name = config.MILVUS_DB_NAME
        # 1) 确保数据库存在（缺失则创建）
        await _ensure_database(uri, db_name)
        # 2) 库级客户端 + 绑定模型（置于 Model 之后，创建集合需要 dense_dim）
        client = AsyncMilvusClient(uri=uri, db_name=db_name)
        store = MilvusStore(client=client)
        store.bind(client, app_state.model)
        # 3) 确保集合存在：不存在则创建 + 索引，已存在则直接加载（幂等）
        await store.ensure_collection()
        app_state.milvus = client
        app_state.milvus_store = store
        logger.info(f'Milvus 就绪（db={db_name}, collection={config.MILVUS_COLLECTION}）')

    async def stop(self) -> None:
        if app_state.milvus:
            await app_state.milvus.close()

"""
faq_cache_manager - 启动项：把 rag_faq 全量载入 Redis 缓存（设计文档 §11）

顺序需在 RedisManager(20) 与 MySQLManager(30) 之后，故注册 order=35。
载入失败不影响整体启动，FAQ 查询会回源 DB（store.faq_cache 冷启动兜底）。
"""
from app.server import Manager
from common.logger import logger
from store.faq_cache import load_all_faqs_to_redis


class FaqCacheManager(Manager):
    """启动时全量载入 FAQ 到 Redis。"""

    async def start(self) -> None:
        try:
            await load_all_faqs_to_redis()
        except Exception:
            logger.exception('FAQ 缓存载入失败（服务继续启动，FAQ 查询将回源 DB）')

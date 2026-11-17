"""FaqCacheManager（order=35，在 Redis/PG 之后）：启动时全量载入 FAQ 到 Redis，失败不影响启动（查询回源 DB）。"""
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

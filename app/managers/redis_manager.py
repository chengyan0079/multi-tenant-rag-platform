"""RedisManager（order=20）：建立 Redis 异步连接池，注入 app_state.redis。"""
import redis.asyncio as redis

from app.server import Manager
from app.app_state import app_state
from common.config import config
from common.logger import logger


class RedisManager(Manager):
    async def start(self) -> None:
        # 有密码时把密码放进 URL 的 userinfo 段
        url = f'redis://{config.REDIS_HOST}:{config.REDIS_PORT}'
        if config.REDIS_PASSWORD:
            url = f'redis://:{config.REDIS_PASSWORD}@{config.REDIS_HOST}:{config.REDIS_PORT}'
        self.pool = redis.from_url(url, db=config.REDIS_DB, decode_responses=True)
        # ping 验证连通性，连不上会抛异常，由 AppServer 触发停止回滚
        await self.pool.ping()
        app_state.redis = self.pool
        logger.info('Redis 连接就绪')

    async def stop(self) -> None:
        if app_state.redis:
            await app_state.redis.aclose()

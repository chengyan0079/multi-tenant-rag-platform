"""PostgresManager（order=30）：校验 PostgreSQL 连通且目标库可用，不自动建表（生产建表由 Alembic 迁移负责）。

库可用性校验采用"直接连目标库探测"：连得上即可用；不通再按 sqlstate 区分
（3D000=库不存在 / 其它=连不上或无权限）。不查 information_schema——非 superuser 可能因无权限
看不到库而误判"不存在"，且对"该用户能否使用此库"这一真实诉求是间接的。
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.server import Manager, StartupError
from app.app_state import app_state
from common.config import config
from common.logger import logger


def _is_unknown_database(exc: Exception) -> bool:
    """判断异常是否为"数据库不存在"（PostgreSQL sqlstate 3D000 / invalid_catalog_name）。

    兼容 SQLAlchemy 包裹（exc.orig 为 DBAPI 异常）与直接 DBAPI 异常两种形态。
    """
    orig = getattr(exc, 'orig', exc)
    sqlstate = getattr(orig, 'sqlstate', None)
    if sqlstate == '3D000':
        return True
    msg = str(exc).lower()
    return '3d000' in msg or ('database' in msg and 'does not exist' in msg)


class PostgresManager(Manager):
    async def start(self) -> None:
        db = config.POSTGRES_DATABASE
        # 直接连目标库探测（不查 information_schema）；探活即证明"该用户可用此库"
        app_dsn = (
            f'postgresql+asyncpg://{config.POSTGRES_USER}:{config.POSTGRES_PASSWORD}'
            f'@{config.POSTGRES_HOST}:{config.POSTGRES_PORT}/{db}'
        )
        app_state.db_engine = create_async_engine(
            app_dsn, pool_pre_ping=True,
            pool_size=config.POSTGRES_POOL_MAX_SIZE,
        )
        try:
            async with app_state.db_engine.connect() as conn:
                await conn.execute(text('SELECT 1'))
        except Exception as e:  # noqa: BLE001  分类：库不存在 vs 连不上/无权限
            await app_state.db_engine.dispose()
            app_state.db_engine = None
            if _is_unknown_database(e):
                raise StartupError(
                    f"PostgreSQL 数据库 {db!r} 不存在，请先创建（执行 scripts/schema_postgresql.sql 会自动建库）"
                ) from e
            raise StartupError(
                f'PostgreSQL 连接失败（{config.POSTGRES_HOST}:{config.POSTGRES_PORT}, db={db!r}）：{e}'
            ) from e
        logger.info(f'PostgreSQL 连接成功，数据库 {db!r} 就绪')

    async def stop(self) -> None:
        # dispose 关闭整个连接池，比逐个 close 更彻底
        if app_state.db_engine:
            await app_state.db_engine.dispose()

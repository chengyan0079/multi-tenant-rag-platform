"""MySQLManager（order=30）：校验 MySQL 连通且目标库可用，不自动建表（生产建表由 Alembic 迁移负责）。

库可用性校验采用"直接连目标库探测"：连得上即可用；不通再按错误码区分
（1049=库不存在 / 其它=连不上或无权限）。不查 information_schema.schemata——该视图按
用户权限过滤，非 root 用户可能因无权限看不到库而误判"不存在"，且对"该用户能否使用此库"
这一真实诉求是间接的。
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.server import Manager, StartupError
from app.app_state import app_state
from common.config import config
from common.logger import logger


def _is_unknown_database(exc: Exception) -> bool:
    """判断异常是否为"数据库不存在"（MySQL errno 1049 / Unknown database）。

    兼容 SQLAlchemy 包裹（exc.orig 为 DBAPI 异常）与直接 DBAPI 异常两种形态。
    """
    orig = getattr(exc, 'orig', exc)
    args = getattr(orig, 'args', None) or ()
    if args and args[0] == 1049:
        return True
    msg = str(exc).lower()
    return '1049' in msg or 'unknown database' in msg


class MySQLManager(Manager):
    async def start(self) -> None:
        db = config.MYSQL_DATABASE
        # 直接连目标库探测（不查 information_schema）；探活即证明"该用户可用此库"
        app_dsn = (
            f'mysql+aiomysql://{config.MYSQL_USER}:{config.MYSQL_PASSWORD}'
            f'@{config.MYSQL_HOST}:{config.MYSQL_PORT}/{db}?charset=utf8mb4'
        )
        app_state.engine = create_async_engine(
            app_dsn, pool_pre_ping=True,
            pool_size=config.MYSQL_POOL_MAX_SIZE,
        )
        try:
            async with app_state.engine.connect() as conn:
                await conn.execute(text('SELECT 1'))
        except Exception as e:  # noqa: BLE001  分类：库不存在 vs 连不上/无权限
            await app_state.engine.dispose()
            app_state.engine = None
            if _is_unknown_database(e):
                raise StartupError(
                    f"MySQL 数据库 {db!r} 不存在，请先创建（执行 scripts/schema.sql 会自动建库）"
                ) from e
            raise StartupError(
                f'MySQL 连接失败（{config.MYSQL_HOST}:{config.MYSQL_PORT}, db={db!r}）：{e}'
            ) from e
        logger.info(f'MySQL 连接成功，数据库 {db!r} 就绪')

    async def stop(self) -> None:
        # dispose 关闭整个连接池，比逐个 close 更彻底
        if app_state.engine:
            await app_state.engine.dispose()

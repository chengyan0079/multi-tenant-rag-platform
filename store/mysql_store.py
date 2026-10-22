"""
mysql_store - MySQL 异步连接与会话（M3 元数据）

引擎与连接池由 AppServer 的 MySQLManager 创建并注入 app_state.db_engine；
本模块提供会话工厂与查询辅助。生产环境建议改用 Alembic 迁移。
"""
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.app_state import app_state
from common.logger import logger


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """基于 app_state.db_engine 创建会话工厂。"""
    if app_state.db_engine is None:
        raise RuntimeError('MySQL 引擎未初始化，请确认 MySQLManager 已启动')
    return async_sessionmaker(app_state.db_engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：提供一个请求级异步会话。"""
    factory = get_sessionmaker()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception('MySQL 会话回滚')
            raise


async def ping() -> bool:
    """健康检查：执行 SELECT 1。"""
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text('SELECT 1'))
        return True
    except Exception as e:
        logger.warning(f'MySQL ping 失败: {e}')
        return False

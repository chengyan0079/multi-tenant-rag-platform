"""API 调用日志 DAO：把一次调用的"请求 + 响应"落一行（rag_api_call_log）。

落库是旁路：写失败仅告警，绝不影响接口返回。
"""
from typing import Any

from app.app_state import app_state
from common.logger import logger
from store.orm_models.api_log import ApiCallLog


class ApiLogDao:
    """API 调用日志落库（一条请求一行）。"""

    async def save(self, record: dict[str, Any]) -> None:
        """写入一行；引擎未就绪或写库异常时仅告警。"""
        if app_state.db_engine is None:
            return                      # MySQL 未启动（如单测/降级），跳过落库
        try:
            from store.mysql_store import get_sessionmaker
            async with get_sessionmaker()() as session:
                session.add(ApiCallLog(**record))
                await session.commit()
        except Exception:  # noqa: BLE001  日志旁路：失败不影响主链路
            logger.exception('API 调用日志落库失败（不影响接口返回）')

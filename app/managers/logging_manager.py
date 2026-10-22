"""LoggingManager（order=10）：初始化结构化日志，trace_id 由中间件注入上下文。"""
from app.server import Manager
from common.logger import setup_logging


class LoggingManager(Manager):
    async def start(self) -> None:
        setup_logging()
        # trace_id 由 app.middleware.TraceMiddleware 写入 contextvars，日志里透传

    async def stop(self) -> None:
        pass

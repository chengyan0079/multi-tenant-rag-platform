"""服务入口：组合 AppServer 后由 uvicorn 以 workers 多进程拉起 FastAPI（Windows spawn 安全），优雅关闭由 uvicorn 处理；启动后自检 /admin/health。运行：python run.py（生产多 worker）/ python app/main.py（开发单 worker 热重载）。"""
import asyncio
import urllib.request
from pathlib import Path

import uvicorn

from common.config import config
from common.logger import logger

_ROOT = Path(__file__).resolve().parent.parent


def _health_url() -> str:
    """健康检查地址；0.0.0.0 等通配绑定回退为回环地址用于展示/探测。"""
    host = '127.0.0.1' if config.HOST in ('', '0.0.0.0', '*', '::') else config.HOST
    return f'http://{host}:{config.PORT}/admin/health'


def _http_get(url: str) -> str:
    """阻塞式 GET（放入线程池执行，避免挡住事件循环）。"""
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.read().decode('utf-8', 'replace')


async def _probe_health(server: uvicorn.Server) -> None:
    """启动后轮询健康接口：就绪打印结果，收到退出信号或超时则结束（不阻断 serve）。"""
    url = _health_url()
    logger.info(f'[{config.SERVICE_NAME}] 启动中，就绪后健康检查: {url}')
    for attempt in range(1, 61):
        if server.should_exit:
            return
        try:
            body = await asyncio.to_thread(_http_get, url)
            logger.info(f'[{config.SERVICE_NAME}] 健康检查通过（第 {attempt} 次）: {body}')
            return
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1.0)
    logger.warning(f'[{config.SERVICE_NAME}] 健康检查未就绪: {url}')


async def _serve(*, workers: int, reload: bool) -> None:
    cfg = uvicorn.Config(
        "app.app_factory:create_app",
        factory=True,
        host=config.HOST,
        port=config.PORT,
        workers=workers,
        reload=reload,
        log_level=config.LOG_LEVEL.lower(),
        timeout_graceful_shutdown=30,
    )
    server = uvicorn.Server(cfg)
    probe = asyncio.create_task(_probe_health(server))
    try:
        # serve() 内部 capture_signals 接管 SIGINT/SIGTERM，优雅关闭语义不变
        await server.serve()
    finally:
        probe.cancel()


def main() -> None:
    asyncio.run(_serve(workers=config.WORKERS, reload=False))


def main_dev() -> None:
    """开发环境：单 worker + 热重载；就绪后自检健康接口。"""
    asyncio.run(_serve(workers=1, reload=True))

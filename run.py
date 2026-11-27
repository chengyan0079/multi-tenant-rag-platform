"""项目启动入口（uvicorn 驱动，多进程 + 异步 + 优雅关闭）。

启动：main() 经 uvicorn 以 factory 模式拉起 FastAPI，worker 数取 config.WORKERS；
每个 worker 在 lifespan 内按序初始化各 Manager，失败则记日志并退出。
开发调试可用 --reload 切到单 worker 热重载。

优雅关闭：收到 SIGINT/SIGTERM 后等待在途请求结束（最多 30 秒），再逆序释放各 Manager。
"""
import sys

from app.main import main, main_dev
from common.logger import logger


if __name__ == "__main__":
    logger.info('RAG-Ultra 启动中 ...')
    try:
        main_dev() if '--reload' in sys.argv else main()
    except KeyboardInterrupt:
        logger.info('收到中断信号')
    finally:
        logger.info('RAG-Ultra 已停止')

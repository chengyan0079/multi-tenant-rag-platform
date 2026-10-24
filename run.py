"""项目启动入口（uvicorn 驱动，多进程 + 异步 + 优雅关闭）。

启动：main.main() 经 uvicorn.run(factory=True, workers=config.WORKERS) 拉起 FastAPI，
每个 worker 在 lifespan 内按序初始化各 Manager（Config/Logging/Redis/PostgreSQL/Milvus/
Model/FileProvider/Nacos），任一启动项失败即记 "启动失败" 日志并退出该进程。

优雅关闭：收到 SIGINT/SIGTERM 后，uvicorn 停止接收新连接、等待在途请求结束
（最多 timeout_graceful_shutdown=30 秒），再触发 lifespan 逆序释放各 Manager 资源
（PostgreSQL/Redis/Milvus 连接关闭、Nacos 心跳取消与实例注销），最后进程退出。
"""
from app.main import main, main_dev
from common.logger import logger


if __name__ == "__main__":
    logger.info('RAG-Ultra 启动中 ...')
    try:
        # main()
        main_dev()
    except KeyboardInterrupt:
        # uvicorn 通常已自行处理中断信号；此处仅作兜底日志
        logger.info('收到中断信号')
    finally:
        logger.info('RAG-Ultra 已停止')

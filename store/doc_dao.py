"""文档（索引）DAO（store 数据访问层）：占位——建 doc 记录 + 索引任务投递尚未实现。

后续在此落地：写 rag_document / rag_chunk 行、投递 index_task、更新索引状态。
返回简单状态值，响应模型由路由层组装。
"""


class DocDao:
    async def create(self, tenant_id: int, oss_key: str) -> str:
        # TODO: 写 doc 表 + 投递 index_task 到 Redis 队列（Celery/ARQ）
        return "indexing"

    async def get_status(self, doc_id: int) -> str:
        # TODO: 查索引任务状态
        return "pending"

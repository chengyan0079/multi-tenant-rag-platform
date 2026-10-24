"""FAQ DAO（store 数据访问层）：rag_faq 的 PostgreSQL 增删改查 + Redis 缓存刷新。

返回 ORM 实体 / 简单值；实体 → 响应模型（app.api.schemas）的映射在路由层完成，
故本模块不依赖 app，保持 store 层纯净。
"""
from sqlalchemy import select

from store.faq_cache import delete_faq_cache, upsert_faq_cache
from store.postgres_store import get_sessionmaker
from store.orm_models.faq import Faq


class FaqDao:
    def __init__(self, factory=None) -> None:
        self._factory = factory or get_sessionmaker()

    async def create(self, tenant_id: int, question: str, answer: str,
                     question_type: int, similar_questions: str | None) -> int:
        """新建 FAQ，返回 faq_id；写穿透刷新缓存。"""
        async with self._factory() as session:
            row = Faq(
                tenant_id=tenant_id, question=question, answer=answer,
                question_type=question_type, similar_questions=similar_questions or '', enabled=1,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            faq_id = row.id
        await upsert_faq_cache(tenant_id)
        return faq_id

    async def update(self, tenant_id: int, faq_id: int, question: str, answer: str,
                     question_type: int, similar_questions: str | None) -> bool:
        """更新 FAQ（按租户隔离）；不存在返回 False。"""
        async with self._factory() as session:
            row = await session.get(Faq, faq_id)
            if not row or row.tenant_id != tenant_id:
                return False
            row.question = question
            row.answer = answer
            row.question_type = question_type
            row.similar_questions = similar_questions or ''
            await session.commit()
        await upsert_faq_cache(tenant_id)
        return True

    async def soft_delete(self, tenant_id: int, faq_id: int) -> bool:
        """软删除 FAQ（enabled=0，按租户隔离）；不存在返回 False。"""
        async with self._factory() as session:
            row = await session.get(Faq, faq_id)
            if not row or row.tenant_id != tenant_id:
                return False
            row.enabled = 0
            await session.commit()
        await delete_faq_cache(faq_id, tenant_id)
        return True

    async def get(self, tenant_id: int, faq_id: int) -> Faq | None:
        """按 id 查单条实体（按租户隔离）；不存在返回 None。"""
        async with self._factory() as session:
            row = await session.get(Faq, faq_id)
            if not row or row.tenant_id != tenant_id:
                return None
            return row

    async def list(self, tenant_id: int) -> list[Faq]:
        """列出本租户启用中的 FAQ 实体。"""
        async with self._factory() as session:
            return (await session.execute(
                select(Faq).where(Faq.tenant_id == tenant_id, Faq.enabled == 1)
            )).scalars().all()

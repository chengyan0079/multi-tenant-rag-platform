"""config_store - 配置审批 DAO（ConfigChangeRequest / ConfigReview 的 MySQL 增删改查）。

从 service 抽出的纯持久化操作；service 只做业务编排（校验 / Nacos 写 / 状态机）。
所有方法接收调用方传入的 session，事务边界由 service 控制（提交 / 回滚由 service 决定时机）。
对应 store 数据访问层在本业务域的落地（与 session_store / memory_store 同属"业务包内 DAO"）。
"""
from typing import Any

from sqlalchemy import func, select

from store.orm_models.config import ConfigChangeRequest, ConfigReview


class ConfigStore:
    # ---- 写 ----
    async def add_request(self, session, **fields: Any) -> ConfigChangeRequest:
        """新增一条变更申请（字段即列名），flush 取 id 后提交，返回实体。"""
        req = ConfigChangeRequest(**fields)
        session.add(req)
        await session.flush()
        await session.commit()
        return req

    async def add_review(self, session, *, request_id: int, reviewer: str,
                         action: int, comment: str | None) -> None:
        """登记一条复核记录（不提交，交由 service 统一 commit）。"""
        session.add(ConfigReview(
            request_id=request_id, reviewer=reviewer, action=action, comment=comment))

    async def commit(self, session) -> None:
        await session.commit()

    async def rollback(self, session) -> None:
        await session.rollback()

    # ---- 读 ----
    async def get_request(self, session, request_id: int,
                          *, for_update: bool = False) -> ConfigChangeRequest | None:
        """按 id 查申请；for_update=True 时加行级悲观锁（复核 / 执行用）。"""
        stmt = select(ConfigChangeRequest).where(ConfigChangeRequest.id == request_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await session.execute(stmt)).scalar_one_or_none()

    async def list_requests(
        self, session, *,
        status: int | None = None, domain: str | None = None, applicant: str | None = None,
        page: int = 1, page_size: int = 20,
    ) -> tuple[list[ConfigChangeRequest], int]:
        """申请列表（可按状态 / 域 / 申请人筛选 + 分页），返回 (rows, total)。"""
        conditions = []
        if status is not None:
            conditions.append(ConfigChangeRequest.status == status)
        if domain:
            conditions.append(ConfigChangeRequest.domain == domain)
        if applicant:
            conditions.append(ConfigChangeRequest.applicant == applicant)
        total = (await session.execute(
            select(func.count()).select_from(ConfigChangeRequest).where(*conditions)
        )).scalar_one()
        rows = (await session.execute(
            select(ConfigChangeRequest).where(*conditions)
            .order_by(ConfigChangeRequest.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        )).scalars().all()
        return list(rows), total

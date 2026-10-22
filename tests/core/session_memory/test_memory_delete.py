"""记忆删除单测：MemoryService.delete 与 SessionService.clear_context（假 store）。"""
import asyncio


class _FakeStore:
    def __init__(self) -> None:
        self.deleted = None

    async def delete(self, uid, sid):
        self.deleted = (uid, sid)


def test_memory_service_delete() -> None:
    from core.session_memory.memory_service import MemoryService
    st = _FakeStore()
    svc = MemoryService(memory_store=st)
    asyncio.run(svc.delete("u1", "s1"))
    assert st.deleted == ("u1", "s1")


def test_session_service_clear_context() -> None:
    from core.session.session_service import SessionService
    fm = _FakeStore()
    # 只用到 memory；session_store 传占位对象，避免构造真实依赖
    svc = SessionService(session_store=object(), memory_service=fm)
    asyncio.run(svc.clear_context("u1", "s1"))
    assert fm.deleted == ("u1", "s1")

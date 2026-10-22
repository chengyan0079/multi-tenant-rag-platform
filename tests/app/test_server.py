"""AppServer 编排单测：分 stage 并发启动、失败回滚、启动失败日志。"""
import asyncio

import pytest

from app.server import AppServer, Manager, StartupError


class _Mgr(Manager):
    """可观测的假 Manager：记录 start/stop 是否被调用，可按要求在 start 抛错。"""

    def __init__(self, name: str, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        if self.fail:
            raise RuntimeError(f"{self.name} boom")
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


def test_stage0_all_start_and_stop_reverses() -> None:
    a, b, c = _Mgr("A"), _Mgr("B"), _Mgr("C")
    srv = AppServer("t")
    srv.register(c, order=30, stage=0)
    srv.register(a, order=10, stage=0)
    srv.register(b, order=20, stage=0)
    asyncio.run(srv.start_all())
    assert all(m.started for m in (a, b, c))
    asyncio.run(srv.stop_all())
    assert all(m.stopped for m in (a, b, c))


def test_failure_in_stage_blocks_later_stages() -> None:
    # a 在 stage0 成功；b 在 stage1 失败；c 在 stage2 不应启动
    a, b, c = _Mgr("A"), _Mgr("B", fail=True), _Mgr("C")
    srv = AppServer("t")
    srv.register(a, stage=0)
    srv.register(b, stage=1)
    srv.register(c, stage=2)
    with pytest.raises(StartupError):
        asyncio.run(srv.start_all())
    assert a.started and a.stopped          # stage0 成功但被回滚
    assert not b.started                    # 失败项自身未启动完成
    assert not c.started and not c.stopped  # 后续 stage 不再执行


def test_failure_within_stage_rolls_back_concurrent_started() -> None:
    # 同 stage 并发：b 失败，a/c 并发已启动 → 一并回滚
    a, b, c = _Mgr("A"), _Mgr("B", fail=True), _Mgr("C")
    srv = AppServer("t")
    srv.register(a, stage=0)
    srv.register(b, stage=0)
    srv.register(c, stage=0)
    with pytest.raises(StartupError):
        asyncio.run(srv.start_all())
    assert not b.started
    assert a.started and a.stopped          # 并发启动成功但被回滚
    assert c.started and c.stopped


def test_failure_logs_startup_failed() -> None:
    import loguru
    b = _Mgr("B", fail=True)
    srv = AppServer("t")
    srv.register(b, stage=0)
    captured: list[str] = []
    sink_id = loguru.logger.add(lambda msg: captured.append(str(msg)), level="ERROR")
    try:
        with pytest.raises(StartupError):
            asyncio.run(srv.start_all())
    finally:
        loguru.logger.remove(sink_id)
    assert any("启动失败" in m for m in captured)

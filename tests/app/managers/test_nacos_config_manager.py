"""NacosConfigManager 单测：启动时拉起配置中心长连接并注入 app_state（供 /admin/health 读取）。

不连真实 Nacos：loader 与预热函数均用假实现替换。
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.app_state import app_state
from app.managers import nacos_config_manager as m
from common.config import config


class _FakeLoader:
    def __init__(self, fail_start: bool = False) -> None:
        self.started = False
        self.stopped = False
        self._fail = fail_start

    @property
    def is_started(self) -> bool:
        return self.started

    async def start(self) -> None:
        if self._fail:
            raise RuntimeError('nacos unreachable')
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self.started = False


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    app_state.config_center = None


def _patch(monkeypatch, loader: _FakeLoader) -> list:
    """替换 loader 工厂与预热函数，返回记录预热调用次的列表。"""
    calls: list = []
    monkeypatch.setattr(m, 'get_config_center_loader', lambda: loader)
    monkeypatch.setattr(m, 'warmup_prompts', lambda *a, **kw: calls.append(1))
    return calls


def test_start_starts_loader_and_warms_up(monkeypatch) -> None:
    loader = _FakeLoader()
    calls = _patch(monkeypatch, loader)
    monkeypatch.setattr(config, 'NACOS_CONFIG_ENABLED', True)

    asyncio.run(m.NacosConfigManager().start())
    assert loader.started is True
    assert app_state.config_center is loader      # 健康检查据此读 is_started
    assert len(calls) == 1                        # 提示词预热执行了一次


def test_start_disabled_skips_warmup(monkeypatch) -> None:
    loader = _FakeLoader()
    calls = _patch(monkeypatch, loader)
    monkeypatch.setattr(config, 'NACOS_CONFIG_ENABLED', False)

    asyncio.run(m.NacosConfigManager().start())
    assert calls == []                            # 未启用：不预热
    assert app_state.config_center is loader      # 仍注入，探活读到 is_started=False


def test_start_failure_does_not_block(monkeypatch) -> None:
    """长连接起不来（Nacos 不可达）不阻断启动，预热仍照常（直连 Open API 兜底）。"""
    loader = _FakeLoader(fail_start=True)
    calls = _patch(monkeypatch, loader)
    monkeypatch.setattr(config, 'NACOS_CONFIG_ENABLED', True)

    asyncio.run(m.NacosConfigManager().start())    # 不应抛出
    assert loader.started is False
    assert len(calls) == 1


def test_stop_closes_loader_and_clears_state(monkeypatch) -> None:
    loader = _FakeLoader()
    _patch(monkeypatch, loader)
    monkeypatch.setattr(config, 'NACOS_CONFIG_ENABLED', True)

    mgr = m.NacosConfigManager()
    asyncio.run(mgr.start())
    asyncio.run(mgr.stop())
    assert loader.stopped is True
    assert app_state.config_center is None

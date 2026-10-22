"""NacosDiscoverManager 单测：注册成功/失败（旁路）时对外暴露的注册状态，供 /admin/health 读取。

不连真实 Nacos：用假的 naming 服务替换 v2.nacos 的工厂与参数模型。
"""
import asyncio
import sys
from types import SimpleNamespace

import pytest

from app.app_state import app_state
from app.managers import nacos_discover_manager as m
from common.config import config


class _FakeNaming:
    """假 naming 服务：可设定 register_instance 成功 / 失败 / 返回 False。"""

    def __init__(self, result: bool = True, raise_exc: Exception | None = None) -> None:
        self._result = result
        self._raise = raise_exc
        self.registered = None
        self.deregistered = None
        self.closed = False

    async def register_instance(self, param) -> bool:
        if self._raise:
            raise self._raise
        self.registered = param
        return self._result

    async def deregister_instance(self, param) -> bool:
        self.deregistered = param
        return True

    async def shutdown(self) -> None:
        self.closed = True


def _install_fake_sdk(monkeypatch, fake: _FakeNaming) -> None:
    """把 v2.nacos 换成假模块（工厂返回 fake，参数模型原样保存构造参数）。"""

    async def _create(cc) -> _FakeNaming:
        return fake

    mod = SimpleNamespace(
        ClientConfig=lambda **kw: SimpleNamespace(**kw),
        NacosNamingService=SimpleNamespace(create_naming_service=_create),
        RegisterInstanceParam=lambda **kw: SimpleNamespace(**kw),
        DeregisterInstanceParam=lambda **kw: SimpleNamespace(**kw),
    )
    monkeypatch.setitem(sys.modules, 'v2.nacos', mod)
    monkeypatch.setitem(sys.modules, 'v2', SimpleNamespace(nacos=mod))


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    app_state.nacos_discover = None


def test_disabled_skips_registration(monkeypatch) -> None:
    monkeypatch.setattr(config, 'NACOS_ENABLED', False)
    asyncio.run(m.NacosDiscoverManager().start())
    assert app_state.nacos_discover is None        # 未启用：健康检查读到 False


def test_register_success_sets_state(monkeypatch) -> None:
    fake = _FakeNaming(result=True)
    _install_fake_sdk(monkeypatch, fake)
    monkeypatch.setattr(config, 'NACOS_ENABLED', True)

    mgr = m.NacosDiscoverManager()
    asyncio.run(mgr.start())
    assert mgr.is_registered is True
    assert app_state.nacos_discover is mgr
    assert fake.registered.service_name == config.SERVICE_NAME

    asyncio.run(mgr.stop())
    assert mgr.is_registered is False
    assert app_state.nacos_discover is None
    assert fake.deregistered is not None and fake.closed is True


def test_register_failure_is_bypassed(monkeypatch) -> None:
    """注册失败（如服务端返回 501）默认不阻断启动，状态保持未注册。"""
    fake = _FakeNaming(raise_exc=RuntimeError('Request Error, code is 501'))
    _install_fake_sdk(monkeypatch, fake)
    monkeypatch.setattr(config, 'NACOS_ENABLED', True)
    monkeypatch.setattr(config, 'NACOS_DISCOVER_STRICT', False)

    mgr = m.NacosDiscoverManager()
    asyncio.run(mgr.start())                        # 不应抛出
    assert mgr.is_registered is False
    assert fake.closed is True                      # 失败后释放连接


def test_register_failure_strict_raises(monkeypatch) -> None:
    fake = _FakeNaming(raise_exc=RuntimeError('boom'))
    _install_fake_sdk(monkeypatch, fake)
    monkeypatch.setattr(config, 'NACOS_ENABLED', True)
    monkeypatch.setattr(config, 'NACOS_DISCOVER_STRICT', True)

    with pytest.raises(RuntimeError):
        asyncio.run(m.NacosDiscoverManager().start())


def test_missing_sdk_warns(monkeypatch) -> None:
    """没装 SDK（或版本不对）：告警跳过，不置注册状态。"""
    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name.startswith('v2'):
            raise ImportError('no module named v2.nacos')
        return real_import(name, *a, **kw)

    monkeypatch.setattr(config, 'NACOS_ENABLED', True)
    monkeypatch.setattr(builtins, '__import__', _fake_import)
    mgr = m.NacosDiscoverManager()
    asyncio.run(mgr.start())
    assert mgr.is_registered is False


def test_stop_without_start_is_safe() -> None:
    """未注册过直接 stop 不应抛错。"""
    asyncio.run(m.NacosDiscoverManager().stop())

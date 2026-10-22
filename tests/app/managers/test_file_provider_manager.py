"""FileProviderManager 单测：健康检查通过/失败(fail-fast) 逻辑（mock FileProvider，不依赖真实存储）。"""
import asyncio
from unittest import mock

import pytest

from app.app_state import app_state


def _import_mgr():
    try:
        from app.managers.file_provider_manager import FileProviderManager
        import app.managers.file_provider_manager as mod
        return FileProviderManager, mod
    except ImportError:
        pytest.skip("file_provider_manager 不可用，跳过")


class _FakeFP:
    def __init__(self, ok: bool) -> None:
        self._ok = ok

    async def healthcheck(self) -> bool:
        return self._ok


def test_file_provider_ok_sets_app_state() -> None:
    FileProviderManager, mod = _import_mgr()
    app_state.file_provider = None
    try:
        with mock.patch.object(mod, 'build_file_provider', side_effect=lambda: _FakeFP(True)):
            asyncio.run(FileProviderManager().start())
        assert app_state.file_provider is not None
    finally:
        app_state.file_provider = None


def test_file_provider_fail_raises() -> None:
    FileProviderManager, mod = _import_mgr()
    from app.server import StartupError
    app_state.file_provider = None
    try:
        with mock.patch.object(mod, 'build_file_provider', side_effect=lambda: _FakeFP(False)):
            with pytest.raises(StartupError):
                asyncio.run(FileProviderManager().start())
    finally:
        app_state.file_provider = None

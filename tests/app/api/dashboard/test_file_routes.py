"""文件路由辅助函数单测：_ext（版本分配逻辑已内联进 FileDao.save_upload）。

注意：tests/api/user/test_routes_session 会在 collection 阶段把
app.api.dashboard.routes_file 用 MagicMock 占位（规避重依赖导入）；
这里确保拿到真实模块再测。
"""
import importlib
import sys

_MOD = "app.api.dashboard.routes_file"


def _rf():
    mod = sys.modules.get(_MOD)
    if mod is None or type(mod).__name__ == "MagicMock":
        sys.modules.pop(_MOD, None)
        mod = importlib.import_module(_MOD)
    return mod


def test_ext() -> None:
    rf = _rf()
    assert rf._ext("a.PDF") == "PDF"
    assert rf._ext("noext") == "bin"
    assert rf._ext(None) == "bin"

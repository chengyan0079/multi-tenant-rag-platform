"""文件对象路径版本化单测：_build_object_key 含版本、mock put_file 回填真实版本且不覆盖。"""
import asyncio

from common.config import config
from core.file_provider.provider import _build_object_key


def test_object_key_includes_version() -> None:
    k1 = _build_object_key(1, "doc", "pdf", 1)
    k2 = _build_object_key(1, "doc", "pdf", 2)
    assert k1 != k2
    assert "/v1/" in k1 and "/v2/" in k2
    assert k1.endswith("doc.pdf")
    # 不同租户隔离
    assert _build_object_key(2, "doc", "pdf", 1) != k1


def test_mock_put_file_versioned(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path)
    from core.file_provider.mock_provider import MockFileProvider

    fp = MockFileProvider()

    async def _run():
        return (await fp.put_file(1, "doc", b"hello", "pdf", 1),
                await fp.put_file(1, "doc", b"world", "pdf", 2))

    r1, r2 = asyncio.run(_run())
    assert r1.version == 1 and r2.version == 2
    assert r1.oss_key != r2.oss_key            # 版本入路径，互不覆盖
    assert "/v1/" in r1.oss_key and "/v2/" in r2.oss_key

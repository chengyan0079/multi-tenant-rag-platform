"""MinioFileProvider 单测：_check_bucket 不自动建桶，缺失即抛错（mock Minio 客户端，不连真实服务）。"""
import asyncio
from unittest import mock

import pytest


def _import_mod():
    try:
        from core.file_provider.minio_provider import MinioFileProvider
        import core.file_provider.minio_provider as mod
        return MinioFileProvider, mod
    except ImportError:
        pytest.skip("minio 不可用，跳过 MinIO 测试")


class _FakeMinio:
    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.made = False

    def bucket_exists(self, bucket):
        return self._exists

    def make_bucket(self, bucket):
        self.made = True


def _make_fp(exists: bool):
    MinioFileProvider, _ = _import_mod()
    fp = MinioFileProvider()
    fp.client = _FakeMinio(exists)
    return fp


def test_check_bucket_ok() -> None:
    fp = _make_fp(True)
    fp._check_bucket()  # 不应抛错，也不应建桶
    assert fp.client.made is False


def test_check_bucket_missing_raises() -> None:
    fp = _make_fp(False)
    with pytest.raises(RuntimeError):
        fp._check_bucket()
    assert fp.client.made is False


def test_healthcheck_returns_false_when_bucket_missing() -> None:
    fp = _make_fp(False)
    assert asyncio.run(fp.healthcheck()) is False


def test_healthcheck_returns_true_when_bucket_exists() -> None:
    fp = _make_fp(True)
    assert asyncio.run(fp.healthcheck()) is True

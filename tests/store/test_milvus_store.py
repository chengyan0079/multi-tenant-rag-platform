"""MilvusStore.ensure_collection 单测：缺失则创建+加载，已存在则直接加载（mock 异步客户端）。"""
import asyncio

import pytest


def _import_mod():
    try:
        from store.milvus_store import MilvusStore
        return MilvusStore
    except ImportError:
        pytest.skip("pymilvus 不可用，跳过 Milvus 测试")


class _FakeIndexParams:
    def __init__(self) -> None:
        self.indexes: list[dict] = []

    def add_index(self, **kw) -> None:
        self.indexes.append(kw)


class _FakeClient:
    def __init__(self, exists: bool) -> None:
        self._exists = exists
        self.loaded = False
        self.created = False

    async def has_collection(self, name):
        return self._exists

    async def load_collection(self, name):
        self.loaded = True

    def prepare_index_params(self):
        return _FakeIndexParams()

    async def create_collection(self, name, schema=None, index_params=None):
        self.created = True


class _FakeModel:
    dense_dim = 1024


def test_ensure_collection_creates_when_missing() -> None:
    MilvusStore = _import_mod()
    client = _FakeClient(False)
    store = MilvusStore(client=client)
    store.bind(client, _FakeModel())
    asyncio.run(store.ensure_collection())
    assert client.created is True   # 缺失 → 创建
    assert client.loaded is True    # 创建后加载到内存


def test_ensure_collection_loads_when_exists() -> None:
    MilvusStore = _import_mod()
    client = _FakeClient(True)
    store = MilvusStore(client=client)
    store.bind(client, _FakeModel())
    asyncio.run(store.ensure_collection())
    assert client.created is False  # 已存在 → 不创建
    assert client.loaded is True    # 直接加载

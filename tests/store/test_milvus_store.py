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


def _import_funcs():
    mod = _import_mod()
    return mod


def test_build_filter_expr_base_only() -> None:
    mod = _import_funcs()
    expr = mod._build_filter_expr(100, 0)
    assert expr == 'tenant_id == 100 and scope == 0'


def test_build_filter_expr_with_doc_ids() -> None:
    mod = _import_funcs()
    expr = mod._build_filter_expr(100, 0, {'doc_ids': [1, 2, 3]})
    assert 'doc_id in [1, 2, 3]' in expr
    assert 'tenant_id == 100' in expr


def test_build_filter_expr_with_doc_and_ref_ids() -> None:
    mod = _import_funcs()
    expr = mod._build_filter_expr(100, 1, {'doc_ids': [5], 'ref_ids': [7, 8]})
    assert 'doc_id in [5]' in expr
    assert 'ref_id in [7, 8]' in expr
    assert 'scope == 1' in expr


def test_build_filter_expr_ignores_empty_filters() -> None:
    mod = _import_funcs()
    expr = mod._build_filter_expr(100, 0, {'doc_ids': []})
    assert 'doc_id' not in expr  # 空列表不下推


def test_stable_chunk_id_deterministic_and_version_sensitive() -> None:
    mod = _import_funcs()
    a = mod.stable_chunk_id(100, 1, 1, 'p1', 0)
    b = mod.stable_chunk_id(100, 1, 1, 'p1', 0)
    assert a == b  # 同输入恒定
    c = mod.stable_chunk_id(100, 1, 2, 'p1', 0)
    assert a != c  # 不同版本主键不冲突（支持回滚共存）


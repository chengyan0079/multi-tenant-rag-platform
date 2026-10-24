"""dashboard DAO 单测（store 层）：FaqDao / FileDao / DocDao / HealthDao。

DAO 返回 ORM 实体 / FileRef / UploadOutcome / dict / 状态值（实体→响应映射在路由层）；
用脚本化 fake session + fake provider，本地可跑，覆盖租户隔离与上传幂等三态。
"""
import datetime
from types import SimpleNamespace

import pytest

import store.faq_dao as faq_dao_module
import store.health_dao as health_dao_module
from app.app_state import app_state
from store.doc_dao import DocDao
from store.faq_dao import FaqDao
from store.file_dao import FileDao
from store.health_dao import HealthDao
from store.orm_models.faq import Faq
from store.orm_models.file import File, FileVersion


# ---------- 脚本化 fake session ----------
class _Result:
    def __init__(self, value):
        self._v = value

    def scalar_one_or_none(self):
        return self._v

    def scalar(self):
        return self._v

    def scalars(self):
        return self

    def all(self):
        return self._v or []


class _FakeSession:
    def __init__(self, exec_results=None, get_result=None, flush_error=None):
        self._exec = list(exec_results or [])
        self._get = get_result
        self._flush_error = flush_error
        self._auto = 0
        self.added = []
        self.committed = 0
        self.rolled_back = 0

    def add(self, row):
        self.added.append(row)

    def add_all(self, rows):
        for r in rows:
            self.add(r)

    def _assign_id(self, row):
        if getattr(row, "id", None) is None:
            self._auto += 1
            row.id = self._auto

    async def flush(self):
        if self._flush_error is not None:
            exc = self._flush_error
            self._flush_error = None
            raise exc
        for r in self.added:
            self._assign_id(r)

    async def commit(self):
        self.committed += 1

    async def rollback(self):
        self.rolled_back += 1

    async def refresh(self, row):
        self._assign_id(row)

    async def get(self, model, pk):
        return self._get

    async def execute(self, clause):
        return self._exec.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _faq(**kw):
    base = dict(tenant_id=1, question="q", answer="a", question_type=0,
                similar_questions="", enabled=1, updated_at=datetime.datetime(2026, 1, 1))
    base.update(kw)
    return Faq(**base)


# ================= FaqDao =================
def test_faq_create_returns_id(monkeypatch):
    import asyncio
    called = {}

    async def _upsert(tid):
        called["tid"] = tid
    monkeypatch.setattr(faq_dao_module, "upsert_faq_cache", _upsert)
    sess = _FakeSession()
    fid = asyncio.run(FaqDao(factory=lambda: sess).create(1, "q", "a", 0, None))
    assert fid == 1 and called["tid"] == 1 and sess.committed == 1


def test_faq_get_and_tenant_isolation():
    import asyncio
    row = _faq(id=5, tenant_id=1)
    got = asyncio.run(FaqDao(factory=lambda: _FakeSession(get_result=row)).get(1, 5))
    assert got is not None and got.id == 5
    none = asyncio.run(FaqDao(factory=lambda: _FakeSession(get_result=row)).get(2, 5))
    assert none is None


def test_faq_list():
    import asyncio
    rows = [_faq(id=1, tenant_id=1), _faq(id=2, tenant_id=1)]
    items = asyncio.run(FaqDao(factory=lambda: _FakeSession(exec_results=[_Result(rows)])).list(1))
    assert [r.id for r in items] == [1, 2]


def test_faq_update_and_soft_delete(monkeypatch):
    import asyncio

    async def _noop(*a, **k):
        return None

    deleted = {}

    async def _del(*args):
        deleted["args"] = args
    monkeypatch.setattr(faq_dao_module, "upsert_faq_cache", _noop)
    monkeypatch.setattr(faq_dao_module, "delete_faq_cache", _del)
    row = _faq(id=9, tenant_id=1)
    assert asyncio.run(FaqDao(factory=lambda: _FakeSession(get_result=row)).update(1, 9, "q2", "a2", 1, "s")) is True
    assert row.question == "q2"
    assert asyncio.run(FaqDao(factory=lambda: _FakeSession(get_result=row)).soft_delete(1, 9)) is True
    assert row.enabled == 0 and deleted["args"] == (9, 1)
    assert asyncio.run(FaqDao(factory=lambda: _FakeSession(get_result=None)).update(1, 9, "q", "a", 0, None)) is False


# ================= FileDao =================
class _FakeProvider:
    def __init__(self):
        self.puts = []

    async def put_file(self, tenant_id, base_key, data, ext, version):
        self.puts.append((tenant_id, base_key, version))
        return SimpleNamespace(oss_key=f"oss/v{version}/{base_key}.{ext}", file_name=f"{base_key}.{ext}",
                               file_type=ext, file_size=len(data), expires_at=None)

    async def get_file(self, oss_key, with_base64=False, tenant_id=None):
        self.last_tenant = tenant_id
        return SimpleNamespace(oss_key=oss_key, url=f"/admin/files/{oss_key}", file_name="f",
                               file_type="pdf", file_size=3, expires_at=None,
                               base64="QQ==" if with_base64 else None)

    async def get_files(self, oss_keys, with_base64=False, tenant_id=None):
        return [await self.get_file(k, with_base64, tenant_id) for k in oss_keys]

    async def get_bytes(self, oss_key, tenant_id=None):
        self.last_tenant = tenant_id
        return b"raw-bytes"


@pytest.fixture
def fake_provider(monkeypatch):
    fp = _FakeProvider()
    monkeypatch.setattr(app_state, "file_provider", fp)
    return fp


def test_file_upload_new(fake_provider):
    import asyncio
    sess = _FakeSession(exec_results=[_Result(None), _Result(None), _Result(None)])
    out = asyncio.run(FileDao(factory=lambda: sess).save_upload(1, "doc", b"hello", "pdf", "u1"))
    assert out.dedup is False and out.version == 1 and out.file_id == 1
    assert out.oss_key.startswith("oss/v1/")
    assert fake_provider.puts == [(1, "doc", 1)]


def test_file_upload_dedup(fake_provider):
    import asyncio
    existing = FileVersion(id=9, version=3, oss_key="oss/v3/doc.pdf", expires_at=None)
    sess = _FakeSession(exec_results=[_Result(File(id=1, tenant_id=1, base_key="doc")), _Result(existing)])
    out = asyncio.run(FileDao(factory=lambda: sess).save_upload(1, "doc", b"same", "pdf", "u1"))
    assert out.dedup is True and out.version == 3 and out.oss_key == "oss/v3/doc.pdf"
    assert fake_provider.puts == []


def test_file_upload_version_increment(fake_provider):
    import asyncio
    sess = _FakeSession(exec_results=[_Result(File(id=1, tenant_id=1, base_key="doc")), _Result(None), _Result(1)])
    out = asyncio.run(FileDao(factory=lambda: sess).save_upload(1, "doc", b"v2", "pdf", "u1"))
    assert out.dedup is False and out.version == 2


def test_file_list_and_delete():
    import asyncio
    docs = [File(id=1, tenant_id=1, base_key="d", create_user_id="u1", current_version=2, version_count=2, status=1)]
    rows = asyncio.run(FileDao(factory=lambda: _FakeSession(exec_results=[_Result(docs)])).list_documents(1))
    assert rows[0].base_key == "d" and rows[0].current_version == 2
    vers = [FileVersion(id=1, tenant_id=1, base_key="d", version=2, oss_key="k", file_hash="h",
                        upload_user_id="u", status=1, created_at=datetime.datetime(2026, 1, 1))]
    vrows = asyncio.run(FileDao(factory=lambda: _FakeSession(exec_results=[_Result(vers)])).list_versions(1, "d"))
    assert vrows[0].version == 2 and vrows[0].upload_user_id == "u"
    f = File(id=1, tenant_id=1, base_key="d", status=1)
    assert asyncio.run(FileDao(factory=lambda: _FakeSession(exec_results=[_Result(f)])).delete_document(1, "d")) is True
    assert f.status == 0
    assert asyncio.run(FileDao(factory=lambda: _FakeSession(exec_results=[_Result(None)])).delete_document(1, "x")) is False


def test_file_get_and_batch(fake_provider):
    import asyncio
    raw = asyncio.run(FileDao(factory=lambda: _FakeSession()).get_bytes("oss/v1/doc.pdf", 1))
    assert raw == b"raw-bytes"
    assert fake_provider.last_tenant == 1      # 单文件下载带租户校验
    refs = asyncio.run(FileDao(factory=lambda: _FakeSession()).batch_get_files(["a", "b"], True, 7))
    assert [r.oss_key for r in refs] == ["a", "b"]
    assert fake_provider.last_tenant == 7      # 批量下载同样带租户校验（修复跨租户漏洞）


# ================= DocDao / HealthDao =================
def test_doc_dao():
    import asyncio
    from store.orm_models.file import File
    from store.orm_models.knowledge import Doc

    # create: 关联 rag_file 落 rag_document，返回自增 id，初始 status=0
    f = File(id=3, tenant_id=1, base_key="doc", current_file_hash="h",
             current_oss_key="oss/k", file_name="f.pdf", status=1)
    sess = _FakeSession(exec_results=[_Result(f)])
    doc_id = asyncio.run(DocDao(factory=lambda: sess).create(1, 3, 0))
    assert doc_id == 1
    assert isinstance(sess.added[0], Doc)
    assert sess.added[0].tenant_id == 1 and sess.added[0].file_upload_id == 3
    assert sess.added[0].file_hash == "h" and sess.added[0].status == 0
    assert sess.committed == 1

    # get_status: 返回 {status, chunk_count, err_msg}
    d = Doc(id=7, tenant_id=1, status=2, chunk_count=4, err_msg=None)
    sess2 = _FakeSession(exec_results=[_Result(d)])
    assert asyncio.run(DocDao(factory=lambda: sess2).get_status(7)) == {
        "status": 2, "chunk_count": 4, "err_msg": None}

    # update_status: 回写状态（含 err_msg 截断）
    d3 = Doc(id=7, tenant_id=1, status=0)
    sess3 = _FakeSession(exec_results=[_Result(d3)])
    asyncio.run(DocDao(factory=lambda: sess3).update_status(7, 3, chunk_count=2, err_msg="boom"))
    assert d3.status == 3 and d3.chunk_count == 2 and d3.err_msg == "boom"

    # save_chunks: 先删后插，幂等
    chunks = [{"text": "t1", "parent_id": "p0", "parent_content": "pc",
               "doc_name": "f.pdf", "page": 0, "ref_id": 0}]
    sess4 = _FakeSession(exec_results=[_Result(None)])
    n = asyncio.run(DocDao(factory=lambda: sess4).save_chunks(1, 7, 0, chunks))
    assert n == 1 and len(sess4.added) == 1 and sess4.committed == 1


def test_health_dao(monkeypatch):
    import asyncio

    async def _redis():
        return True

    async def _pg():
        return False

    class _Milvus:
        async def has_collection(self, name):
            return True

    monkeypatch.setattr(health_dao_module, "redis_ping", _redis)
    monkeypatch.setattr(health_dao_module, "db_ping", _pg)
    class _Store:
        client = _Milvus()
    monkeypatch.setattr(app_state, "milvus_store", _Store())
    monkeypatch.setattr(app_state, "model_service", object())
    d = asyncio.run(HealthDao().check())
    assert d["redis"] is True and d["postgres"] is False and d["milvus"] is True and d["model"] is True
    monkeypatch.setattr(app_state, "milvus_store", None)
    assert asyncio.run(HealthDao().check())["milvus"] is False


def test_health_dao_extra_components(monkeypatch):
    """MCP / Nacos 服务发现 / 配置中心 三项均取自内存状态，未启用或未就绪为 False。"""
    import asyncio

    async def _no():
        return False

    monkeypatch.setattr(health_dao_module, "redis_ping", _no)
    monkeypatch.setattr(health_dao_module, "db_ping", _no)
    monkeypatch.setattr(app_state, "milvus_store", None)
    monkeypatch.setattr(app_state, "model_service", None)

    # 全部未注入：三项都是 False
    monkeypatch.setattr(app_state, "mcp_server", None)
    monkeypatch.setattr(app_state, "nacos_discovery", None)
    monkeypatch.setattr(app_state, "config_center", None)
    d = asyncio.run(HealthDao().check())
    assert d["mcp"] is False and d["nacos"] is False and d["config_center"] is False

    # 已就绪：MCP 注入实例、Nacos 注册成功标记、loader 已启动
    monkeypatch.setattr(app_state, "mcp_server", object())
    monkeypatch.setattr(app_state, "nacos_discovery", SimpleNamespace(is_registered=True))
    monkeypatch.setattr(app_state, "config_center", SimpleNamespace(is_started=True))
    d = asyncio.run(HealthDao().check())
    assert d["mcp"] is True and d["nacos"] is True and d["config_center"] is True

    # 有 Manager 但未注册成功（注册失败被旁路）仍为 False
    monkeypatch.setattr(app_state, "nacos_discovery", SimpleNamespace(is_registered=False))
    assert asyncio.run(HealthDao().check())["nacos"] is False

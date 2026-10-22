"""routes_doc 单测：mock DocDao / FileDao / enqueue_index，验证创建 + 轮询编排与状态映射。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dashboard import routes_doc
from common.response import ErrCode


class _DocDao:
    @staticmethod
    async def create(tenant_id, file_id, doc_type=0):
        return 11
    @staticmethod
    async def get_status(doc_id):
        return {'status': 2, 'chunk_count': 3, 'err_msg': None}
    @staticmethod
    async def update_status(*a, **k):
        return None


class _FileDao:
    @staticmethod
    async def get_oss_key(tenant_id, file_id):
        return ('oss-k', 'f.pdf')


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(routes_doc, 'DocDao', _DocDao)
    monkeypatch.setattr(routes_doc, 'FileDao', _FileDao)
    calls = []
    async def _enqueue(tenant_id, doc_id, oss_key, file_name, doc_type):
        calls.append((tenant_id, doc_id, oss_key, file_name, doc_type))
    monkeypatch.setattr(routes_doc, 'enqueue_index', _enqueue)
    app = FastAPI()
    app.include_router(routes_doc.router)
    return TestClient(app), calls


def test_create_doc(env):
    client, calls = env
    r = client.post('/admin/docs', json={'file_id': 5}, headers={'X-Tenant-Id': '100'})
    assert r.status_code == 200
    d = r.json()['data']
    assert d['doc_id'] == 11 and d['status'] == 'pending'
    assert calls == [(100, 11, 'oss-k', 'f.pdf', 0)]


def test_doc_status(env):
    client, _ = env
    r = client.get('/admin/docs/11/status', headers={'X-Tenant-Id': '100'})
    assert r.status_code == 200
    assert r.json()['data']['status'] == 'done'


def test_create_doc_missing_file(env, monkeypatch):
    class _F:
        @staticmethod
        async def get_oss_key(t, f):
            return (None, None)
    monkeypatch.setattr(routes_doc, 'FileDao', _F)
    client, calls = env
    r = client.post('/admin/docs', json={'file_id': 5}, headers={'X-Tenant-Id': '100'})
    assert r.json()['data']['status'] == 'failed'
    assert calls == []


def test_status_not_found(env, monkeypatch):
    class _D:
        @staticmethod
        async def get_status(doc_id):
            raise KeyError('x')
    monkeypatch.setattr(routes_doc, 'DocDao', _D)
    client, _ = env
    r = client.get('/admin/docs/999/status', headers={'X-Tenant-Id': '100'})
    assert r.json()['code'] == ErrCode.NOT_FOUND

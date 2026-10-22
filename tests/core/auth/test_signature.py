"""core.auth.signature 单测：canonical 约定、签名生成/校验、时效、密钥轮换。

签名是"防伪造身份头"的最后一道，这里把各类不通过的原因都钉死，避免改动时静默放宽。
"""
from core.auth import signature as s

SECRET = 'devsecret'
OLD_SECRET = 'oldsecret'


def test_canonical_is_stable_and_sensitive_to_identity() -> None:
    base = s.canonical_string('GET', '/v1/faq', 1700000000, 'n1', '1', 'u1')
    assert base == s.canonical_string('GET', '/v1/faq', 1700000000, 'n1', '1', 'u1')
    # 任一字段变了（含身份），待签串必须变
    assert base != s.canonical_string('POST', '/v1/faq', 1700000000, 'n1', '1', 'u1')
    assert base != s.canonical_string('GET', '/v1/faq2', 1700000000, 'n1', '1', 'u1')
    assert base != s.canonical_string('GET', '/v1/faq', 1700000000, 'n1', '2', 'u1')
    assert base != s.canonical_string('GET', '/v1/faq', 1700000000, 'n1', '1', 'u2')


def test_build_and_verify_roundtrip() -> None:
    headers = s.build_auth_headers('POST', '/v1/sessions', SECRET, tenant='1', user='u1')
    headers['x-tenant-id'] = '1'
    headers['x-user-id'] = 'u1'
    assert s.verify(headers, 'POST', '/v1/sessions', SECRET, ttl=60,
                    now=int(headers['X-Auth-Timestamp'])) is None


def test_verify_rejects_tampered_request() -> None:
    headers = s.build_auth_headers('GET', '/v1/faq', SECRET, tenant='1', user='u1')
    headers['x-tenant-id'] = '1'
    headers['x-user-id'] = 'u1'
    now = int(headers['X-Auth-Timestamp'])

    assert '签名校验失败' in (s.verify(headers, 'POST', '/v1/faq', SECRET, 60, now) or '')
    assert '签名校验失败' in (s.verify(headers, 'GET', '/v1/faq2', SECRET, 60, now) or '')
    # 身份头被改：签名不再匹配
    tampered = dict(headers, **{'x-tenant-id': '2'})
    assert '签名校验失败' in (s.verify(tampered, 'GET', '/v1/faq', SECRET, 60, now) or '')


def test_verify_rejects_missing_or_bad_shape() -> None:
    assert '缺少签名头' in (s.verify({'x-auth-timestamp': '1'}, 'GET', '/v1/faq', SECRET, 60, 1) or '')
    bad = s.build_auth_headers('GET', '/v1/faq', SECRET)
    assert '签名格式非法' in (s.verify({**bad, 'X-Auth-Signature': 'abc'}, 'GET', '/v1/faq', SECRET, 60, 1) or '')
    assert '必须为秒级整数' in (s.verify({**bad, 'X-Auth-Timestamp': 'not-a-number'},
                                        'GET', '/v1/faq', SECRET, 60, 1) or '')


def test_verify_rejects_expired_and_future() -> None:
    headers = s.build_auth_headers('GET', '/v1/faq', SECRET)
    ts = int(headers['X-Auth-Timestamp'])
    assert '过期' in (s.verify(headers, 'GET', '/v1/faq', SECRET, 60, ts + 61) or '')
    assert '过期' in (s.verify(headers, 'GET', '/v1/faq', SECRET, 60, ts - 61) or '')
    assert s.verify(headers, 'GET', '/v1/faq', SECRET, 60, ts + 59) is None   # 窗口内通过


def test_verify_supports_secret_rotation() -> None:
    headers = s.build_auth_headers('GET', '/v1/faq', OLD_SECRET)
    now = int(headers['X-Auth-Timestamp'])
    # 新密钥签的名，用 [新, 旧] 都能过；用无关密钥不过
    assert s.verify(headers, 'GET', '/v1/faq', [SECRET, OLD_SECRET], 60, now) is None
    assert s.verify(headers, 'GET', '/v1/faq', [OLD_SECRET], 60, now) is None
    assert s.verify(headers, 'GET', '/v1/faq', [SECRET], 60, now) is not None


def test_verify_empty_secret_never_passes() -> None:
    headers = s.build_auth_headers('GET', '/v1/faq', SECRET)
    assert s.verify(headers, 'GET', '/v1/faq', '', 60, int(headers['X-Auth-Timestamp'])) is not None


def test_signature_is_hex_and_nonce_unique() -> None:
    a = s.build_auth_headers('GET', '/v1/faq', SECRET)
    b = s.build_auth_headers('GET', '/v1/faq', SECRET)
    assert len(a['X-Auth-Signature']) == 64
    assert a['X-Auth-Nonce'] != b['X-Auth-Nonce']           # 每次不同，防重放
    assert a['X-Auth-Signature'] != b['X-Auth-Signature']

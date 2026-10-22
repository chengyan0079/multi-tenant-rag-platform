"""core.auth - 调用方签名（HMAC-SHA256）。

只做一件事：确认"这个请求确实来自可信的调用方（网关联调方），且身份头没被改过"。

为什么需要：本服务的租户/用户身份来自网关下发的 X-Tenant-Id / X-User-Id 明文头，
任何人能直连本服务就能伪造身份读别租户的数据。验签后，没有密钥就造不出合法签名。

签名内容与形态：
  canonical = METHOD \n PATH \n TIMESTAMP \n NONCE \n TENANT \n USER
  signature = HMAC-SHA256(secret, canonical).hexdigest()
请求头：
  X-Auth-Timestamp  秒级时间戳（与服务端偏差超过 AUTH_HMAC_TTL 秒即拒）
  X-Auth-Nonce      随机串，服务端用 Redis 去重（防重放）
  X-Auth-Signature  上述签名

局限（有意为之，换实现简单）：
  - 不签 query 与请求体：内部网关场景够用，也避免中间件消费 body（影响流式/大文件）。
    若将来要防"改参数"，再扩展 canonical 而非改结构。
  - 对称密钥：密钥需要下发到网关与服务端；不如非对称 JWT 便于分发，但依赖最少。
"""

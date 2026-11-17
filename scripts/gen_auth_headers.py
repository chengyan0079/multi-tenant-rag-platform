"""生成调用方签名头（X-Auth-Timestamp/Nonce/Signature）用于接口自测，扮演"网关"角色按 core.auth 一致 canonical 生成签名并输出 curl。支持直接运行（改 DEMO_*）或命令行参数。"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.config import config  # noqa: E402  先加载配置，下方默认值取自配置
from core.auth.signature import build_auth_headers  # noqa: E402

# ===== 直接运行时改这里即可（命令行传参会覆盖同名项）=====
DEMO_METHOD = 'POST'                           # HTTP 方法
DEMO_PATH = '/v1/sessions'                     # 请求路径（不含 query）
DEMO_TENANT = str(config.DEFAULT_TENANT_ID)    # X-Tenant-Id，默认取配置的兜底租户
DEMO_USER = config.DEFAULT_USER_ID             # X-User-Id，默认取配置的兜底用户
DEMO_SECRET = config.AUTH_HMAC_SECRET          # 密钥，默认取配置；配置没配就在这一行填
# 服务地址：默认按 HOST/PORT 拼（0.0.0.0 等通配绑定回退为回环地址）；置空则不输出 curl
_HOST = '127.0.0.1' if config.HOST in ('', '0.0.0.0', '*', '::') else config.HOST
DEMO_BASE_URL = f'http://{_HOST}:{config.PORT}'


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器：默认值取上方 DEMO_*，命令行传参优先。"""
    p = argparse.ArgumentParser(description='生成 HMAC 签名头（自测用）')
    p.add_argument('--method', default=DEMO_METHOD, help='HTTP 方法')
    p.add_argument('--path', default=DEMO_PATH, help='请求路径，如 /v1/sessions（不含 query）')
    p.add_argument('--tenant', default=DEMO_TENANT, help='X-Tenant-Id，可为空')
    p.add_argument('--user', default=DEMO_USER, help='X-User-Id，可为空')
    p.add_argument('--secret', default=DEMO_SECRET, help='密钥；缺省取配置 AUTH_HMAC_SECRET')
    p.add_argument('--base-url', default=DEMO_BASE_URL, help='服务地址；给了就额外输出 curl 命令')
    return p


def main(argv: list[str] | None = None) -> None:
    """argv 为 None 时走 sys.argv（命令行），传空列表则用 DEMO_* 默认值。"""
    args = build_parser().parse_args(argv)
    secret = args.secret or config.AUTH_HMAC_SECRET
    if not secret:
        print('[错误] 未提供密钥：在文件顶部填 DEMO_SECRET，或在 .env 配置 AUTH_HMAC_SECRET')
        raise SystemExit(2)

    headers = build_auth_headers(
        method=args.method, path=args.path, secret=secret,
        tenant=args.tenant, user=args.user)
    identity: dict[str, str] = {}
    if args.tenant:
        identity['X-Tenant-Id'] = args.tenant
    if args.user:
        identity['X-User-Id'] = args.user

    print(f'# {args.method.upper()} {args.path}')
    for k, v in {**identity, **headers}.items():
        print(f'{k}: {v}')

    if args.base_url:
        flags = ' '.join(f'-H "{k}: {v}"' for k, v in {**identity, **headers}.items())
        print('\ncurl -X ' + args.method.upper() + ' ' + flags + ' '
              + args.base_url.rstrip('/') + args.path)


if __name__ == '__main__':
    main()

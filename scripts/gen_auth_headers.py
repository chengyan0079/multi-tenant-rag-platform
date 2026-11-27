"""生成调用方签名头（X-Auth-Timestamp/Nonce/Signature），按 core.auth 的 canonical 规则签名并输出 curl。默认值取自配置，命令行参数可覆盖。"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.config import config  # noqa: E402  先加载配置，下方默认值取自配置
from core.auth.signature import build_auth_headers  # noqa: E402

DEFAULT_METHOD = 'POST'
DEFAULT_PATH = '/v1/sessions'
DEFAULT_TENANT = str(config.DEFAULT_TENANT_ID)
DEFAULT_USER = config.DEFAULT_USER_ID
DEFAULT_SECRET = config.AUTH_HMAC_SECRET
_HOST = '127.0.0.1' if config.HOST in ('', '0.0.0.0', '*', '::') else config.HOST
DEFAULT_BASE_URL = f'http://{_HOST}:{config.PORT}'


def build_parser() -> argparse.ArgumentParser:
    """默认值取自配置，命令行参数覆盖。"""
    p = argparse.ArgumentParser(description='生成 HMAC 签名头')
    p.add_argument('--method', default=DEFAULT_METHOD, help='HTTP 方法')
    p.add_argument('--path', default=DEFAULT_PATH, help='请求路径（不含 query）')
    p.add_argument('--tenant', default=DEFAULT_TENANT, help='X-Tenant-Id')
    p.add_argument('--user', default=DEFAULT_USER, help='X-User-Id')
    p.add_argument('--secret', default=DEFAULT_SECRET, help='密钥；缺省取配置 AUTH_HMAC_SECRET')
    p.add_argument('--base-url', default=DEFAULT_BASE_URL, help='服务地址；给了就额外输出 curl 命令')
    return p


def main(argv: list[str] | None = None) -> None:
    """argv 为 None 时走 sys.argv，传空列表则用配置默认值。"""
    args = build_parser().parse_args(argv)
    secret = args.secret or config.AUTH_HMAC_SECRET
    if not secret:
        print('[错误] 未提供密钥：在 .env 配置 AUTH_HMAC_SECRET 或通过 --secret 传入')
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

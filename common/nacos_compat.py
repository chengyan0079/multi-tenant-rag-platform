"""Nacos 版本适配助手（服务发现 / 配置中心共用）。

服务端为 **Nacos 3.x**，故统一使用 **nacos-sdk-python 2.0+**（导入路径 `v2.nacos`，
全异步 API）；1.x 的 `import nacos` + `NacosClient` 已不可用（3.x 服务端移除了其
v1 注册接口 /nacos/v1/ns/instance，会返回 501）。

两个能力：
- `normalize_namespace_id`：'public' → ''（Nacos 公共命名空间的 id 为空串）。
- `resolve_namespace_id`：把命名空间**显示名**（如 rag-ultra）查成 3.x 要求的 **UUID**。
- `import_error_hint`：导入失败时给出可操作提示（区分"没装"与"装的是旧版 1.x"）。
"""
import uuid

from common.logger import logger

_INSTALL_CMD = 'pip install -U "nacos-sdk-python>=2"'


def normalize_namespace_id(namespace: str | None) -> str:
    """Nacos 公共命名空间：控制台显示 'public'，实际 namespaceId 为空串。"""
    if not namespace or namespace == 'public':
        return ''
    return namespace


def resolve_namespace_id(server_addr: str, namespace: str,
                         username: str = '', password: str = '',
                         timeout: float = 3.0) -> str:
    """把命名空间解析为 Nacos 3.x 需要的 namespaceId（UUID）。

    - 空 / 'public' → ''；本身是 UUID → 原样返回；
    - 否则查 `/nacos/v3/admin/core/namespace/list`，按 namespaceShowName 映射成 UUID；
    - 查询失败则原样返回（交由 Nacos 报错，不在此静默吞掉）。
    """
    ns = normalize_namespace_id(namespace)
    if ns == '':
        return ''
    try:
        uuid.UUID(ns)
        return ns
    except ValueError:
        pass
    try:
        import requests

        headers: dict[str, str] = {}
        if username:
            login = requests.post(f'{server_addr}/nacos/v3/auth/login',
                                  data={'username': username, 'password': password},
                                  timeout=timeout)
            if login.status_code == 200:
                token = login.json().get('accessToken')
                if token:
                    headers['Authorization'] = f'Bearer {token}'
        resp = requests.get(f'{server_addr}/nacos/v3/admin/core/namespace/list',
                            headers=headers, timeout=timeout)
        if resp.status_code == 200:
            for item in (resp.json().get('data') or []):
                if isinstance(item, dict) and item.get('namespaceShowName') == ns:
                    return item['namespace']
    except Exception:  # noqa: BLE001  解析失败不阻断，交由 Nacos 报错
        logger.warning(f'命名空间解析失败，按原值使用: {namespace}')
    return ns


def import_error_hint(feature: str) -> str:
    """`from v2.nacos import ...` 失败时的提示：区分"没装"与"装的是旧版 1.x"。"""
    import importlib.util

    if importlib.util.find_spec('nacos') is not None:
        return (
            f'已安装 nacos-sdk-python 1.x（旧 API：import nacos），不支持 Nacos 3.x 服务端，'
            f'{feature}不可用。请升级：{_INSTALL_CMD}'
        )
    return f'未安装 nacos-sdk-python 2.0+，{feature}不可用（{_INSTALL_CMD}）'

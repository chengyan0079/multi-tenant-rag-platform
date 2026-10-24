"""
nacos_config - Nacos 3.x 配置中心客户端（直连 Open API）

与服务发现（app/managers/nacos_discover_manager.py，namespace=public）完全独立：
- 命名空间：config.NACOS_CONFIG_NAMESPACE（**填命名空间 ID，不是显示名**；默认见 common/config.py，部署用 .env 覆盖）
- 变量：config.NACOS_CONFIG_*（与 NACOS_* 分离）

Nacos 3.x API 约定（仅 3.x，不做版本兼容）：
- 前缀 /nacos/v3，读/写/列/历史统一走 admin 路径 /v3/admin/cs/config(+/list|/history/list)
  （client 只读接口 /v3/client/cs/config 需应用级身份，本部署未启用，故统一用 admin）
- 参数驼峰：namespaceId / groupName / dataId
- 响应包裹在 data 字段，成功业务码 code=0（message "success"），不存在返回 code=20004
- 历史记录主键为 id（归一到 nid 以兼容上层）

设计要点（见 docs/配置中心对接设计.md）：
- 读类（命名空间列表 / 配置列表 / 配置详情 / 历史）直连 Nacos，无副作用。
- 写类（publish / remove / rollback）仅在审批通过后由 ConfigChangeService 调用，
  并携带 casMd5 做乐观锁，防止基于过期基准的覆盖写（lost update）。
- 历史/当前态以 Nacos 为准，不本地冗余。

实现：基于 requests 同步调用（FastAPI 异步侧用 asyncio.to_thread 包裹），
Nacos Open API 对 casMd5 / history / rollback 的参数控制比 nacos-sdk 更直接。
"""
import uuid
from typing import Any

import requests

from common.config import config
from common.logger import logger


class ConfigCenterError(RuntimeError):
    """配置中心调用异常。"""


class ConfigCenterClient:
    """Nacos 3.x 配置中心 REST 客户端（直连 /nacos/v3 Open API）。"""

    def __init__(
        self,
        server_addr: str | None = None,
        namespace: str | None = None,
        group: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.server_addr = (server_addr or config.NACOS_CONFIG_SERVER_ADDR).rstrip('/')
        self.namespace = namespace or config.NACOS_CONFIG_NAMESPACE
        self.group = group or config.NACOS_CONFIG_GROUP
        self.user = user if user is not None else config.NACOS_CONFIG_USER
        self.password = password if password is not None else config.NACOS_CONFIG_PASSWORD
        self.timeout = timeout
        self._token: str | None = None
        self._ns_resolved: str | None = None

    # ---------- 路径 / 参数名（Nacos 3.x 固定） ----------
    def _base(self) -> str:
        return f'{self.server_addr}/nacos/v3'

    def _path(self, action: str) -> str:
        mapping = {
            'get': '/admin/cs/config',
            'publish': '/admin/cs/config',
            'remove': '/admin/cs/config',
            'list': '/admin/cs/config/list',
            'history': '/admin/cs/history/list',
            # 历史详情（按 nid 取单条，返回 content；注意不是 .../list）
            'history_detail': '/admin/cs/history',
            # 注：3.2.x 无 rollback 端点，回滚由 get_history(取内容) + publish 实现
            'namespaces': '/admin/core/namespace/list',
        }
        return f'{self._base()}{mapping[action]}'

    # ---------- 鉴权（无鉴权时跳过） ----------
    def _ensure_token(self) -> str | None:
        if self._token or not self.user:
            return self._token
        login_path = f'{self.server_addr}/nacos/v3/auth/login'
        resp = requests.post(
            login_path,
            data={'username': self.user, 'password': self.password},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise ConfigCenterError(f'Nacos 登录失败: {resp.status_code} {resp.text}')
        payload = resp.json()
        self._token = payload.get('accessToken')
        if not self._token:
            raise ConfigCenterError('Nacos 登录未返回 accessToken')
        return self._token

    def _auth_header(self) -> dict:
        token = self._ensure_token()
        if not token:
            return {}
        return {'Authorization': f'Bearer {token}'}

    def _ns_id(self) -> str:
        """把命名空间解析为 Nacos 的 namespaceId（UUID）。

        Nacos 3.x 的 namespaceId 必须是命名空间 UUID，而非显示名；
        若配置里写的是显示名（如 'rag-ultra'），这里自动查命名空间列表映射成 UUID。
        """
        if self._ns_resolved is not None:
            return self._ns_resolved
        try:
            uuid.UUID(self.namespace)
            self._ns_resolved = self.namespace
            return self._ns_resolved
        except ValueError:
            pass
        try:
            resp = requests.get(self._path('namespaces'), params={},
                                headers=self._auth_header(), timeout=self.timeout)
            for n in (self._check(resp) or []):
                if isinstance(n, dict) and n.get('namespaceShowName') == self.namespace:
                    self._ns_resolved = n['namespace']
                    return self._ns_resolved
        except Exception:
            pass
        self._ns_resolved = self.namespace
        return self._ns_resolved

    def _query(self, extra: dict[str, Any]) -> dict[str, Any]:
        params: dict[str, Any] = {'namespaceId': self._ns_id()}
        params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def _check(self, resp: requests.Response) -> Any:
        if resp.status_code != 200:
            raise ConfigCenterError(f'Nacos 返回 {resp.status_code}: {resp.text}')
        text = resp.text.strip()
        if text in ('true', 'false'):
            return text == 'true'
        try:
            data = resp.json()
        except ValueError:
            return text
        # 3.x 统一响应 {code, message, data}；成功码为 0
        if isinstance(data, dict) and 'code' in data:
            if data.get('code') != 0:
                raise ConfigCenterError(f"Nacos 业务错误: {data.get('message')} (code={data.get('code')})")
            payload = data.get('data')
            return payload if payload is not None else True
        return data

    # ---------- 读类（直连，无副作用） ----------
    def list_namespaces(self) -> list[dict]:
        """获取命名空间列表。"""
        resp = requests.get(
            self._path('namespaces'), params=self._query({}),
            headers=self._auth_header(), timeout=self.timeout,
        )
        data = self._check(resp)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get('data', []) or []
        return []

    def get_config(self, data_id: str, group: str | None = None) -> str | None:
        """获取当前配置内容；不存在返回 None。

        Nacos 3.x：成功返回配置详情对象（内容在 data.content）；不存在/无权限返回 {"code":20004,...}。
        """
        params = self._query({'dataId': data_id, 'groupName': group or self.group})
        resp = requests.get(
            self._path('get'), params=params,
            headers=self._auth_header(), timeout=self.timeout,
        )
        if resp.status_code == 404:
            return None
        ct = resp.text.strip()
        if not ct:
            return None
        try:
            j = resp.json()
        except ValueError:
            return ct  # 纯文本配置内容（兜底）
        if isinstance(j, dict) and 'code' in j:
            if j.get('code') == 0:
                d = j.get('data')
                if isinstance(d, str):
                    return d
                if isinstance(d, dict):
                    return d.get('content')  # 3.x admin 返回配置详情对象，内容在 content 字段
                return ct
            if j.get('code') == 20004 or 'not found' in str(j.get('message', '')).lower():
                return None
            raise ConfigCenterError(f"Nacos 查询失败: {j.get('message')} (code={j.get('code')})")
        return ct

    def list_configs(
        self,
        data_id: str | None = None,
        group: str | None = None,
        page_no: int = 1,
        page_size: int = 100,
    ) -> dict:
        """指定命名空间/group 查配置列表。"""
        resp = requests.get(
            self._path('list'), params=self._query({
                'pageNo': page_no, 'pageSize': page_size,
                'groupName': group or self.group, 'dataId': data_id or '',
            }),
            headers=self._auth_header(), timeout=self.timeout,
        )
        return self._check(resp) or {}

    def list_history(
        self,
        data_id: str,
        group: str | None = None,
        page_no: int = 1,
        page_size: int = 100,
    ) -> dict:
        """查看配置历史。3.x 历史记录主键为 id，归一到 nid 以兼容 service 层。"""
        resp = requests.get(
            self._path('history'),
            params=self._query({
                'dataId': data_id, 'groupName': group or self.group,
                'pageNo': page_no, 'pageSize': page_size,
            }),
            headers=self._auth_header(), timeout=self.timeout,
        )
        data = self._check(resp) or {}
        for it in (data.get('pageItems') or []):
            if isinstance(it, dict) and 'nid' not in it and 'id' in it:
                it['nid'] = it['id']
        return data

    def get_history(self, data_id: str, nid: int, group: str | None = None) -> dict | None:
        """单条历史详情（Nacos 3.2.x 参数名为 **nid**；返回 data 含 content）。

        注意用的是 `/admin/cs/history`（详情），不是 `/admin/cs/history/list`（列表）。
        """
        resp = requests.get(
            self._path('history_detail'),
            params=self._query({
                'dataId': data_id, 'groupName': group or self.group, 'nid': nid,
            }),
            headers=self._auth_header(), timeout=self.timeout,
        )
        data = self._check(resp)
        if isinstance(data, dict):
            return data.get('data') or data
        return data

    # ---------- 写类（仅审批通过后由 Service 调用，带 CAS） ----------
    def publish_config(
        self,
        data_id: str,
        content: str,
        group: str | None = None,
        cas_md5: str | None = None,
    ) -> bool:
        """发布/更新配置；cas_md5 非空时做乐观锁，与服务器当前 md5 不一致则失败。"""
        data = {
            'namespaceId': self._ns_id(),
            'groupName': group or self.group,
            'dataId': data_id, 'content': content, 'type': 'text',
        }
        if cas_md5:
            data['casMd5'] = cas_md5
        resp = requests.post(
            self._path('publish'), data=data,
            headers=self._auth_header(), timeout=self.timeout,
        )
        result = self._check(resp)
        if isinstance(result, bool):
            return result
        return str(result).strip().lower() == 'true'

    def remove_config(
        self,
        data_id: str,
        group: str | None = None,
        cas_md5: str | None = None,
    ) -> bool:
        """删除配置；cas_md5 非空时做乐观锁。"""
        params = self._query({'dataId': data_id, 'groupName': group or self.group})
        if cas_md5:
            params['casMd5'] = cas_md5
        resp = requests.delete(
            self._path('remove'), params=params,
            headers=self._auth_header(), timeout=self.timeout,
        )
        result = self._check(resp)
        if isinstance(result, bool):
            return result
        return str(result).strip().lower() == 'true'

    def rollback(self, data_id: str, nid: int, group: str | None = None) -> bool:
        """按历史 nid 回滚（审批通过后由 Service 调用）。

        Nacos 3.2.x 已移除 `/nacos/v3/admin/cs/history/rollback` 端点（返回 404），
        故改为等价实现：取该历史版本内容 → 重新发布。
        """
        hist = self.get_history(data_id, nid, group)
        content = hist.get('content') if isinstance(hist, dict) else None
        if not content:
            raise ConfigCenterError(
                f'回滚失败：取不到历史内容 data_id={data_id} nid={nid}')
        return self.publish_config(data_id, content, group)


_client: ConfigCenterClient | None = None


def get_config_center_client() -> ConfigCenterClient:
    """懒加载单例客户端。"""
    global _client
    if _client is None:
        if not config.NACOS_CONFIG_ENABLED:
            raise ConfigCenterError('Nacos 配置中心未启用（NACOS_CONFIG_ENABLED=False）')
        _client = ConfigCenterClient()
    return _client

"""loader - 配置中心热加载（P1）

业务侧从 Nacos 配置中心（namespace=rag-ultra）读取配置，并监听变更热更新到本地缓存。
与控制台"直连 Nacos 查询"（core.config_center.nacos_config，requests + Open API）不同，
本 loader 用 **SDK 的 gRPC 长连接**订阅变更，避免轮询。

适配 **Nacos Server 3.x + nacos-sdk-python 2.0+**（`v2.nacos`，全异步）：
  - 客户端：`ClientConfig` + `await NacosConfigService.create_config_service(...)`
  - 读取：`await get_config(ConfigParam(data_id=..., group=...))`
  - 监听：`await add_listener(data_id, group, listener)`，listener 为 async，
    签名 `(tenant, group, data_id, content)`
故本 loader 的对外方法也相应改为 async。
"""
import asyncio
import json
from typing import Callable, Optional

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore
    _HAS_YAML = False

from common.config import config as app_config
from common.logger import logger
from common.nacos_compat import import_error_hint, resolve_namespace_id

WatcherCb = Callable[[str, str, Optional[str]], None]   # (data_id, group, content)

_loader: "ConfigCenterLoader | None" = None


class ConfigCenterLoader:
    """配置中心本地缓存 + 热更新（SDK gRPC 长连接订阅）。"""

    def __init__(self) -> None:
        self._client = None
        self._cache: dict[tuple[str, str], str] = {}
        self._started = False

    @property
    def is_started(self) -> bool:
        """是否已连上 Nacos（供 /admin/health 读取）。"""
        return self._started

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        """连接 Nacos 配置中心（gRPC）；未启用或 SDK 缺失时跳过。"""
        if not app_config.NACOS_CONFIG_ENABLED:
            logger.info('配置中心未启用（NACOS_CONFIG_ENABLED=False），跳过热加载')
            return
        try:
            from v2.nacos import ClientConfig, NacosConfigService
        except ImportError:
            logger.warning(import_error_hint('配置中心热加载'))
            return
        # Nacos 3.x 的 namespaceId 需为 UUID，显示名（rag-ultra）先查成 UUID
        ns_id = await asyncio.to_thread(
            resolve_namespace_id,
            app_config.NACOS_CONFIG_SERVER_ADDR,
            app_config.NACOS_CONFIG_NAMESPACE,
            app_config.NACOS_CONFIG_USER,
            app_config.NACOS_CONFIG_PASSWORD,
        )
        cc = ClientConfig(
            server_addresses=app_config.NACOS_CONFIG_SERVER_ADDR,
            namespace_id=ns_id,
            username=app_config.NACOS_CONFIG_USER or None,
            password=app_config.NACOS_CONFIG_PASSWORD or None,
        )
        self._client = await NacosConfigService.create_config_service(cc)
        self._started = True
        logger.info(f'配置中心热加载已启动: ns={app_config.NACOS_CONFIG_NAMESPACE} '
                    f'addr={app_config.NACOS_CONFIG_SERVER_ADDR}')

    async def stop(self) -> None:
        """关闭 gRPC 连接并清空本地缓存（重复调用安全）。"""
        if self._client is not None:
            try:
                await self._client.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self._cache.clear()
        self._client = None
        self._started = False
        logger.info('配置中心热加载已停止')

    # ---------- 读取 ----------
    def _key(self, data_id: str, group: str | None) -> tuple[str, str]:
        return (group or app_config.NACOS_CONFIG_GROUP, data_id)

    async def _on_change(self, tenant: str, group: str, data_id: str, content: str) -> None:
        """SDK 监听回调（async，签名由 SDK 约定）。"""
        self._cache[(group, data_id)] = content
        logger.info(f'配置中心热更新: data_id={data_id} group={group}')

    async def get(self, data_id: str, group: str | None = None) -> Optional[str]:
        """读取配置（优先缓存；未命中则从 Nacos 拉取并自动订阅变更）。"""
        key = self._key(data_id, group)
        if key in self._cache:
            return self._cache[key]
        if not self._started or self._client is None:
            return None
        from v2.nacos import ConfigParam, NacosException
        try:
            content = await self._client.get_config(
                ConfigParam(data_id=data_id, group=key[0]))
        except NacosException as e:
            logger.warning(f'配置中心读取失败: data_id={data_id} err={e}')
            return None
        if not content:
            return None
        self._cache[key] = content
        try:
            await self._client.add_listener(data_id, key[0], self._on_change)
        except NacosException as e:
            logger.warning(f'注册配置监听失败（不影响读取）: data_id={data_id} err={e}')
        return content

    async def get_json(self, data_id: str, group: str | None = None) -> Optional[dict]:
        """按 data_id 后缀选择解析器：yaml/yml 用 YAML，其余用 JSON。"""
        content = await self.get(data_id, group)
        if content is None:
            return None
        ext = data_id.rsplit('.', 1)[-1].lower() if '.' in data_id else ''
        if ext in ('yaml', 'yml') and _HAS_YAML:
            try:
                return yaml.safe_load(content)  # type: ignore[union-attr]
            except yaml.YAMLError:  # type: ignore[union-attr]
                logger.warning(f'配置非 YAML，无法解析: data_id={data_id}')
                return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            logger.warning(f'配置非 JSON，无法解析: data_id={data_id}')
            return None

    async def subscribe(self, data_id: str, group: str | None, callback: WatcherCb) -> None:
        """业务侧主动订阅某配置变更（除缓存外额外回调）。

        callback 仍按旧约定 `(data_id, group, content)`；SDK 的
        `(tenant, group, data_id, content)` 回调在此适配。
        """
        if not self._started or self._client is None:
            return
        g = group or app_config.NACOS_CONFIG_GROUP

        async def _adapted(tenant: str, grp: str, did: str, content: str) -> None:
            callback(did, grp, content)

        await self._client.add_listener(data_id, g, _adapted)


def get_config_center_loader() -> ConfigCenterLoader:
    """懒加载单例。"""
    global _loader
    if _loader is None:
        _loader = ConfigCenterLoader()
    return _loader

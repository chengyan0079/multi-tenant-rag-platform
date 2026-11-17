"""NacosDiscoverManager（stage 3）：服务注册/注销（仅发现端，Nacos 3.x + sdk 2.0+）。心跳由 SDK gRPC 自动维持；1.x 的 v1 注册接口在 3.x 已移除故不可用。放最后确保服务已监听再对外暴露。"""
from app.app_state import app_state
from app.server import Manager
from common.config import config
from common.logger import logger
from common.nacos_compat import import_error_hint, normalize_namespace_id


class NacosDiscoverManager(Manager):
    """放最后：确保服务已真正监听后再对外暴露。"""

    def __init__(self) -> None:
        self._naming = None
        self._registered = False

    @property
    def is_registered(self) -> bool:
        """实例是否已注册成功（供 /admin/health 读取）。"""
        return self._registered

    async def start(self) -> None:
        if not config.NACOS_ENABLED:
            logger.info('Nacos 未启用（NACOS_ENABLED=False），跳过注册')
            return
        # 健康检查据此判断注册状态；未启用时不写入，探活即为 False
        app_state.nacos_discovery = self
        try:
            from v2.nacos import ClientConfig, NacosNamingService, RegisterInstanceParam
        except ImportError:
            logger.warning(import_error_hint('Nacos 服务发现'))
            return

        cc = ClientConfig(
            server_addresses=config.NACOS_SERVER_ADDR,
            namespace_id=normalize_namespace_id(config.NACOS_NAMESPACE),
        )
        self._naming = await NacosNamingService.create_naming_service(cc)
        try:
            ok = await self._naming.register_instance(RegisterInstanceParam(
                service_name=config.SERVICE_NAME,
                ip=config.SERVICE_IP,
                port=config.PORT,
                group_name=config.NACOS_GROUP,
                ephemeral=True,
            ))
            if not ok:
                raise RuntimeError('register_instance 返回 False')
        except Exception as e:  # noqa: BLE001  注册是可旁路能力，默认不阻断启动
            if config.NACOS_DISCOVER_STRICT:
                raise
            logger.warning(
                f'Nacos 服务注册失败（{e}），跳过注册：服务继续启动，'
                f'但网关无法发现本实例（需阻断启动请设 NACOS_DISCOVER_STRICT=True）')
            await self._safe_shutdown()
            return
        self._registered = True
        logger.info(f'Nacos 注册成功: {config.SERVICE_NAME}@{config.SERVICE_IP}:{config.PORT}')

    async def stop(self) -> None:
        self._registered = False
        app_state.nacos_discovery = None
        if self._naming is None:
            return
        try:
            from v2.nacos import DeregisterInstanceParam
            await self._naming.deregister_instance(DeregisterInstanceParam(
                service_name=config.SERVICE_NAME, ip=config.SERVICE_IP,
                port=config.PORT, group_name=config.NACOS_GROUP, ephemeral=True,
            ))
        except Exception:  # noqa: BLE001
            logger.warning('Nacos 注销失败（临时实例会随心跳超时自动摘除）')
        await self._safe_shutdown()

    async def _safe_shutdown(self) -> None:
        """关闭 gRPC 连接（异常不影响停机流程）。"""
        if self._naming is None:
            return
        try:
            await self._naming.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self._naming = None

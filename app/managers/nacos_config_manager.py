"""NacosConfigManager（order=105）：配置中心启动。拉起 ConfigCenterLoader（gRPC 订阅变更）+ 预热 PROMPT 组到 Redis；旁路能力失败只告警不阻断启动。"""
import asyncio

from app.app_state import app_state
from app.server import Manager
from common.config import config as app_config
from common.logger import logger
from core.config_center.loader import get_config_center_loader
from core.config_center.prompts_cache import warmup_prompts


class NacosConfigManager(Manager):
    async def start(self) -> None:
        loader = get_config_center_loader()
        app_state.config_center = loader
        # 长连接订阅变更；未启用 / SDK 缺失时 loader.start 内部自行跳过
        try:
            await loader.start()
        except Exception as e:  # noqa: BLE001  配置中心不阻断启动
            logger.warning(f'配置中心热加载启动失败，运行时将直连 Nacos 兜底: {e}')

        if not app_config.NACOS_CONFIG_ENABLED:
            logger.info('配置中心未启用（NACOS_CONFIG_ENABLED=False），跳过提示词预热')
            return
        # 预热是同步的 Nacos/Redis 调用，丢到线程池避免阻塞启动事件循环
        try:
            await asyncio.to_thread(warmup_prompts)
        except Exception as e:  # noqa: BLE001
            logger.warning(f'提示词预热失败，运行时将直连 Nacos 兜底: {e}')

    async def stop(self) -> None:
        # Redis 缓存跨重启保留，无需主动清理；此处只关配置中心长连接
        try:
            await get_config_center_loader().stop()
        except Exception as e:  # noqa: BLE001
            logger.warning(f'配置中心热加载停止异常: {e}')
        app_state.config_center = None

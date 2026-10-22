"""FileProviderManager（order=60）：构建文件提供者（OSS→MinIO→Mock 回退链）。"""
from app.server import Manager, StartupError
from app.app_state import app_state
from common.logger import logger
from core.file_provider.provider import build_file_provider


class FileProviderManager(Manager):
    async def start(self) -> None:
        fp = build_file_provider()
        # 生产：bucket 必须已存在且存储可达；缺失/不可达则启动失败（不自动建桶）
        if not await fp.healthcheck():
            raise StartupError(
                'FileProvider 健康检查未通过（bucket 不存在或存储不可达），请先预创建存储桶'
            )
        app_state.file_provider = fp
        logger.info(f'FileProvider 就绪: {type(fp).__name__}')

    async def stop(self) -> None:
        pass

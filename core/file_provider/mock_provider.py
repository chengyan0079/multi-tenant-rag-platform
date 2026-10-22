"""
mock_provider - Mock 文件提供者（本地临时区，作为无对象存储环境兜底）

对象路径与租户前缀由 core.file_provider.provider 统一构造（_build_object_key / _tenant_prefix），
与 MinIO 等实现一致，确保多服务通用时路径格式统一。
"""
import base64
import os
from datetime import datetime, timedelta
from typing import List

from common.config import config
from common.logger import logger
from core.file_provider.provider import (
    FileProvider, FileRef, _build_object_key, _tenant_prefix,
)


class MockFileProvider(FileProvider):
    def __init__(self) -> None:
        self.root = config.UPLOAD_DIR
        os.makedirs(self.root, exist_ok=True)

    def _resolve(self, oss_key: str) -> str:
        # 安全：剥离前导斜杠与 '..' 段，避免路径穿越写到 root 之外
        safe = oss_key.lstrip('/').replace('..', '')
        return os.path.join(self.root, safe)

    async def get_file(self, oss_key: str, with_base64: bool = False, tenant_id: int | None = None) -> FileRef:
        key = oss_key.lstrip('/')
        # 租户隔离：下载须归属当前租户，防止跨租户取文件
        if tenant_id is not None and not key.startswith(_tenant_prefix(tenant_id)):
            raise FileNotFoundError(f'文件不属于该租户: {oss_key}')
        path = self._resolve(oss_key)
        if not os.path.exists(path):
            raise FileNotFoundError(f'文件不存在: {oss_key}')
        file_size = os.path.getsize(path)
        file_name = os.path.basename(oss_key)
        file_type = os.path.splitext(oss_key)[1].lstrip('.')
        b64 = None
        # base64 直出仅用于小文件，避免大文件占满内存与响应体
        if with_base64 and file_size <= config.BASE64_MAX_BYTES:
            with open(path, 'rb') as f:
                b64 = base64.b64encode(f.read()).decode()
        return FileRef(
            oss_key=oss_key,
            url=f'/admin/files/{oss_key}',
            file_name=file_name,
            file_type=file_type,
            file_size=file_size,
            base64=b64,
        )

    async def get_files(self, oss_keys: List[str], with_base64: bool = False,
                        tenant_id: int | None = None) -> List[FileRef]:
        # 批量取：逐个带租户归属校验；单个缺失不中断整体，记日志后跳过
        result = []
        for k in oss_keys:
            try:
                result.append(await self.get_file(k, with_base64, tenant_id))
            except FileNotFoundError:
                logger.warning(f'批量取文件缺失: {k}')
        return result

    async def get_bytes(self, oss_key: str, tenant_id: int | None = None) -> bytes:
        # 下载：原始字节（不做 base64）；带租户归属校验，不存在抛 FileNotFoundError
        key = oss_key.lstrip('/')
        if tenant_id is not None and not key.startswith(_tenant_prefix(tenant_id)):
            raise FileNotFoundError(f'文件不属于该租户: {oss_key}')
        path = self._resolve(oss_key)
        if not os.path.exists(path):
            raise FileNotFoundError(f'文件不存在: {oss_key}')
        with open(path, 'rb') as f:
            return f.read()

    async def put_file(self, tenant_id: int, base_key: str, data: bytes, ext: str, version: int) -> FileRef:
        # 路径规则由 provider 共享函数统一构造（含版本，避免同文档覆盖）
        oss_key = _build_object_key(tenant_id, base_key, ext, version)
        path = self._resolve(oss_key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(data)
        expires_at = (datetime.now() + timedelta(hours=config.FILE_TTL_HOURS)).isoformat()
        return FileRef(
            oss_key=oss_key,
            url=f'/admin/files/{oss_key}',
            file_name=os.path.basename(oss_key),
            file_type=ext.lstrip('.'),
            file_size=len(data),
            version=version,
            expires_at=expires_at,
        )

    async def healthcheck(self) -> bool:
        try:
            os.makedirs(self.root, exist_ok=True)
            return os.path.isdir(self.root)
        except Exception:
            return False

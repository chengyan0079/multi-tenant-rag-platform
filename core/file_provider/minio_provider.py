"""
minio_provider - MinIO 文件提供者

对接 config.MINIO_*：endpoint / bucket / access_key / secret_key。
minio 官方 SDK 为同步阻塞，所有网络 IO 用 asyncio.to_thread 包一层，避免阻塞事件循环。
对象路径与租户前缀由 core.file_provider.provider 统一构造（_build_object_key / _tenant_prefix），
与 Mock 等实现保持一致，便于多服务通用。
接口层 url 统一走本服务 /admin/files/{oss_key} 中转。
"""
import asyncio
import base64
import os
from datetime import datetime, timedelta
from io import BytesIO
from typing import List

from minio import Minio
from minio.error import S3Error

from common.config import config
from common.logger import logger
from core.file_provider.provider import (
    FileProvider, FileRef, _build_object_key, _tenant_prefix,
)


class MinioFileProvider(FileProvider):
    def __init__(self) -> None:
        # endpoint 形如 http://localhost:9000，拆出 host:port 与 secure
        raw = config.MINIO_ENDPOINT
        self.secure = raw.startswith('https')
        netloc = raw.split('://')[-1]
        host, _, port = netloc.partition(':')
        port = int(port) if port else 9000
        self.bucket = config.MINIO_BUCKET
        # 凭据缺省回退到 MinIO 容器默认账号，便于 docker 环境开箱即用
        ak = config.MINIO_ACCESS_KEY or 'minioadmin'
        sk = config.MINIO_SECRET_KEY or 'minioadmin'
        # 构造 client 本身不发起网络请求，连接延迟到首操作时发生
        self.client = Minio(f'{host}:{port}', access_key=ak, secret_key=sk, secure=self.secure)

    def _check_bucket(self) -> None:
        # 生产：bucket 必须已存在（由部署/初始化预创建），缺失即抛错，不自动建桶
        if not self.client.bucket_exists(self.bucket):
            raise RuntimeError(f'MinIO bucket [{self.bucket}] 不存在，请先创建')

    def _read_all(self, oss_key: str) -> bytes:
        resp = self.client.get_object(self.bucket, oss_key)
        try:
            return resp.read()
        finally:
            resp.close()
            resp.release()

    async def get_file(self, oss_key: str, with_base64: bool = False, tenant_id: int | None = None) -> FileRef:
        key = oss_key.lstrip('/')
        # 租户隔离：下载须归属当前租户，防止跨租户取文件
        if tenant_id is not None and not key.startswith(_tenant_prefix(tenant_id)):
            raise FileNotFoundError(f'文件不属于该租户: {oss_key}')
        try:
            info = await asyncio.to_thread(self.client.stat_object, self.bucket, key)
        except S3Error:
            raise FileNotFoundError(f'文件不存在: {oss_key}')
        file_name = key.rstrip('/').split('/')[-1]
        file_type = os.path.splitext(file_name)[1].lstrip('.')
        b64 = None
        # base64 直出仅用于小文件，避免大文件占满内存与响应体
        if with_base64 and info.size <= config.BASE64_MAX_BYTES:
            b64 = base64.b64encode(await asyncio.to_thread(self._read_all, key)).decode()
        return FileRef(
            oss_key=oss_key,
            url=f'/admin/files/{oss_key}',
            file_name=file_name,
            file_type=file_type,
            file_size=info.size,
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
        # 下载：原始字节流（不做 base64）；带租户归属校验，不存在抛 FileNotFoundError
        key = oss_key.lstrip('/')
        if tenant_id is not None and not key.startswith(_tenant_prefix(tenant_id)):
            raise FileNotFoundError(f'文件不属于该租户: {oss_key}')
        try:
            return await asyncio.to_thread(self._read_all, key)
        except S3Error:
            raise FileNotFoundError(f'文件不存在: {oss_key}')

    async def put_file(self, tenant_id: int, base_key: str, data: bytes, ext: str, version: int) -> FileRef:
        await asyncio.to_thread(self._check_bucket)
        # 路径规则由 provider 共享函数统一构造（含版本，避免同文档覆盖），保证多存储后端一致
        oss_key = _build_object_key(tenant_id, base_key, ext, version)
        length = len(data)
        # put_object 需要可 Seek 的数据源与长度；用 BytesIO 包装 bytes
        await asyncio.to_thread(
            self.client.put_object, self.bucket, oss_key, BytesIO(data), length
        )
        expires_at = (datetime.now() + timedelta(hours=config.FILE_TTL_HOURS)).isoformat()
        return FileRef(
            oss_key=oss_key,
            url=f'/admin/files/{oss_key}',
            file_name=os.path.basename(oss_key),
            file_type=ext.lstrip('.'),
            file_size=length,
            version=version,
            expires_at=expires_at,
        )

    async def healthcheck(self) -> bool:
        try:
            await asyncio.to_thread(self._check_bucket)
            return True
        except Exception as e:
            logger.warning(f'MinIO healthcheck 失败: {e}')
            return False

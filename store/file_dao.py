"""文件 / 文档 DAO（store 数据访问层）：rag_file + rag_file_version 的 MySQL 操作 + 对象存储读写。

封装上传幂等编排（get-or-create 文档 → 内容去重 → 分配版本 → 存对象 → 写版本 → 更新当前指针）。
返回 ORM 实体 / provider 的 FileRef / 简单 DTO（UploadOutcome）；实体 → 响应模型的映射在路由层完成，
故本模块不依赖 app.api.schemas，保持 store 层纯净。
"""
import datetime
import hashlib
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.app_state import app_state
from store.mysql_store import get_sessionmaker
from store.orm_models.file import File, FileVersion


@dataclass
class UploadOutcome:
    """一次上传的结果（路由据此组装单发 / 批量响应）。"""
    file_id: int
    version: int
    oss_key: str
    expires_at: str | None
    dedup: bool


class FileDao:
    def __init__(self, factory=None) -> None:
        self._factory = factory or get_sessionmaker()

    async def save_upload(self, tenant_id: int, base_key: str, data: bytes,
                          ext: str, upload_user_id: str) -> UploadOutcome:
        """幂等保存一次上传：get-or-create 文档 → 内容去重 → 分配版本(重试) → 存对象 → 写版本 → 更新当前指针。"""
        file_hash = hashlib.md5(data).hexdigest()
        provider = app_state.file_provider
        async with self._factory() as session:
            # 1) get-or-create 文档（同租户同 base_key 唯一）
            f = (await session.execute(
                select(File).where(File.tenant_id == tenant_id, File.base_key == base_key)
            )).scalar_one_or_none()
            if f is None:
                f = File(tenant_id=tenant_id, base_key=base_key, create_user_id=upload_user_id,
                         current_version=0, version_count=0, status=1)
                session.add(f)
                await session.flush()

            # 2) 内容幂等：同文档同内容直接返回既有版本，不新增
            dup = (await session.execute(
                select(FileVersion).where(FileVersion.file_id == f.id, FileVersion.file_hash == file_hash)
            )).scalar_one_or_none()
            if dup is not None:
                await session.commit()
                return UploadOutcome(f.id, dup.version, dup.oss_key,
                                     dup.expires_at.isoformat() if dup.expires_at else None, True)

            # 3) 分配版本号 + 存对象 + 写版本（撞唯一约束重试）
            last_exc = None
            for _ in range(3):
                max_v = (await session.execute(
                    select(func.max(FileVersion.version)).where(FileVersion.file_id == f.id)
                )).scalar()
                version = (max_v or 0) + 1
                ref = await provider.put_file(tenant_id, base_key, data, ext, version)
                row = FileVersion(
                    tenant_id=tenant_id, file_id=f.id, base_key=base_key, version=version,
                    oss_key=ref.oss_key, source_path=ref.oss_key,
                    file_name=ref.file_name, file_type=ref.file_type, file_size=ref.file_size,
                    file_hash=file_hash, upload_user_id=upload_user_id, status=1,
                    expires_at=datetime.datetime.fromisoformat(ref.expires_at) if ref.expires_at else None,
                )
                session.add(row)
                try:
                    await session.flush()
                except IntegrityError as e:
                    await session.rollback()
                    last_exc = e
                    # 并发：可能版本号冲突或同内容已被他人插入 → 重查文档与去重
                    f = (await session.execute(
                        select(File).where(File.tenant_id == tenant_id, File.base_key == base_key)
                    )).scalar_one()
                    dup = (await session.execute(
                        select(FileVersion).where(FileVersion.file_id == f.id, FileVersion.file_hash == file_hash)
                    )).scalar_one_or_none()
                    if dup is not None:
                        await session.commit()
                        return UploadOutcome(f.id, dup.version, dup.oss_key,
                                             dup.expires_at.isoformat() if dup.expires_at else None, True)
                    continue

                # 4) 更新文档当前指针（冗余快照，供列表直查）
                f.current_version = version
                f.current_oss_key = ref.oss_key
                f.current_file_size = ref.file_size
                f.current_file_hash = file_hash
                f.file_name = ref.file_name
                f.file_type = ref.file_type
                f.version_count = version
                await session.commit()
                exp = row.expires_at.isoformat() if row.expires_at else None
                return UploadOutcome(f.id, version, ref.oss_key, exp, False)

            raise last_exc or RuntimeError('版本分配失败')

    async def list_documents(self, tenant_id: int) -> list[File]:
        """文档列表实体（一个 base_key 一行），按更新时间倒序。"""
        async with self._factory() as session:
            return (await session.execute(
                select(File).where(File.tenant_id == tenant_id, File.status == 1)
                .order_by(File.updated_at.desc())
            )).scalars().all()

    async def list_versions(self, tenant_id: int, base_key: str) -> list[FileVersion]:
        """某文档的版本历史实体，按版本倒序。"""
        async with self._factory() as session:
            return (await session.execute(
                select(FileVersion).where(
                    FileVersion.tenant_id == tenant_id,
                    FileVersion.base_key == base_key,
                    FileVersion.status == 1,
                ).order_by(FileVersion.version.desc())
            )).scalars().all()

    async def get_bytes(self, oss_key: str, tenant_id: int) -> bytes:
        """下载：返回原始字节（带租户归属校验）；不存在抛 FileNotFoundError。"""
        return await app_state.file_provider.get_bytes(oss_key, tenant_id=tenant_id)

    async def batch_get_files(self, oss_keys: list[str], base64: bool, tenant_id: int) -> list:
        """批量取文件（逐个带租户归属校验），返回 provider 的 FileRef 列表（含 base64 内容）。"""
        return await app_state.file_provider.get_files(
            oss_keys, with_base64=base64, tenant_id=tenant_id)

    async def delete_document(self, tenant_id: int, base_key: str) -> bool:
        """软删除文档（status=0，幂等，版本历史保留）；不存在返回 False。"""
        async with self._factory() as session:
            f = (await session.execute(
                select(File).where(File.tenant_id == tenant_id, File.base_key == base_key)
            )).scalar_one_or_none()
            if f is None:
                return False
            f.status = 0
            await session.commit()
            return True

"""
provider - FileProvider 抽象与 FileRef 数据类（§4）

统一返回"可下载/可上传 URL + 元信息（+ 可选 base64 内容）"。
put_file 必须传入 tenant_id，oss_key 以 t{tenant_id}/ 前缀实现租户隔离；
get_file / get_files / get_bytes 均可传 tenant_id 做归属校验，防止跨租户下载。
下载用 get_bytes（原始字节流，勿用 base64）；base64 仅用于在 JSON 内承载字节（批量下载 / 小文件内联预览）。

多服务通用：路由层只依赖 FileProvider 抽象，具体落地（MinIO / Mock / 未来 OSS）
由 build_file_provider 按配置选择。对象路径规则与租户前缀集中在模块级函数
（_safe_key / _build_object_key / _tenant_prefix）统一管理，各存储实现共用，
保证不同后端下路径格式一致。
"""
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List

from common.config import config


@dataclass
class FileRef:
    oss_key: str
    url: str
    file_name: str
    file_type: str
    file_size: int = 0
    version: int = 1
    expires_at: str | None = None
    base64: str | None = None   # 可选：文件内容 base64（小文件直出）


def _safe_key(seg: str) -> str:
    # 仅保留小写字母数字与 _-，其余替换为下划线，避免非法对象名或路径穿越
    seg = (seg or 'default').strip().lower()
    seg = re.sub(r'[^a-z0-9_-]+', '_', seg)
    return seg[:64] or 'default'


def _safe_filename(name: str) -> str:
    # 保留原文件名可读性（含中文/字母/数字/点/空格/下划线/连字符），仅去掉路径分隔符与控制字符
    name = (name or 'file').strip()
    name = re.sub(r'[\x00-\x1f\\/]', '_', name)
    return name or 'file'


def _tenant_prefix(tenant_id: int) -> str:
    # 租户隔离前缀，所有存储后端共用，下载校验与路径构造都依赖它；模板来自配置
    return config.FILE_TENANT_PREFIX_TMPL.format(tenant_id=tenant_id)


def _build_object_key(tenant_id: int, base_key: str, ext: str, version: int) -> str:
    # 统一对象路径：{root}/t{租户}/{文档}/v{版本}/{文件名}.{ext}
    # 版本入路径，保证同文档多版本对象互不覆盖；规则来自配置，各存储后端共用。
    segs: list = []
    if config.FILE_OBJECT_ROOT:
        segs.append(config.FILE_OBJECT_ROOT.strip('/'))
    segs.append(_tenant_prefix(tenant_id).rstrip('/'))
    segs.append(_safe_key(base_key))                       # 文档目录（防路径穿越）
    segs.append(f'v{version}')                             # 版本目录
    segs.append(_safe_filename(f'{base_key}.{ext.lstrip(".")}'))
    return '/'.join(segs)


class FileProvider(ABC):
    @abstractmethod
    async def get_file(self, oss_key: str, with_base64: bool = False,
                       tenant_id: int | None = None) -> FileRef:
        """取文件元信息（可选 base64 内容，仅小文件）；带租户归属校验。"""

    @abstractmethod
    async def get_files(self, oss_keys: List[str], with_base64: bool = False,
                        tenant_id: int | None = None) -> List[FileRef]:
        """批量取文件元信息（带租户归属校验）；单个缺失由实现方跳过。"""

    @abstractmethod
    async def get_bytes(self, oss_key: str, tenant_id: int | None = None) -> bytes:
        """下载：返回原始字节（带租户归属校验）；不存在抛 FileNotFoundError。"""

    @abstractmethod
    async def put_file(self, tenant_id: int, base_key: str, data: bytes,
                       ext: str, version: int) -> FileRef:
        """上传：写入对象并返回 FileRef（含版本化 oss_key 与真实版本号）。"""

    @abstractmethod
    async def healthcheck(self) -> bool:
        """存储连通性探活。"""


def build_file_provider() -> FileProvider:
    """按配置选择文件存储实现（路由层只依赖 FileProvider 抽象，无需改动）。

    当前支持 MinIO 与 Mock 两种；新增对象存储（如 OSS）时，实现 FileProvider
    抽象并在此注册即可，接口与业务逻辑不受影响。默认走 MinIO。
    """
    from common.config import config
    from core.file_provider.minio_provider import MinioFileProvider
    from core.file_provider.mock_provider import MockFileProvider

    if config.MINIO_ENABLED:
        return MinioFileProvider()
    return MockFileProvider()

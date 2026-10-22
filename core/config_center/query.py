"""
query - 配置中心只读查询封装（按 group 维度）

业务侧 / 控制台统一从这里取配置，底层都是调用 Nacos Open API（core.config_center.nacos_config）。

设计约定：配置按 group 组织（如 SYSTEM=系统级、PROMPT=提示词），但本模块只做
**通用、group 参数化**的抽象——group 只是入参，代码中不按 group 特判，所有 group 走同一套逻辑。
group 建议传 `core.config_center.groups.ConfigGroup` 枚举成员（str 枚举，底层即字符串）。
"""
import json
from typing import Any

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore
    _HAS_YAML = False

from core.config_center.nacos_config import ConfigCenterClient, ConfigCenterError, get_config_center_client


def _parse(data_id: str, raw: str) -> Any:
    """按 data_id 后缀兼容解析：yaml/yml 走 YAML，其余走 JSON；失败返回 None。"""
    ext = data_id.rsplit('.', 1)[-1].lower() if '.' in data_id else ''
    if ext in ('yaml', 'yml') and _HAS_YAML:
        try:
            return yaml.safe_load(raw)  # type: ignore[union-attr]
        except yaml.YAMLError:  # type: ignore[union-attr]
            return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def list_by_group(
    group: str | None,
    data_id: str | None = None,
    page_no: int = 1,
    page_size: int = 100,
    client: ConfigCenterClient | None = None,
) -> dict:
    """取某 group 下的配置列表（可按 data_id 进一步过滤）。底层 Nacos /nacos/v1/cs/configs。"""
    client = client or get_config_center_client()
    return client.list_configs(data_id=data_id, group=group, page_no=page_no, page_size=page_size)


def get_detail(
    group: str | None,
    data_id: str,
    client: ConfigCenterClient | None = None,
) -> str | None:
    """取某 group + data_id 的配置原始内容。底层 Nacos /nacos/v1/cs/configs。"""
    client = client or get_config_center_client()
    return client.get_config(data_id, group)


def get_detail_json(
    group: str | None,
    data_id: str,
    client: ConfigCenterClient | None = None,
) -> Any:
    """取配置内容并按 data_id 后缀兼容解析为 dict（json/yaml）；不存在或解析失败返回 None。"""
    raw = get_detail(group, data_id, client)
    if raw is None:
        return None
    return _parse(data_id, raw)


__all__ = ['list_by_group', 'get_detail', 'get_detail_json', 'ConfigCenterError']

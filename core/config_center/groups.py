"""配置中心 group 枚举。

配置按 group 维度分桶（系统级 / 提示词 / ...）。新增分组时只需在此追加枚举成员，
业务侧统一引用枚举（而非散落硬编码字符串）；底层定位配置仍是 (namespace, group, data_id)。
"""
from enum import Enum


class ConfigGroup(str, Enum):
    """Nacos 配置中心 group 枚举（str 枚举，成员值即配置中心里的 group 名）。"""

    SYSTEM = "SYSTEM"   # 系统级配置（全局开关、运行参数等）
    PROMPT = "PROMPT"   # 提示词专用

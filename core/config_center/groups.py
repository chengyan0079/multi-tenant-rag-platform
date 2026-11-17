"""配置中心 group 枚举（系统级 / 提示词等）。新增分组只追加枚举成员，业务侧统一引用，避免硬编码字符串。"""
from enum import Enum


class ConfigGroup(str, Enum):
    """Nacos 配置中心 group 枚举（str 枚举，成员值即配置中心里的 group 名）。"""

    SYSTEM = "SYSTEM"   # 系统级配置（全局开关、运行参数等）
    PROMPT = "PROMPT"   # 提示词专用

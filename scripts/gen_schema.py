"""从 store/orm_models 生成 MySQL 建表脚本 scripts/schema.sql（保证与 ORM 一致）。

用法：python scripts/gen_schema.py
模型变更后重新运行本脚本即可同步 schema.sql（schema.sql 由本脚本生成，请勿手改）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

import store.orm_models  # noqa: F401  触发所有模型注册到 Base.metadata
from store.orm_models import Base

_DIALECT = mysql.dialect()

# (分区标题, [(表名, 表注释), ...]) —— 同时决定输出顺序
SECTIONS = [
    ("1) 租户级业务表（均带 tenant_id 做租户隔离）", [
        ("rag_document", "文档解析/索引记录（点击解析时创建，经 file_upload_id 引用上传记录，不重复文件元数据）"),
        ("rag_chunk", "切片（文档/FAQ 切分后的最小检索单元；scope 区分 doc/faq）"),
        ("rag_index_task", "索引任务（文档解析/嵌入入库的异步任务）"),
        ("rag_faq", "每租户高频问题表（MySQL+Redis 字面匹配；question_type 为意图训练数据源）"),
        ("rag_file", "文档/文件列表（一个 base_key 一行，冗余当前版本；版本历史见 rag_file_version）"),
        ("rag_file_version", "文件版本历史（一个版本一行；同文档同内容唯一，幂等去重）"),
    ]),
    ("2) 用户级表（仅 user_id，无 tenant_id）", [
        ("rag_session", "会话注册表（列表/元数据持久化；对话原文存 Redis）"),
    ]),
    ("3) 系统级表（无 tenant_id；配置审批流，复用 Nacos）", [
        ("rag_config_change_request", "配置变更申请"),
        ("rag_config_review", "配置复核记录"),
    ]),
    ("4) 系统级日志表（无 tenant_id；API 调用审计）", [
        ("rag_api_call_log", "API 调用日志（一条请求一行：请求侧 + 响应侧 + 耗时）"),
    ]),
]

HEADER = """-- ============================================================
-- RAG-Ultra 问答系统 元数据表（MySQL 8）
-- 本文件由 scripts/gen_schema.py 依据 store/orm_models 自动生成，请勿手改；
-- 模型变更后运行 `python scripts/gen_schema.py` 重新生成本文件。
--
-- 租户 / 身份模型：
--   - 租户身份由网关 OAuth 校验后分配（X-Tenant-Id 下发），本服务不维护租户表；
--   - 三级隔离：租户级(带 tenant_id) / 用户级 rag_session(仅 user_id) / 系统级配置审批表；
--   - 文件上传记录归 rag_file_upload（文件元数据 + 路径 + 版本，控制台文档/版本列表用它）；
--     rag_document 为“解析/索引”记录，经 file_upload_id 引用上传版本，不重复文件元数据。
-- 说明：列默认值由 ORM 在写入时提供（Python 侧 default），故 DDL 不含服务器端 DEFAULT。
-- ============================================================

CREATE DATABASE IF NOT EXISTS rag_ultra CHARACTER SET utf8mb4;
USE rag_ultra;
"""


def _create_table_sql(table, comment: str) -> str:
    raw = str(CreateTable(table).compile(dialect=_DIALECT)).replace("\t", "  ")
    ddl = "\n".join(line.rstrip() for line in raw.splitlines()).strip()
    if not ddl.endswith(")"):
        raise RuntimeError(f"unexpected DDL for {table.name}")
    ddl = ddl[:-1].rstrip()
    return f"{ddl}\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='{comment}';"


def _index_sql(table) -> list[str]:
    stmts = [str(CreateIndex(i).compile(dialect=_DIALECT)).strip() + ";" for i in table.indexes]
    return sorted(stmts)


def build_sql() -> str:
    tables = {t.name: t for t in Base.metadata.tables.values()}
    parts = [HEADER]
    for title, items in SECTIONS:
        parts += ["", "-- " + "=" * 58, f"-- {title}", "-- " + "=" * 58, ""]
        for name, comment in items:
            table = tables[name]
            parts.append(_create_table_sql(table, comment))
            parts += _index_sql(table)
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    content = build_sql()
    target = ROOT / "scripts" / "schema.sql"
    target.write_text(content, encoding="utf-8")
    print(f"generated {target} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

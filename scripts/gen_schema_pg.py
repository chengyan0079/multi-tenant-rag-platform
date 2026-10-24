"""从 store/orm_models 生成 PostgreSQL 建表脚本 scripts/schema_postgresql.sql（保证与 ORM 一致）。

用法：python scripts/gen_schema_pg.py
模型变更后重新运行本脚本即可同步（该文件由脚本生成，请勿手改）。

与 MySQL 版（gen_schema.py）的差异来自 PG 的语法限制：
  - PG 没有行内 COMMENT 语法，表/列注释改用 COMMENT ON 语句；
  - PG 没有 MEDIUMTEXT，映射为 TEXT（PG 的 TEXT 本身无长度上限）；
  - 自增主键由 BIGINT AUTO_INCREMENT 变为 BIGSERIAL。
分区与表顺序复用 gen_schema.py 的 SECTIONS，避免两份脚本漂移。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import Table
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.schema import CreateIndex, CreateTable

import store.orm_models  # noqa: F401  触发所有模型注册到 Base.metadata
from store.orm_models import Base

from gen_schema import SECTIONS

_DIALECT = postgresql.dialect()


@compiles(MEDIUMTEXT, 'postgresql')
def _compile_mediumtext(type_, compiler, **kw):
    """PG 没有 MEDIUMTEXT，落到 TEXT。"""
    return 'TEXT'


HEADER = """-- ============================================================
-- RAG-Ultra 问答系统 元数据表（PostgreSQL 14+）
-- 本文件由 scripts/gen_schema_pg.py 依据 store/orm_models 自动生成，请勿手改；
-- 模型变更后运行 `python scripts/gen_schema_pg.py` 重新生成本文件。
--
-- 租户 / 身份模型：
--   - 租户身份由网关 OAuth 校验后分配（X-Tenant-Id 下发），本服务不维护租户表；
--   - 三级隔离：租户级(带 tenant_id) / 用户级 rag_session(仅 user_id) / 系统级配置审批表；
--   - 文件上传记录归 rag_file_upload（文件元数据 + 路径 + 版本，控制台文档/版本列表用它）；
--     rag_document 为“解析/索引”记录，经 file_upload_id 引用上传版本，不重复文件元数据。
-- 说明：列默认值由 ORM 在写入时提供（Python 侧 default），故 DDL 不含服务器端 DEFAULT。
--
-- 与 MySQL 版（schema.sql）的语法差异：
--   - 注释用 COMMENT ON（PG 无行内 COMMENT 语法）；
--   - MEDIUMTEXT -> TEXT；
--   - 自增主键 BIGINT AUTO_INCREMENT -> BIGSERIAL；
--   - JSON 列编译为 JSON；若需要 JSONB（支持索引/GIN），可自行替换类型。
-- ============================================================

-- 建库（库名与 .env 的 MYSQL_DATABASE 对齐即可；已有库或库名不同则跳过本句）
CREATE DATABASE rag_ultra ENCODING 'UTF8';

-- psql 下切到该库后执行建表：\\connect rag_ultra
"""


def _esc(text: str) -> str:
    """注释里的单引号要转义，否则 COMMENT ON 语句会被截断。"""
    return text.replace("'", "''")


def _create_table_sql(table: Table) -> str:
    raw = str(CreateTable(table).compile(dialect=_DIALECT)).replace("\t", "  ")
    ddl = "\n".join(line.rstrip() for line in raw.splitlines()).strip()
    if not ddl.endswith(")"):
        raise RuntimeError(f"unexpected DDL for {table.name}")
    return ddl + ";"


def _index_sql(table: Table) -> list[str]:
    return sorted(str(CreateIndex(i).compile(dialect=_DIALECT)).strip() + ";" for i in table.indexes)


def _comment_sql(table: Table, comment: str) -> list[str]:
    """PG 的注释只能走 COMMENT ON，表与列分开写。"""
    stmts = [f"COMMENT ON TABLE {table.name} IS '{_esc(comment)}';"]
    for col in table.columns:
        if col.comment:
            stmts.append(f"COMMENT ON COLUMN {table.name}.{col.name} IS '{_esc(col.comment)}';")
    return stmts


def build_sql() -> str:
    tables = {t.name: t for t in Base.metadata.tables.values()}
    parts = [HEADER]
    for title, items in SECTIONS:
        parts += ["", "-- " + "=" * 58, f"-- {title}", "-- " + "=" * 58, ""]
        for name, comment in items:
            table = tables[name]
            parts.append(_create_table_sql(table))
            parts += _comment_sql(table, comment)
            parts += _index_sql(table)
            parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def main() -> int:
    content = build_sql()
    target = ROOT / "scripts" / "schema_postgresql.sql"
    target.write_text(content, encoding="utf-8")
    print(f"generated {target} ({len(content.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

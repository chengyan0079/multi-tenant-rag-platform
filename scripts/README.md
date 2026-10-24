# scripts/ —— 项目脚本目录

放置面向**运维 / 部署 / 初始化**的脚本入口（区别于 `offline/` 的线下训练评估代码）。

| 文件 | 用途 |
|---|---|
| `gen_schema_pg.py` | 依据 `store/orm_models` 生成 `schema_postgresql.sql`（PostgreSQL 建表脚本，模型变更后重跑以同步） |
| `gen_auth_headers.py` | 生成调用方签名头（自测/联调用，扮演网关角色；见 `core/auth`） |
| `schema_postgresql.sql` | PostgreSQL 建表基准（由 `gen_schema_pg.py` 自动生成，请勿手改） |

> Milvus 的数据库与向量集合由**服务启动时自动创建**（`app/managers/milvus_manager.py`：缺失则建、有则加载，幂等），无需单独的初始化脚本。

## 使用

```bash
# 建表（库名与 .env 的 POSTGRES_DATABASE 对齐）
createdb -h127.0.0.1 -U postgres rag_ultra
psql -h127.0.0.1 -U postgres -d rag_ultra -f scripts/schema_postgresql.sql

# 改了 ORM 模型后，重新生成建表脚本
python scripts/gen_schema_pg.py

# 生成调用方签名头（自测：开启 AUTH_HMAC_ENABLED 后用这些头请求接口）
# 方式一：直接运行，改文件顶部 DEMO_* 常量（IDE 右键 Run）
python scripts/gen_auth_headers.py

# 方式二：命令行传参（优先级高于 DEMO_*）
python scripts/gen_auth_headers.py --method GET --path /v1/faq --tenant 100 --user 1001
```

完整部署流程（中间件、配置、Docker、优雅关闭）见 `docs/部署说明.md`。

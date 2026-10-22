# 智能体平台 · 问答系统（FAQ + RAG）架构设计文档

> 版本：v5.1 ｜ 日期：2026-10-17 ｜ 作者：CTO 助手
> 范围：智能体平台中的 **问答系统**，由 **FAQ 问答层**（标准问答对优先）与 **RAG 文档检索层**（兜底）组成；不含 Agent 编排（LangGraph 等上层），Agent 层仅作为本系统的调用方存在。

---

## 0. 已知约束与设计前提

### 0.1 已确认约束

| 项 | 约束 / 已知信息 |
|---|---|
| 模型 | **bge-m3**（dense 1024 + sparse，一次推理同时产出）、**bge-reranker-v2-m3**（精排）、**google-bert**（可选：兜底 embedding / 多轮 query 改写） |
| 向量库 | **Milvus**（dense + sparse 同集合；sparse 用 **IP 内积** 度量） |
| 元数据 | **MySQL 8** |
| 缓存/队列 | **Redis**（会话 / 答案缓存 / 热点 / 队列 / 限流） |
| 源文件 | 实际在 **OSS**，当前无 OSS 服务；本地有 **MinIO**（可用性待定） |
| 接口 | **FastAPI** + **Swagger**；支持多租户、文档隔离 |
| 生成框架 | **LangChain**（仅生成层 M8） |
| 检索范式 | **两路混合召回**：bge-m3 dense(cosine) + bge-m3 sparse(IP)，RRF 融合；**不单独维护 BM25 倒排** |
| 业务侧基建 | Java 侧已有 **gateway / oauth / nacos**；问答系统**不维护租户（租户由网关经 OAuth 分配，本服务仅消费 X-Tenant-Id）**，暂不同步业务侧；**nacos 双用：服务发现与注册（namespace=public）+ 配置中心（namespace=rag-ultra，经审批流写、业务侧经 loader 热加载读）** |
| 日志 | 全程携带 **traceid**（与网关对齐），为未来日志收集与调用链路追踪准备 |
| 质量要求 | 异步、高性能、生产级；注意并发与多进程，必要时用锁控制 |
| 配置 | 全局配置集中加载（`ConfigManager`），各 Manager 只消费 |

### 0.2 已确认关键决策

| 决策 | 结论 |
|---|---|
| 词法召回 | 移除独立 BM25 倒排，统一用 **bge-m3 sparse + Milvus IP**（学习权重，非原始 TF） |
| FAQ/RAG 路由 | 不引入独立意图识别模型，用 **FAQ 相似度阈值** 做轻量路由（零额外延迟） |
| 生成 | **LangChain** 仅用于 M8 生成层；检索管道自研 |
| LangGraph | **不进系统内部**（确定性单跳，无分支/循环），留待上游 Agent 层边界外用 |
| MCP / A2A | **暂不需要**；系统边界用 REST（`/v1/qa`）暴露，未来如需再由边界适配层接入 |
| 多租户 | 全链路强制 `tenant_id`；Milvus partition key、Redis `t:{tenant_id}:` 前缀 |

---

## 1. 设计目标

- 构建 **问答系统**：**FAQ 优先**（高频标准问题毫秒级、答案可控可审计）+ **RAG 兜底**（长尾/开放问题检索文档生成）。
- 支持多格式文档（PDF/Word/Excel/MD/TXT）入库；源文件在 OSS，当前无 OSS 时 **Mock 文件获取/上传接口**（单/批量），兼容本地 MinIO，生产无缝切 OSS。
- 元数据落 **MySQL 8**，向量（dense+sparse）落 **Milvus**，缓存/队列落 **Redis**；BM25 倒排不再维护。
- 检索：**bge-m3 dense(cosine) + sparse(IP)** 两路混合，RRF 融合，bge-reranker-v2-m3 精排。
- 全链路异步；支持 **多租户 / 文档级隔离**（文档与切片为租户级，无独立知识库）。
- 支持 **多次多轮问答**（会话与历史上下文）。
- 文件上传接口（Mock）：带 **有效期** 与 **版本号**；下载接口支持返回 **base64** 内容。
- 全局配置 **集中加载与校验**（ConfigManager）。
- 服务 **优雅启动/关闭**：`AppServer` 注册多个 `XXManager`，单一函数 `run()`，支持信号优雅停机。
- 提供 **管理控制台页面**：文件、租户、FAQ、文档索引、检索调试。
- 生成层用 **LangChain** 组装"检索结果 + LLM"（底层走平台 LLM 网关）。
- 生产级：可观测、可评测、支持灰度与回滚。

---

## 2. 总体架构（module 范式 + FAQ 优先分层）

```
                        问答系统（FAQ 优先 + RAG 兜底）
┌────────────────────────────────────────────────────────────────┐
│   M1 接入模块 (rag-api, FastAPI+Swagger)                         │
│   路由 / 鉴权 / 限流 / 多租户上下文 / SSE / 问答编排 / traceid     │
└───────────────┬────────────────────────────────────────────────┘
                │  Query 进入问答编排
┌───────────────▼───────────────────────────────────────────────┐
│  M9 问答路由层（FAQ 优先）                                       │
│  ① 答案缓存命中? → 直接返回                                      │
│  ② FAQ 检索(bge-m3 dense+sparse, scope=1) → 超阈值? → 标准答案   │
│  ③ 未命中 → 转 RAG：M2 索引 / M4 检索 / M8 生成                  │
└───┬───────────────────────┬──────────────────────────────────┘
    │ 索引链路（异步 Worker）│ 查询链路（在线）
┌───▼────────────┐  ┌────────▼─────────────────┐
│ M2 文档处理模块 │  │   M4 检索模块 (retriever) │
│ 获取→解析→分块→向量化│ 两路召回→RRF融合→Rerank  │
└──┬─────────┬──┘  └───────┬──────────────────┘
   │         │             │
┌──▼──┐ ┌────▼─────┐ ┌─────▼──────────┐
│ M3  │ │ M5 模型   │ │ M6 评测与运维   │
│ 存储 │ │ 服务模块  │ │ (指标/日志/灰度) │
└──┬──┘ └──────────┘ └────────────────┘
   │   ┌──────────────────┐
   └──►│ M7 控制台  M8 生成 │ (LangChain)
       └──────────────────┘
```

### 2.1 模块职责

| 模块 | 名称 | 职责 | 关键技术 |
|---|---|---|---|
| M1 | rag-api | 请求接入、租户鉴权、限流、Swagger、SSE、编排、traceid | FastAPI、Pydantic v2、slowapi |
| M2 | rag-indexer | 文档获取、解析、清洗、分块、向量化、落库（异步 Worker） | Celery/ARQ、MinerU/PyMuPDF、bge-m3 |
| M3 | rag-store | 多源持久化与隔离 | MySQL 8、Milvus（dense+sparse IP）、Redis |
| M4 | rag-retriever | 两路混合召回、RRF 融合、Rerank、上下文组装 | bge-m3、RRF、bge-reranker-v2-m3 |
| M5 | rag-model | Embedding / Rerank 统一封装 | bge-m3、bge-reranker-v2-m3、google-bert（可选兜底） |
| M6 | rag-ops | 评测、监控、灰度、索引重建 | RAGAS、Prometheus、双集合切换 |
| M7 | rag-console | 管理控制台：租户/FAQ/文件/文档/检索调试 | FastAPI admin API + 前端页 |
| M8 | rag-generator | 生成层（LangChain）：检索结果→Prompt→LLM，多轮拼接、溯源 | LangChain LCEL、平台 LLM 网关 |
| M9 | qa-router | 问答路由：答案缓存 → FAQ 优先 → RAG 兜底（阈值路由） | bge-m3、Redis 缓存、阈值判定 |

### 2.2 技术栈

| 层 | 选型 |
|---|---|
| 服务框架 | FastAPI + Pydantic v2 + asyncio |
| 配置加载 | `ConfigManager`（env + 可选 YAML，Pydantic 校验）→ `app_state.settings` |
| 异步任务 | Celery / ARQ（Redis broker）+ 幂等状态机 |
| 元数据 | MySQL 8（按 tenant_id 隔离） |
| 向量库 | Milvus 2.4+（dense 1024 cosine + sparse IP，partition key 隔离） |
| 缓存/队列/限流 | Redis |
| 模型 | bge-m3（dense+sparse）、bge-reranker-v2-m3、google-bert（可选兜底） |
| 源文件 | OSS（生产）/ MinIO（可选）/ Mock FileProvider（当前） |
| 生成 | LangChain LCEL + 平台 LLM 网关（OpenAI 兼容） |
| 可观测 | Prometheus + 结构化 JSON 日志（trace_id）+ Swagger；LangSmith/Langfuse（建议） |

---

## 3. 数据存储设计（M3）

### 3.1 存储分工

| 数据 | 存储 | 说明 |
|---|---|---|
| 源文件 | OSS / MinIO / Mock | 仅存引用与下载 URL，流式解析不落本地盘 |
| 元数据 | MySQL 8 | 租户、文档、分块、任务、FAQ、上传记录、权限 |
| 向量（dense + sparse） | Milvus | bge-m3 dense(1024, cosine) + sparse(IP)，doc 与 faq 同集合分区 |
| 缓存/会话/队列/限流 | Redis | 答案缓存（query→answer）、多轮 history、embedding 缓存、任务队列、令牌桶 |

> 原"BM25 倒排（Redis term→posting）"已移除，词法召回由 **bge-m3 sparse + Milvus IP** 承担。

### 3.2 MySQL 核心表

```sql
CREATE TABLE rag_document (
  id BIGINT PRIMARY KEY, tenant_id BIGINT NOT NULL,
  doc_type TINYINT DEFAULT 0,            -- 0=普通文档 1=FAQ 来源
  oss_key VARCHAR(512) NOT NULL,        -- 存储路径（OSS/MinIO 对象键 或 Mock 本地相对路径），即 FileProvider 取文件 key
  source_path VARCHAR(512),             -- 原始来源路径（溯源/按路径去重，不参与取文件）
  file_name VARCHAR(256), file_type VARCHAR(16), file_size BIGINT, file_hash VARCHAR(64),
  status TINYINT, err_msg VARCHAR(512), created_at DATETIME, updated_at DATETIME,
  KEY idx_tenant_status (tenant_id, status)
);

CREATE TABLE rag_chunk (
  id BIGINT PRIMARY KEY, tenant_id BIGINT NOT NULL, doc_id BIGINT NOT NULL,
  scope TINYINT DEFAULT 0,              -- 0=doc 1=faq
  ref_id BIGINT DEFAULT 0,              -- scope=1 时指向 rag_faq.id
  seq INT, content TEXT, token_count INT, meta JSON, enabled TINYINT DEFAULT 1, created_at DATETIME, updated_at DATETIME,
  KEY idx_doc (tenant_id, doc_id)
);

CREATE TABLE rag_index_task (
  id BIGINT PRIMARY KEY, tenant_id BIGINT NOT NULL, doc_id BIGINT,
  stage VARCHAR(16), status TINYINT, retry_count INT DEFAULT 0, created_at DATETIME, updated_at DATETIME,
  KEY idx_status (status, stage)
);

CREATE TABLE rag_faq (
  id BIGINT PRIMARY KEY, tenant_id BIGINT NOT NULL,
  question VARCHAR(512) NOT NULL, answer TEXT NOT NULL,
  similar_questions TEXT,               -- 相似问题（多行文本，命中候选）
  question_type TINYINT NOT NULL DEFAULT 0,  -- 0=通用知识 1=专业咨询（对齐意图分类器）
  enabled TINYINT NOT NULL DEFAULT 1, created_at DATETIME, updated_at DATETIME,
  KEY idx_tenant_enabled (tenant_id, enabled), KEY idx_tenant_type (tenant_id, question_type)
);

CREATE TABLE rag_file_upload (
  id BIGINT PRIMARY KEY, tenant_id BIGINT NOT NULL,
  base_key VARCHAR(512) NOT NULL, oss_key VARCHAR(512) NOT NULL, source_path VARCHAR(512),
  version INT NOT NULL DEFAULT 1, file_name VARCHAR(256), file_type VARCHAR(16), file_size BIGINT,
  status TINYINT DEFAULT 1, expires_at DATETIME, created_at DATETIME, updated_at DATETIME,
  UNIQUE KEY uk_base_ver (base_key, version), KEY idx_tenant (tenant_id)
);
```

> **路径字段**：`oss_key` 即存储路径（FileProvider 取文件唯一 key），无需另设 `path`；`source_path` 仅记录原始来源路径，便于溯源与去重。

### 3.3 Milvus 集合 Schema（dense cosine + sparse IP，partition key 隔离）

```python
fields = [
    FieldSchema(name="chunk_id",   dtype=DataType.INT64, is_primary=True),
    FieldSchema(name="tenant_id",  dtype=DataType.INT64, is_partition_key=True),
    FieldSchema(name="scope",      dtype=DataType.INT8),     # 0=doc 1=faq
    FieldSchema(name="dense",      dtype=DataType.FLOAT_VECTOR, dim=1024),  # cosine
    FieldSchema(name="sparse",     dtype=DataType.SPARSE_FLOAT_VECTOR),    # IP
]
# dense:  HNSW(GPU 可选) metric_type="COSINE"
# sparse: SPARSE_INVERTED_INDEX metric_type="IP"
```

> `tenant_id` 作 partition key；`scope` 区分 doc/faq 供检索 filter。文档/切片为租户级，不再挂知识库。

### 3.4 稀疏召回：bge-m3 sparse + Milvus IP

- bge-m3 一次推理同时产出 dense(1024) 与 sparse；sparse 值是**学习到的词重要性权重**（非负 relu），非原始词频。
- sparse 向量在 Milvus 用 **`IP`（内积）** 度量：`IP(query_sparse, doc_sparse)=Σ w_i·w_j`（共享词权重积和），即加权重合度，是 bge-m3 官方默认用法。**不对 bge-m3 sparse 使用 Milvus `BM25` metric**（`BM25` 是给"原始 TF 稀疏向量"准备的）。
- 词法信号（型号、代码、人名、ID）由 bge-m3 sparse 覆盖，与独立 BM25 能力高度重叠，故移除倒排后核心召回不损失，且少一套 Redis 倒排 + 分词 + 增量维护 + 并发锁。

---

## 4. 源文件获取：Mock FileProvider（适配当前无 OSS 环境）

抽象 `FileProvider` 接口，统一返回"可下载/可上传 URL + 元信息（+ 可选 base64 内容）"，下游只依赖接口，不关心底层 OSS/MinIO/Mock。

```python
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

class FileProvider(ABC):
    @abstractmethod
    async def get_file(self, oss_key: str, with_base64: bool = False) -> FileRef: ...
    @abstractmethod
    async def get_files(self, oss_keys: List[str], with_base64: bool = False) -> List[FileRef]: ...
    @abstractmethod
    async def put_file(self, base_key: str, data: bytes, ext: str) -> FileRef: ...
    @abstractmethod
    async def healthcheck(self) -> bool: ...
```

- `with_base64=True`：Provider 读取字节并 `base64.b64encode(...).decode()` 填入 `FileRef.base64`，**仅小文件**；大文件接口层校验 `file_size ≤ BASE64_MAX_BYTES`，超限忽略 base64 仅返回 url。
- 三种实现与回退链：`OssFileProvider` → `MinioFileProvider`（本地 MinIO 优先）→ `MockFileProvider`（兜底）。索引/解析链路零改动切换。

---

## 5. 文档索引链路（M2，异步 Worker）

```
建 doc 记录(oss_key) → 投递 index_task 到 Redis 队列
  fetch  : FileProvider.get_file(oss_key) → URL（流式下载到内存/临时区）
  parse  : MinerU/PyMuPDF(PDF)、python-docx(Word)、openpyxl(Excel)、RapidOCR(扫描件)
  clean  : 去页眉页脚、乱码过滤、表格转 Markdown
  chunk  : 标题感知递归分块，chunk_size≈512 token，overlap≈64；表格整块；父子分块
  embed  : bge-m3 批量编码 → dense(1024) + sparse 一次产出（batch=32~64）
  index  : ① chunk 写 MySQL(scope=0)  ② (chunk_id,dense,sparse,scope) 写 Milvus
           ③ 更新 doc.status=完成
```

要点：每阶段独立任务、幂等可重试（状态机）；失败进死信队列告警；批量 Embedding + 限流；Milvus 批量 insert；按 `file_hash` 去重；更新走"双写新版本→校验→切换→删旧"；所有写带 `tenant_id`。

**FAQ 入库**：`faq_qa` 写入后，每条标准问题当作一个 chunk（`scope=1, ref_id=faq_qa.id`）走同样 `embed`，bge-m3 产出 dense+sparse 写入 Milvus，无需单独分词/倒排。

---

## 6. 问答编排与查询链路（M9 + M4，在线 + 多轮）

### 6.1 FAQ 优先 + RAG 兜底路由

```
Query
  → 归一化 → Redis 答案缓存命中(query hash)? → 直接返回（最快路径）
  → 否 → 【FAQ 检索】(M4, scope=1, bge-m3 dense+sparse, RRF)
         top-1 相似度 ≥ faq_qa.sim_threshold?
            ├─ 命中: 取 faq_qa.answer（标准答案）→ 回写缓存 → 返回
            └─ 未命中: 【RAG 检索】(M4, scope=0)
                  → 两路召回(dense cosine + sparse IP) → RRF top-50
                  → bge-reranker-v2-m3 精排 top-5~8 → 父块扩展/来源标注
                  → M8 LangChain 生成 → 回写缓存 → 返回
```

- **阈值路由**：FAQ 相似度分数阈值做 FAQ/RAG 分流，零额外模型调用、零额外延迟。
- 多轮：`session_id` 命中则从 Redis 取历史做轻量 query 改写/指代消解后再走流程。

### 6.2 两路混合召回（RAG 部分）

| 通道 | 模型/方法 | 强项 | 弱项 |
|---|---|---|---|
| Dense | bge-m3 dense(1024, cosine) | 语义相似、同义改写、跨语言 | 专有名词/编号/冷门实体易丢 |
| Sparse | bge-m3 sparse（Milvus IP） | 学习词级权重，精确关键词：型号/代码/人名/ID | 超词表罕用语覆盖有限 |

bge-m3 一次推理同时产出 dense+sparse，避免二次编码。RRF(k=60) 不需分数归一化、对单路失效鲁棒；可按租户配置两路权重/开关。FAQ 检索复用完全相同的两路召回，仅 `scope=1` 过滤。

### 6.3 关键接口

```python
class Retriever:
    async def retrieve(
        self, query: str, tenant_id: int,
        scope: Literal["doc", "faq"] = "doc",
        top_k: int = 5, recall_k: int = 20,
        fusion: Literal["rrf", "weighted"] = "rrf",
        rerank: bool = True, session_id: str | None = None,
        filters: dict | None = None,
    ) -> list[RetrievedChunk]: ...
```

### 6.4 多轮问答

- 会话存储（Redis）：`sess:{tenant_id}:{session_id}` 存 `[{role, content, ts}]`，TTL 默认 30m。
- 上下文感知：`retrieve()` 收 `session_id` 先取历史做轻量改写/指代消解，失败退化为仅当前 query。
- 历史回写：`ConversationStore.append(tenant_id, session_id, role, content)` 由调用方每轮结束写入。
- 隔离：session 仅绑定 user_id（网关 OAuth 全局唯一下发，跨租户不重复），越权在路由层按用户拦截。

```python
class ConversationStore:
    def __init__(self, redis, ttl: int = 1800): ...
    async def append(self, tenant_id, session_id, role, content): ...   # 原子 RPUSH
    async def get_history(self, tenant_id, session_id, last: int = 6): ...
    async def expire(self, tenant_id, session_id): ...
```

---

## 7. 模型服务（M5）

| 用途 | 模型 | 说明 |
|---|---|---|
| Dense + Sparse Embedding | **bge-m3** | 一次推理产出 dense 与 sparse（IP）；GPU 服务化、批量化、embedding 缓存 |
| Rerank | **bge-reranker-v2-m3** | 服务化；超时降级为仅 RRF |
| 可选兜底 | **google-bert** | bge-m3 不可用时兜底 embedding / 多轮 query 改写（不再承担 BM25 分词） |
| LLM | 平台统一 LLM 网关 | RAG 模块不直接依赖具体厂商 |

部署：Embedding/Rerank 独立服务（GPU），与检索解耦、横向扩容；调用加熔断、超时与重试；LLM 走平台网关。

---

## 8. 多租户与文档隔离

| 维度 | 实现 |
|---|---|
| 接入层 | 解析 `tenant_id` 写入请求上下文（FastAPI dependency），所有 DB/向量/Redis 操作强制带 `tenant_id` |
| MySQL | 所有表带 `tenant_id`，索引加 `(tenant_id, ...)` |
| Milvus | `tenant_id` partition key（或按 kb 独立 Collection） |
| Redis | 所有 key 加 `t:{tenant_id}:` 前缀 |
| 鉴权 | 非法跨租户在 M1 拦截，检索结果再过滤兜底 |
| 配额 | 按租户限流（令牌桶）、配额统计（文档/向量/FAQ 数） |

---

## 9. FAQ 问答层（M9）

### 9.1 为什么需要 FAQ 层

| 维度 | FAQ（问答对） | RAG（文档检索） |
|---|---|---|
| 回答形态 | 标准答案，一字不差、可控可审计 | LLM 生成，存在幻觉风险 |
| 延迟/成本 | 毫秒级、无 LLM 调用 | 数百 ms~数秒 + token 成本 |
| 适用问题 | 高频、标准、措辞固定 | 长尾、开放、需跨文档推理 |

### 9.2 与原 BM25 流程图的关键差异（升级点）

- **FAQ 检索复用 M4**：FAQ 作为 `scope=1` chunk 入库，**复用 bge-m3 两路混合检索**（语义匹配强于纯 VSM 余弦）。
- **答案缓存**：原图"问题→答案"缓存即 Redis 答案缓存，键由**归一化 query hash** 生成，提高命中率。
- **阈值路由**：FAQ 相似度超阈值→直出标准答案（可选 LLM 轻微润色）；低于阈值→降级走 RAG。天然承担 FAQ/RAG 路由，零额外延迟。
- **兜底**：RAG 也无结果（Rerank 过低）→ 返回"未找到"或转人工。

### 9.3 落地形态

```
Query → [FAQ 层] M4(scope=1) + 阈值判定
          ├─ 命中 → 标准答案（缓存回写）
          └─ 未命中 → [RAG 层] 两路检索 → Rerank → M8 生成
```

对 RAG 改动很小：新增 `faq_qa` 表、检索入口加 `scope` 与阈值判定、`doc/chunk` 加 `scope` 字段，M2~M5 基础设施全部复用。

---

## 10. 文件上传接口（Mock，含有效期与版本号）

- **上传**：`POST /v1/files/upload`（multipart）→ `MockFileProvider.put_file` 落本地临时区（有 MinIO 存 MinIO）→ 生成 `oss_key = upload/{tenant_id}/{yyyyMM}/{uuid}.{ext}` → 同 `base_key` 再传则 `version += 1`，旧版本标记 `superseded` 仍可定位 → 写 `file_upload` 表 → 返回 `oss_key / version / expires_at`。
- **有效期**：`expires_at = now + TTL`（默认 24h）；过期后 `get_file` 返回 410；Redis 记录 TTL 清理。
- **版本号**：`file_upload(base_key, version)` 唯一；可回滚/下载指定版本（分配加锁见 §14）。
- 与 doc 解耦：先上传拿 `oss_key`，再 `POST /v1/docs` 建 doc 触发索引。

---

## 11. 优雅启动：AppServer + XXManager

服务启动封装为 `AppServer`，每个启动项封装成 `XXManager`（统一 `start()`/`stop()`），`register` 统一注册，单一函数 `run()` 启动，支持 SIGINT/SIGTERM 优雅停机。

### 11.1 全局配置加载：ConfigManager（首个注册项）

最先启动（order=0）：从 env / 可选 `config.yaml` 加载，Pydantic `BaseSettings` 校验（缺项 fail-fast），加载密钥，注入 `app_state.settings`；**其余 Manager 只消费、不再各自读环境**。

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_", env_file=".env", extra="ignore")
    REDIS_URL: str; MYSQL_DSN: str; MILVUS_URI: str
    OSS_ENABLED: bool = False; MINIO_ENABLED: bool = False
    HOST: str = "0.0.0.0"; PORT: int = 8000
    FILE_TTL_HOURS: int = 24
    BASE64_MAX_BYTES: int = 5 * 1024 * 1024
    FAQ_SIM_THRESHOLD: float = 0.82

class ConfigManager:
    def __init__(self): self.settings = None
    async def start(self):
        self.settings = Settings(); app_state.settings = self.settings
    async def stop(self): pass
```

### 11.2 其余 Manager 与 AppServer

```python
class RedisManager:
    async def start(self):
        self.pool = await redis.from_url(app_state.settings.REDIS_URL, decode_responses=True)
        app_state.redis = self.pool
    async def stop(self): await self.pool.aclose()

class MySQLManager:
    async def start(self):
        app_state.engine = create_async_engine(app_state.settings.MYSQL_DSN, pool_pre_ping=True)
        async with app_state.engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)   # 生产建议 Alembic 迁移
    async def stop(self): await app_state.engine.dispose()

class MilvusManager:
    async def start(self):
        app_state.milvus = MilvusClient(app_state.settings.MILVUS_URI)
        ensure_collection(dense_metric="COSINE", sparse_metric="IP")
    async def stop(self): app_state.milvus.close()

class ModelManager:
    async def start(self): warmup_model_clients()
    async def stop(self): pass

class FileProviderManager:
    async def start(self):
        app_state.file_provider = build_file_provider(app_state.settings)
        await app_state.file_provider.healthcheck()
    async def stop(self): pass

class FastAPIManager:
    def __init__(self, app): self.app = app
    async def start(self): configure_routes(self.app)
    async def stop(self): pass

class NacosDiscoverManager:
    """服务发现与注册（nacos，namespace=public；配置中心另见 NacosConfigManager）。
    注册时机晚于 FastAPI，确保服务已真正监听后再对外暴露。"""
    async def start(self):
        nc = build_nacos_client(app_state.settings)   # 读 NACOS_* 配置
        await nc.register_service(
            service_name=app_state.settings.SERVICE_NAME or "rag-qa",
            ip=app_state.settings.SERVICE_IP,
            port=app_state.settings.PORT,
            metadata={"trace": "on"},
        )
        self._nc = nc
        self._hb = asyncio.create_task(nc.send_heartbeat_loop())  # 心跳协程
    async def stop(self):
        self._hb.cancel()
        await self._nc.deregister_service()           # 优雅注销，摘除流量

class AppServer:
    def __init__(self, name="rag"): self._items = []
    def register(self, manager, order=100):
        self._items.append((order, manager.__class__.__name__, manager)); self._items.sort()
    async def _start(self):
        self._stack = []
        for _, name, mgr in self._items:
            await mgr.start(); self._stack.append((name, mgr))
    async def _stop(self):
        for name, mgr in reversed(self._stack):
            try: await mgr.stop()
            except Exception: self.logger.exception("stop failed: %s", name)
    def run(self):
        import asyncio, signal
        from contextlib import suppress
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, lambda: loop.stop())
        with suppress(asyncio.CancelledError):
            loop.run_until_complete(self._start())
            uvicorn.run(app_state.app, host=app_state.settings.HOST, port=app_state.settings.PORT)
        loop.run_until_complete(self._stop())
```

**统一注册**：

```python
server = AppServer("rag")
server.register(ConfigManager(),       0)
server.register(LoggingManager(),      10)
server.register(RedisManager(),        20)
server.register(MySQLManager(),        30)
server.register(MilvusManager(),       40)
server.register(ModelManager(),        50)
server.register(FileProviderManager(), 60)
server.register(FastAPIManager(app),  100)
server.register(NacosConfigManager(),     105)   # 配置中心热加载（namespace=rag-ultra）
server.register(NacosDiscoverManager(),   110)   # 服务已监听后再注册进 nacos
server.run()
```

优雅性：按依赖顺序启动；任一项失败 fail-fast；停机逆序释放连接；uvicorn 自身 drained 在途请求。

> **多进程注意**：gunicorn + 多 uvicorn worker 时每 worker 都执行 `AppServer.run()`。将"有副作用且只需一次"的操作（建表/迁移/消费者启动）抽到单实例任务（Alembic 部署脚本执行，或 Redis 分布式锁保证只跑一次）。

---

## 12. 生成层：LangChain 组装检索结果 → LLM（M8）

检索（M4）只产 `RetrievedChunk`；**最终生成交给 LangChain**，底层 LLM 走平台网关。

```python
def to_documents(chunks):
    return [Document(page_content=c.content, metadata={
        "doc_name": c.doc_name, "page": c.page, "oss_key": c.oss_key, "score": c.score}) for c in chunks]

def format_docs(docs):
    return "\n\n".join(f"[来源:{d.metadata['doc_name']} P{d.metadata['page']}]\n{d.page_content}" for d in docs)

prompt = ChatPromptTemplate.from_messages([
    ("system", "你是企业智能体助手，仅依据以下上下文作答，并标注来源。\n{context}"),
    ("human", "{question}"),
])
llm = ChatOpenAI(model=GATEWAY_MODEL, api_key=GATEWAY_KEY, base_url=GATEWAY_BASE, streaming=True, temperature=0.1)
answer_chain = (
    RunnableParallel({"context": lambda x: format_docs(to_documents(x["chunks"])),
                      "question": lambda x: x["question"]})
    | prompt | llm | StrOutputParser()
)
async def generate(question, chunks, history=None):
    return await answer_chain.ainvoke({"question": question, "chunks": chunks})
```

要点：M4 永远纯检索（可被 Agent/评测直接调用）；多轮历史来自 `ConversationStore`；溯源含 `oss_key`，前端按需调 FileProvider 取原文（`base64=true` 直取）；`streaming=True` + FastAPI `StreamingResponse` 透传。**FAQ 命中不触发生成**。

---

## 13. 管理控制台页面（M7）

后端 admin API（FastAPI，`role=admin`，租户隔离）+ 前端页。

| 页面 | 功能 |
|---|---|
| 租户隔离 | 租户由网关经 OAuth 分配，本服务不维护租户表，仅按 X-Tenant-Id 隔离数据 |
| 文档管理 | 按租户查看文档、重索引（租户级，无独立知识库） |
| FAQ 管理 | 标准问答对 CRUD、相似度阈值、扩充说法、命中率统计 |
| 文件管理 | Mock 上传、列表（版本/有效期）、下载（可 base64）、删除、回滚版本、续期 |
| 文档索引 | 文档列表与状态、重试、查看分块、重索引 |
| 检索调试 | query，可视化 FAQ/RAG 路由、两路召回/RRF/Rerank，badcase |
| 监控 | QPS、延迟分位、FAQ 命中率、两路命中、模型健康、队列积压 |

访问控制：admin + tenant 范围；写操作经 M1 鉴权与审计日志。

---

## 14. 并发与多线程多进程（锁控制）

运行形态：在线检索 asyncio 单循环（CPU 重活交线程池/进程池）；索引 Celery/ARQ **多 Worker（可能多进程）**；FastAPI 可 **多 uvicorn worker（gunicorn）**。原则：**共享状态必须走 Redis/MySQL，禁止进程内可变状态**；能原子操作就不加锁，必须加锁用 **Redis 分布式锁**（租约/看门狗），禁止进程内 `threading.Lock`（跨进程无效）。

| 并发点 | 风险 | 控制措施 |
|---|---|---|
| 文件上传版本号分配 | version 竞态 | `file_upload(base_key, version)` 唯一约束兜底；按 base_key 加分布式锁后 `SELECT MAX(version)+1`；或用 Redis `INCR ver:{base_key}` 原子分配 |
| Milvus 写入 | 同 chunk_id 并发 upsert | 按 doc_id 串行化索引任务；chunk_id 主键，幂等 upsert |
| MySQL 状态机 | 连接竞争、状态重复推进 | 异步连接池 + 事务；`SELECT ... FOR UPDATE` 或乐观版本号 |
| 答案/结果缓存 | 多进程不一致 | 缓存落 Redis（非进程内存）；设合理 TTL |
| 会话历史追加 | 多轮并发写同 session | Redis `RPUSH` 原子追加 |
| base64 下载 | 大文件重复编码/内存膨胀 | 接口层校验 `file_size ≤ BASE64_MAX_BYTES`，超限仅返回 url；编码交线程池 |
| 多 uvicorn worker 启动 | 重复建表/消费 | 启动幂等；一次性副作用用 Redis 分布式锁协调 |

通用准则：CPU 密集交 Worker/线程池；外部调用带超时+熔断+重试（写操作幂等靠唯一键）；分布式锁（Redis SET NX + 看门狗 Lua 释放）。

---

## 15. 异步、性能与生产级

| 关注点 | 设计 |
|---|---|
| 配置集中 | `ConfigManager` 加载/校验 → `app_state.settings`，其余只消费 |
| 异步框架 | FastAPI 全程 async；I/O 异步驱动；CPU 重活交线程池/Worker |
| 任务解耦 | 索引走 Celery/ARQ Worker，API 仅投递任务返回 task_id |
| 批量并发 | Embedding 批量、Milvus 批量 insert、get_files 并发、两路召回 `asyncio.gather` |
| 缓存 | query embedding 缓存、答案缓存（FAQ/RAG）、结果短缓存 |
| 限流 | 接口层令牌桶 + 租户配额；模型服务限流 |
| 熔断/降级 | Rerank 超时→仅 RRF；FAQ 层失败→直接走 RAG；FileProvider 回退链 |
| 索引性能 | Milvus HNSW/GPU_IVF_PQ（dense）、SPARSE_INVERTED_INDEX+IP（sparse）、分片 insert |
| 灰度/回滚 | 双 Collection（`kb_v1/v2`）+ 切换开关；模型 kb 级灰度 |
| 并发安全 | 见 §14 |
| 可观测 | Prometheus + 结构化 JSON 日志（trace_id/tenant_id/service=rag-qa/span_id）+ Swagger；LangSmith/Langfuse（建议） |
| 安全 | 信任网关经 oauth 下发的租户/用户/角色上下文（header 透传 + 可选签名）；预签名 URL 时效、跨租户二次过滤、输入长度上限 |

---

## 16. 与平台其他模块的边界

| 边界 | 约定 |
|---|---|
| Agent 编排（LangGraph 等） | 仅通过 `qa(query, tenant_id, top_k, session_id)` 调用；FAQ/RAG 路由由本系统阈值完成，意图/多步路由由其负责 |
| 前端 / 接入层 | 溯源信息由 M4 返回（含 oss_key，按需 base64），前端只渲染 |
| LLM 生成 | 由 M8（LangChain）组装 Prompt 调平台 LLM 网关；FAQ 命中不触发生成 |

---

## 17. 关键接口清单（Swagger 自动生成，按业务分类）

### 17.1 问答与检索

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/qa` | 问答主入口：FAQ 优先 + RAG 兜底（强约束 tenant_id，可选 session_id） |
| POST | `/v1/retrieve` | 底层混合检索（scope=doc/faq，强约束 tenant_id） |

### 17.2 会话（多轮）

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/sessions/{session_id}/turns` | 写入一轮对话历史 |
| GET | `/v1/sessions/{session_id}` | 读取会话历史 |

### 17.3 文件（Mock 上传/下载/版本）

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/files/upload` | Mock 上传：返回 oss_key + version + expires_at |
| GET | `/v1/files/{oss_key}` | 经 FileProvider 取文件（`?base64=true` 返回 base64，仅 ≤ `BASE64_MAX_BYTES`） |
| POST | `/v1/files/batch` | 批量取文件，支持 `with_base64` |
| GET | `/v1/files/{base_key}/versions` | 查文件版本列表 |

### 17.4 文档（索引）

| Method | Path | 说明 |
|---|---|---|
| POST | `/v1/docs` | 建 doc 记录（绑定 oss_key）+ 触发索引 |
| GET | `/v1/docs/{doc_id}/status` | 查索引任务状态 |

### 17.5 FAQ

| Method | Path | 说明 |
|---|---|---|
| POST | `/admin/faq` | 新建/更新 FAQ（落 MySQL + 写透 Redis 缓存，字面精确匹配命中，不调大模型） |
| GET | `/v1/faq/{faq_id}` | 查 FAQ 详情 |

### 17.6 控制台 / 管理（admin，role=admin + 租户隔离）

| Method | Path | 说明 |
|---|---|---|
| — | `/admin/faq` | FAQ 管理 |
| — | `/admin/files` | 文件管理 |
| — | `/admin/docs` | 文档索引管理 |
| — | `/admin/retrieve-debug` | 检索调试 |

### 17.7 健康 / 运维

| Method | Path | 说明 |
|---|---|---|
| GET | `/v1/health` | 健康检查（含 Milvus/MySQL/Redis/Model），供 nacos 探活 |

---

## 18. 对接业务层架构（gateway / oauth / nacos）

业务 Java 侧已具备 **gateway / oauth / nacos**，问答系统**仅维护租户数据**；定位"被集成的一方"，不重复造基建。接入时在以下方面补齐：

| 维度 | 现状 | 对接需补齐 |
|---|---|---|
| 路由（gateway） | FastAPI 直暴露 `/v1/**` + Swagger | 网关配 `/qa/**` → 问答服务；**内部/对外端口分离**，对外仅暴露网关；网关统一 TLS/限流/熔断 |
| 认证（oauth） | M1 解析 JWT（开发态） | 信任网关经 oauth 下发的上下文（header：`X-Tenant-Id`/`X-User-Id`）；可选 HMAC 校验；Swagger 走 dev token；必要时接 introspection/公钥验签 |
| 注册（nacos） | 无注册中心 | 新增 `NacosDiscoverManager`（order=110，namespace=public）做**服务注册 + 心跳 + 优雅注销**；`/v1/health` 供 nacos 探活；配置中心另由 `NacosConfigManager`（order=105，namespace=rag-ultra）承载，经审批流写、业务侧经 loader 热加载读 |
| 租户维护 | 本系统为 tenant owner，**暂不同步业务侧** | 当前问答系统独立维护 `tenant` 表（控制台 `/admin/tenants` 管理）；若未来业务侧成为主数据源，再评估事件/Webhook/对账同步机制 |
| 日志/链路（traceid） | 日志带 traceid（仅本系统） | 与网关约定 **X-Trace-Id 透传**：提取→`contextvars`→全链路→响应头回写；下游调用（Milvus/Redis/Model）透传；统一字段 `service=rag-qa,trace_id,tenant_id,span_id`；未来接 Loki/ES + OTel/Tempo |
| 可观测对齐 | Prometheus + Swagger | metrics 暴露业务大盘；统一错误码/SLA；LangSmith/Langfuse 作内部补充 |
| 配置安全 | `ConfigManager` 加载 env/yaml（含密钥） | 非敏感运行时配置（提示词/检索参数）经 Nacos 配置中心（审批流写、loader 热加载读）；敏感配置仍走环境变量/密钥管理（如 KMS），不进 Nacos |

**要点**：问答系统不重复造 gateway/oauth/nacos，核心是 **信任网关上下文 + 注册进 nacos（仅服务发现）+ 独立维护租户 + traceid 贯通**；仅在边界（M1 接入层、AppServer 注册项）做最小改动。

---

*修订记录：v1.0 初版（PGVector + text-embedding-v4）；v2.0 Milvus + Redis + bge-m3 系列 + Mock FileProvider + 多租户；v2.1 多轮问答/上传/AppServer/控制台；v2.2 LangChain + LangGraph/LangSmith 分析；v3.0 全文梳理；v3.1 路径字段/XXManager/并发锁；v3.2 ConfigManager/base64；v4.0 去 BM25(bge-m3 sparse IP 两路)、整合 FAQ 层、google-bert 降级；v4.1 接口分类、业务基建/traceid 约束、§19 业务层对接；**v5.0 从头梳理：仅保留已确定设计，删除所有"是否采用 X"的分析章节（意图识别/LangGraph/LangSmith/MCP/A2A 讨论），确认结论压入 §0.2 决策表与对应模块；BM25 移除、FAQ 阈值路由、LangChain 仅生成层、MCP/A2A 暂不需要等直接落位；业务层对接保留为 §18。v5.1 业务侧约束收敛：nacos 双命名空间落地：public 服务发现（NacosDiscoverManager, order=110）+ rag-ultra 配置中心（NacosConfigManager, order=105，经审批流写、loader 热加载读）；元数据表统一 `rag_` 前缀且含 created_at/updated_at；配置中心对接设计见《配置中心对接设计》；租户暂由问答系统独立维护（tenant 表 + 控制台），暂不对接业务侧同步。** v5.2 移除 rag_tenant 表与 X-Roles：租户由网关经 OAuth 分配，本服务不维护租户表、不做角色校验（admin 端点由网关鉴权守护）。*

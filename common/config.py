"""
config2 - 配置类（更好的版本）

说明：推荐使用 Pydantic Settings 彻底替代 configparser。它提供自动类型校验、环境变量自动映射及默认值约束。

Author: 骆昊
Version: 0.0.1
"""
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from core.config_center.groups import ConfigGroup

# 根布局下：config.py 位于 <ROOT>/common/config.py，需向上 2 级到达项目根
BASE_DIR = Path(__file__).resolve().parent.parent

# 尽早加载 .env：用 BASE_DIR 精确定位，文件中的值覆盖已存在的系统环境变量（override=True），
# 必须在下方 Config（BaseSettings）读取环境变量之前执行。
load_dotenv(dotenv_path=BASE_DIR / '.env', override=True)


class Config(BaseSettings):
    """配置类"""
    FILES_DIR:   Path = BASE_DIR / 'data' / 'files'
    RES_DIR: Path = BASE_DIR / 'data' / 'res'
    TRAIN_DIR: Path = BASE_DIR / 'data' / 'train'
    MODELS_DIR: Path = BASE_DIR / 'models'
    UPLOAD_DIR: Path = BASE_DIR / 'data' / 'uploads'
    TMP_DIR: Path = BASE_DIR / 'tmp'

    # 模型子目录（BGE 系列）
    BGE_M3_DIR:     str = 'BAAI/bge-m3'
    BGE_RERANKER_DIR: str = 'BAAI/bge-reranker-v2-m3'
    BERT_MODEL_DIR:  str = 'google-bert/bert-base-chinese'
    BERT_INTENT_DIR: str = 'bert-intent-recognition-model'


    # 检索 / 召回
    RECALL_K:        int = Field(default=50, ge=1)    # 两路召回后 RRF 融合保留条数
    RERANK_TOP_K:    int = Field(default=8, ge=1)     # Rerank 精排后保留条数
    RRF_K:           int = Field(default=60, ge=1)    # RRF 常数 k
    SUBQUERY_MAX:          int = Field(default=3, ge=1)             # 子查询拆分最大条数
    STRATEGY_SELECT_ENABLED: bool = True                            # 是否启用 LLM 检索策略路由（关闭则专业咨询也走直接检索）

    # 问答明细落库（详见 docs/会话记录持久化设计方案.md）
    QA_PERSIST_ENABLED:         bool = True    # 总开关；false 时 _persist_session 直接返回（保持 no-op 行为）
    QA_PERSIST_ASYNC:           bool = True    # 异步落库；false 走同步 await（单测/排障用）
    QA_PERSIST_MAX_TURNS:       int = 400      # Redis LIST 单会话保留的消息条数（一轮 2 条），超出 LTRIM
    QA_PERSIST_MAX_CONCURRENCY: int = 32       # 异步落库并发上限，超出丢弃并告警（不拖慢主链路）
    QA_PERSIST_CHUNK_FULLTEXT:  bool = False   # chunks 是否附带片段全文；默认只存引用（doc_name/page/score）
    QA_EVAL_SAMPLE_RATE:        float = Field(default=0.05, ge=0.0, le=1.0)  # 评测采样率，命中时 rag_turn.eval_flag=1

    # 上下文装配与压缩（按字符数配置，非 token；详见 docs/上下文压缩设计方案.md）
    # 多轮装配侧：记忆 / 历史注入上限
    HISTORY_MAX_MESSAGES: int = Field(default=6, ge=0)   # 注入 prompt 的历史消息条数（替代 get_history 写死的 last=6）
    MEMORY_MAX_CHARS:     int = Field(default=2000, ge=0)  # 记忆注入文本字符上限，超限按句截断
    ASSEMBLY_MAX_PROMPT_CHARS: int = Field(default=24000, ge=0)  # 送进模型的 prompt 总字符额度（system+context+question）
    ASSEMBLY_MIN_HISTORY_MESSAGES: int = Field(default=2, ge=0)  # 需要裁剪时历史消息的下限（保最近几条，支撑指代消解）
    # 检索片段压缩侧：L0 截取 / L1 精选 / L2 摘要
    COMPRESS_STRATEGY:              str   = 'truncate'   # none | truncate | select | summary（summary 含 L2）
    COMPRESS_MAX_TOTAL_CHARS:       int   = Field(default=8000, ge=0)    # 软上限：累计字符数，超过按分数截取
    COMPRESS_MAX_CHUNK_CHARS:       int   = Field(default=1500, ge=0)    # 单片段字符上限，超出的先按句截短
    COMPRESS_HARD_TOTAL_CHARS:      int   = Field(default=16000, ge=0)   # 硬上限：超过则强制截取或启用 L2 摘要
    COMPRESS_SIM_THRESHOLD:         float = Field(default=0.3, ge=0.0)   # L1 相关性丢弃阈值（select/summary 生效）
    COMPRESS_LLM_SUMMARY_ENABLED:   bool  = False        # 是否允许 L2（付费）；false 时超硬上限退化为强制截取
    DENSE_WEIGHT:    float = Field(default=0.7, ge=0.0, le=1.0)     # 加权融合稠密权重
    SPARSE_WEIGHT:   float = Field(default=0.3, ge=0.0, le=1.0)     # 加权融合稀疏权重

    # FAQ 模糊匹配（BM25）：精确匹配未命中时按相似度 + 阈值兜底（机制对齐 rag-pro）
    FAQ_BM25_ENABLED:   bool  = True                               # 是否启用 BM25 相似匹配
    # BM25 原始分阈值（raw score，非归一化，随语料规模增大而升高）。默认 2.0 按"单租户数十条 FAQ"
    # 校准（好的匹配约 2~4，不相关≈0）；FAQ 规模更大宜调高，更稀疏调低。
    FAQ_BM25_THRESHOLD: float = Field(default=2.0, ge=0.0)

    # 业务 API 的 MCP 服务：把 app 的 HTTP API 封装成 MCP 工具，启动时加载并起服务，
    # 供外部 MCP 客户端调用（与上方"基础设施 MCP"区分：那个是 PostgreSQL/redis/... 工具）。
    MCP_API_ENABLED:   bool = True                            # 是否启用（由 MCPApiManager 加载并起服务）
    MCP_API_TRANSPORT: str  = 'streamable-http'               # 传输方式（需常驻服务，故不用 stdio）
    MCP_API_HOST:      str  = '127.0.0.1'
    MCP_API_PORT:      int  = 8766                            # 与基础设施 MCP(8765) 区分
    # 不暴露为 MCP 工具的路径前缀（文档 / 自描述 / 探活等）
    MCP_API_EXCLUDE:   Annotated[list[str], NoDecode] = [
        '/docs', '/redoc', '/openapi.json', '/admin/health']

    # 调用方签名校验（HMAC-SHA256）：防"绕过网关直连、伪造 X-Tenant-Id/X-User-Id"
    # 开启后调用方必须带 X-Auth-Timestamp / X-Auth-Nonce / X-Auth-Signature（见 core.auth）
    AUTH_HMAC_ENABLED:    bool = True    # 默认关闭：本地自测与既有调用不受影响；接入网关后再开
    AUTH_HMAC_SECRET:     str = ''        # 当前密钥（开启时必填，否则无法验签）
    AUTH_HMAC_SECRET_PREV: str = ''       # 轮换期旧密钥：与当前密钥任一匹配即通过，可留空
    AUTH_HMAC_TTL:        int = 60        # 时间戳允许偏差（秒），同时决定 nonce 去重窗口
    # 不验签的路径前缀（文档 / 自描述 / 探活）
    AUTH_HMAC_EXCLUDE: Annotated[list[str], NoDecode] = [
        '/docs', '/redoc', '/openapi.json', '/admin/health']

    # API 调用日志：中间件记录"请求 + 响应"并落 rag_api_call_log（一条请求一行）
    API_LOG_ENABLED:   bool = True                            # 是否启用 API 调用日志
    API_LOG_DB:        bool = True                            # 是否落库（关闭则仅打日志）
    API_LOG_MAX_BODY:  int  = 8192                            # 请求/响应体记录上限（字节），超出截断
    # 不记录的路径前缀（探活等高频调用，避免刷表）
    API_LOG_EXCLUDE:   Annotated[list[str], NoDecode] = [
        '/docs', '/redoc', '/openapi.json', '/admin/health']

    # 向量库（Milvus）
    MILVUS_URI:      str = 'http://localhost:19530'   # AppServer 注册项注入时优先用此 URI
    MILVUS_DB_NAME:  str = 'rag_ultra'
    MILVUS_COLLECTION: str = 'rag_chunks'

    # 文本加载时的备用编码降级顺序：优先编码解码失败后，依次尝试以下编码
    CANDIDATE_ENCODINGS: list[str] = ['utf-8-sig', 'gbk', 'gb2312', 'gb18030', 'big5']

    POSTGRES_HOST:     str = 'localhost'
    POSTGRES_PORT:     int = 5432
    POSTGRES_USER:     str = 'postgres'
    POSTGRES_PASSWORD: str = ''
    POSTGRES_DATABASE: str = 'rag_ultra'
    POSTGRES_POOL_MIN_SIZE: int = 4
    POSTGRES_POOL_MAX_SIZE: int = 16

    REDIS_HOST:       str = 'localhost'
    REDIS_PORT:       int = 6379
    REDIS_PASSWORD:   str = ''
    REDIS_DB:         int = 0

    # Redis 键有效期（秒）：集中配置，避免硬编码；可用环境变量覆盖
    REDIS_TTL_PROMPT:          int = 3600              # 提示词缓存
    REDIS_TTL_ANSWER:          int = 3600              # 答案缓存
    REDIS_TTL_EMBEDDING:       int = 86400             # 向量 embedding 缓存
    REDIS_TTL_RATELIMIT:       int = 1                 # 限流令牌桶
    REDIS_TTL_SESSION:         int = 1800              # 会话类键（复用 SESSION 前缀的其它场景）
    REDIS_TTL_SESSION_CONTENT: int = 30 * 24 * 3600    # 会话对话原文（30 天）
    REDIS_TTL_MEMORY:          int = 30 * 24 * 3600    # 会话记忆（30 天）
    REDIS_TTL_FAQ:             int = 30 * 24 * 3600    # FAQ 缓存（启动全量载入 + 写穿透，长 TTL 作兜底）
    REDIS_TTL_CHAT_STOP:       int = 120                # 聊天停止标记（秒，短 TTL 自动过期；跨 worker 取消生成）
    REDIS_TTL_AUTH_NONCE:      int = 120                # 签名 nonce 去重（取 ≥ 2×AUTH_HMAC_TTL）

    CACHE_KEY_PREFIX: str = 'ragpro:qa'

    LLM_MODEL_NAME:     str = 'deepseek-flash'
    LLM_MODEL_PROVIDER: str = 'openai'
    LLM_BASE_URL:       str = 'https://api.deepseek.com'
    LLM_TEMPERATURE:    int = 0
    LLM_MAX_TOKENS:     int = 1000
    LLM_THINKING_TYPE:  str = 'disabled'
    LLM_API_KEY:        str = Field(default='', validation_alias='OPENAI_API_KEY')
    OPENAI_API_KEY:     str = Field(default='', validation_alias='LLM_API_KEY')

    PARENT_CHUNK_SIZE:    int = Field(default=1200, ge=300, le=4000)
    CHILD_CHUNK_SIZE:     int = Field(default=300, ge=50, le=1000)
    PARENT_CHUNK_OVERLAP: int = Field(default=100, ge=0)
    CHILD_CHUNK_OVERLAP:  int = Field(default=20, ge=0)

    # 文件上传
    FILE_TTL_HOURS:   int = Field(default=24, ge=1)
    BASE64_MAX_BYTES: int = Field(default=5 * 1024 * 1024, ge=0)

    # 未接 Java 网关时的固定身份（本地 / 联调自测用）：请求没带身份头时兜底用这两个值。
    # 网关接入后设 IDENTITY_DEFAULT_ENABLED=False，恢复"缺头即拒"，避免线上漏传头时静默落到默认租户。
    IDENTITY_DEFAULT_ENABLED: bool = True
    DEFAULT_TENANT_ID: int = 100
    DEFAULT_USER_ID: str = '1001'

    # 服务 / 注册
    SERVICE_NAME: str = 'rag-ultra'
    SERVICE_IP:   str = '127.0.0.1'
    HOST:         str = '127.0.0.1'
    PORT:         int = Field(default=8800, ge=1, le=65535)
    VERSION:      str = '1.0.0'

    # 多进程 worker 数（uvicorn workers）。>1 时 uvicorn 拉起多进程，
    # 每个 worker 在 FastAPI lifespan 内独立初始化自己的资源（Windows spawn 安全）。
    WORKERS:      int = Field(default=1, ge=1, le=32)

    # CORS 跨域（白名单配置化）。默认不放行任何跨域源；生产按需在 .env 配置。
    # .env 写法灵活：[*] / [a.com,b.com] / ["a.com","b.com"](JSON) / a.com,b.com / 单值均可，
    # NoDecode 跳过 JSON 强解码，交由下方 _split_csv 归一为 list[str]。
    CORS_ALLOW_ORIGINS: Annotated[list[str], NoDecode] = []     # 允许的源；[*] 或 ["*"] 放行全部（勿与凭证同时用）
    CORS_ALLOW_METHODS: Annotated[list[str], NoDecode] = ['*']  # 允许的 HTTP 方法；[*] 全部
    CORS_ALLOW_HEADERS: Annotated[list[str], NoDecode] = ['*']  # 允许的请求头；[*] 全部
    CORS_ALLOW_CREDENTIALS: bool = False        # 是否允许携带 Cookie/凭证

    # OpenAPI 文档开关（/docs、/redoc、/openapi.json）；默认关闭，开发环境在 .env 设 OPENAPI_ENABLED=True 开启
    OPENAPI_ENABLED: bool = False

    # 源文件存储（当前实现 MinIO；新增其他对象存储时实现 FileProvider 抽象并在 build_file_provider 注册）
    MINIO_ENABLED:  bool = True
    MINIO_ENDPOINT: str = 'http://localhost:9000'
    MINIO_BUCKET:   str = 'rag-ultra'
    # 默认值对齐 docker-compose 中 MinIO 容器账号；生产请在 .env 覆盖
    MINIO_ACCESS_KEY: str = 'minioadmin'
    MINIO_SECRET_KEY: str = ''

    # 文件对象路径规则（核心代码从配置读取，避免写死；默认与原有 t{租户}/base/yyyymm/uuid.ext 一致）
    FILE_OBJECT_ROOT:        str = 'rag-files/'        # 所有对象统一根前缀（可选），如 'rag-files/'
    FILE_TENANT_PREFIX_TMPL: str = 't{tenant_id}/'     # 租户隔离前缀模板，用 .format(tenant_id=...) 渲染
    FILE_USE_YEARMONTH_DIR:  bool = True               # 是否按 年/月 分子目录

    # 服务注册（nacos，仅服务发现与注册）
    NACOS_ENABLED:     bool = True
    NACOS_SERVER_ADDR: str = 'http://localhost:8848'
    NACOS_NAMESPACE:   str = 'public'
    NACOS_GROUP:       str = 'DEFAULT_GROUP'
    # 注册失败是否阻断启动：False=仅告警并继续提供服务（Nacos 不可用 / 版本不兼容时不拖垮服务）；
    # True=严格 fail-fast（注册不上就退出，避免"服务在跑但网关发现不了"）
    NACOS_DISCOVER_STRICT: bool = True

    # 配置中心（nacos，独立命名空间，与"服务发现"的 NACOS_* 变量分离）
    NACOS_CONFIG_ENABLED:     bool = True
    NACOS_CONFIG_SERVER_ADDR: str = 'http://localhost:8848'
    NACOS_CONFIG_NAMESPACE:   str = '394fd817-95d4-42aa-9479-d9df9229ea5f'
    NACOS_CONFIG_GROUP:       str = ConfigGroup.SYSTEM.value   # 兜底组：调用方不指定 group 时回退到此值
    NACOS_CONFIG_USER:        str = ''         # 当前 Nacos 无鉴权，留空占位；开启鉴权后填账号
    NACOS_CONFIG_PASSWORD:    str = ''         # 当前 Nacos 无鉴权，留空占位；开启鉴权后填密码

    LOG_FILE:              Path = BASE_DIR / 'logs/app.log'
    LOG_SIZE:              int = 1024 ** 3
    LOG_FILE_DAYS:         int = 7
    # 统一日志等级：控制台与文件共用，uvicorn 亦取此值（.lower()）。可选 DEBUG/INFO/WARNING/ERROR
    LOG_LEVEL:             str = 'INFO'

    CONTACT_TEL: str = '400-888-6666'

    @field_validator('CORS_ALLOW_ORIGINS', 'CORS_ALLOW_METHODS', 'CORS_ALLOW_HEADERS', 'MCP_API_EXCLUDE', 'API_LOG_EXCLUDE', 'AUTH_HMAC_EXCLUDE', mode='before')
    @classmethod
    def _split_csv(cls, v):
        # NoDecode 下 .env 原样传入字符串；这里归一为 list[str]：
        # 支持 [*] / [a,b] / ["a","b"](JSON) / a,b / 单值；已是列表则原样返回。
        if isinstance(v, str):
            s = v.strip()
            if s.startswith('[') and s.endswith(']'):
                import json
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return parsed
                except Exception:  # noqa: BLE001
                    pass
                inner = s[1:-1]
                return [x.strip().strip('"').strip("'") for x in inner.split(',') if x.strip()]
            return [x.strip() for x in s.split(',') if x.strip()]
        return v

    # 通过 model_config 加载配置（Pydantic V2 官方文档推荐的最佳实践）
    # 注意：当同一个配置项在不同地方重复定义时，Pydantic 会按以下优先级从高到低覆盖
    # 1. 显式传入的参数：Config(POSTGRES_PORT=5433)
    # 2. 操作系统环境变量：export POSTGRES_PORT=5434
    # 3. 配置文件中定义的变量：.env --> POSTGRES_PORT=5432
    # 4. 类属性默认值：POSTGRES_PORT: int = 5432
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / '.env'),
        env_file_encoding='utf-8',
        extra='ignore'
    )


config = Config()

-- ============================================================
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

-- psql 下切到该库后执行建表：\connect rag_ultra


-- ==========================================================
-- 1) 租户级业务表（均带 tenant_id 做租户隔离）
-- ==========================================================

CREATE TABLE rag_document (
  id BIGSERIAL NOT NULL,
  tenant_id BIGINT NOT NULL,
  file_upload_id BIGINT NOT NULL,
  doc_type SMALLINT NOT NULL,
  file_hash VARCHAR(64),
  status SMALLINT NOT NULL,
  chunk_count INTEGER NOT NULL,
  err_msg VARCHAR(512),
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id)
);
COMMENT ON TABLE rag_document IS '文档解析/索引记录（点击解析时创建，经 file_upload_id 引用上传记录，不重复文件元数据）';
COMMENT ON COLUMN rag_document.id IS '主键';
COMMENT ON COLUMN rag_document.tenant_id IS '租户ID';
COMMENT ON COLUMN rag_document.file_upload_id IS '来源上传记录ID（rag_file_upload.id）';
COMMENT ON COLUMN rag_document.doc_type IS '文档类型（0=普通文档 1=FAQ来源；对应切片 scope）';
COMMENT ON COLUMN rag_document.file_hash IS '解析时文件内容哈希快照（幂等/去重）';
COMMENT ON COLUMN rag_document.status IS '解析状态（0=待解析 1=解析中 2=已完成 3=失败）';
COMMENT ON COLUMN rag_document.chunk_count IS '生成切片数';
COMMENT ON COLUMN rag_document.err_msg IS '解析错误信息';
COMMENT ON COLUMN rag_document.created_at IS '创建时间';
COMMENT ON COLUMN rag_document.updated_at IS '更新时间';
CREATE INDEX idx_file_upload ON rag_document (file_upload_id);
CREATE INDEX idx_tenant_status ON rag_document (tenant_id, status);

CREATE TABLE rag_chunk (
  id BIGSERIAL NOT NULL,
  tenant_id BIGINT NOT NULL,
  doc_id BIGINT NOT NULL,
  scope SMALLINT NOT NULL,
  ref_id BIGINT NOT NULL,
  seq INTEGER NOT NULL,
  content TEXT NOT NULL,
  token_count INTEGER NOT NULL,
  meta JSON NOT NULL,
  enabled SMALLINT NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id)
);
COMMENT ON TABLE rag_chunk IS '切片（文档/FAQ 切分后的最小检索单元；scope 区分 doc/faq）';
COMMENT ON COLUMN rag_chunk.id IS '主键';
COMMENT ON COLUMN rag_chunk.tenant_id IS '租户ID';
COMMENT ON COLUMN rag_chunk.doc_id IS '所属解析记录ID（rag_document.id）';
COMMENT ON COLUMN rag_chunk.scope IS '范围（0=doc 1=faq）';
COMMENT ON COLUMN rag_chunk.ref_id IS '关联ID（scope=1 时指向 rag_faq.id）';
COMMENT ON COLUMN rag_chunk.seq IS '序号';
COMMENT ON COLUMN rag_chunk.content IS '切片内容';
COMMENT ON COLUMN rag_chunk.token_count IS 'token 数';
COMMENT ON COLUMN rag_chunk.meta IS '元数据（JSON）';
COMMENT ON COLUMN rag_chunk.enabled IS '是否启用（1=启用 0=停用）';
COMMENT ON COLUMN rag_chunk.created_at IS '创建时间';
COMMENT ON COLUMN rag_chunk.updated_at IS '更新时间';
CREATE INDEX idx_doc ON rag_chunk (tenant_id, doc_id);

CREATE TABLE rag_index_task (
  id BIGSERIAL NOT NULL,
  tenant_id BIGINT NOT NULL,
  doc_id BIGINT NOT NULL,
  stage VARCHAR(16) NOT NULL,
  status SMALLINT NOT NULL,
  retry_count INTEGER NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id)
);
COMMENT ON TABLE rag_index_task IS '索引任务（文档解析/嵌入入库的异步任务）';
COMMENT ON COLUMN rag_index_task.id IS '主键';
COMMENT ON COLUMN rag_index_task.tenant_id IS '租户ID';
COMMENT ON COLUMN rag_index_task.doc_id IS '关联解析记录ID（rag_document.id）';
COMMENT ON COLUMN rag_index_task.stage IS '阶段（如 parse/embed）';
COMMENT ON COLUMN rag_index_task.status IS '状态';
COMMENT ON COLUMN rag_index_task.retry_count IS '重试次数';
COMMENT ON COLUMN rag_index_task.created_at IS '创建时间';
COMMENT ON COLUMN rag_index_task.updated_at IS '更新时间';
CREATE INDEX idx_status ON rag_index_task (status, stage);

CREATE TABLE rag_faq (
  id BIGSERIAL NOT NULL,
  tenant_id BIGINT NOT NULL,
  question VARCHAR(512) NOT NULL,
  answer TEXT NOT NULL,
  similar_questions TEXT NOT NULL,
  question_type SMALLINT NOT NULL,
  enabled SMALLINT NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id)
);
COMMENT ON TABLE rag_faq IS '每租户高频问题表（MySQL+Redis 字面匹配；question_type 为意图训练数据源）';
COMMENT ON COLUMN rag_faq.id IS '主键';
COMMENT ON COLUMN rag_faq.tenant_id IS '租户ID';
COMMENT ON COLUMN rag_faq.question IS '标准问题';
COMMENT ON COLUMN rag_faq.answer IS '标准答案';
COMMENT ON COLUMN rag_faq.similar_questions IS '相似问题（多行文本，命中候选）';
COMMENT ON COLUMN rag_faq.question_type IS '问题类型（0=通用知识 1=专业咨询，与意图分类器标签对齐）';
COMMENT ON COLUMN rag_faq.enabled IS '是否启用（1=启用 0=停用）';
COMMENT ON COLUMN rag_faq.created_at IS '创建时间';
COMMENT ON COLUMN rag_faq.updated_at IS '更新时间';
CREATE INDEX idx_tenant_enabled ON rag_faq (tenant_id, enabled);
CREATE INDEX idx_tenant_type ON rag_faq (tenant_id, question_type);

CREATE TABLE rag_file (
  id BIGSERIAL NOT NULL,
  tenant_id BIGINT NOT NULL,
  base_key VARCHAR(512) NOT NULL,
  file_name VARCHAR(256),
  file_type VARCHAR(16),
  create_user_id VARCHAR(64) NOT NULL,
  current_version INTEGER NOT NULL,
  current_oss_key VARCHAR(512),
  current_file_size BIGINT,
  current_file_hash VARCHAR(64),
  version_count INTEGER NOT NULL,
  status SMALLINT NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uk_tenant_base UNIQUE (tenant_id, base_key)
);
COMMENT ON TABLE rag_file IS '文档/文件列表（一个 base_key 一行，冗余当前版本；版本历史见 rag_file_version）';
COMMENT ON COLUMN rag_file.id IS '主键';
COMMENT ON COLUMN rag_file.tenant_id IS '租户ID';
COMMENT ON COLUMN rag_file.base_key IS '文档业务键（同文档多版本共用）';
COMMENT ON COLUMN rag_file.file_name IS '当前版本文件名';
COMMENT ON COLUMN rag_file.file_type IS '当前版本文件类型';
COMMENT ON COLUMN rag_file.create_user_id IS '创建人（首个上传者 user_id）';
COMMENT ON COLUMN rag_file.current_version IS '当前版本号';
COMMENT ON COLUMN rag_file.current_oss_key IS '当前版本对象存储 key';
COMMENT ON COLUMN rag_file.current_file_size IS '当前版本文件大小（字节）';
COMMENT ON COLUMN rag_file.current_file_hash IS '当前版本内容哈希';
COMMENT ON COLUMN rag_file.version_count IS '版本总数';
COMMENT ON COLUMN rag_file.status IS '状态（1=有效 0=删除）';
COMMENT ON COLUMN rag_file.created_at IS '创建时间';
COMMENT ON COLUMN rag_file.updated_at IS '更新时间';
CREATE INDEX idx_tenant_status ON rag_file (tenant_id, status, updated_at);

CREATE TABLE rag_file_version (
  id BIGSERIAL NOT NULL,
  tenant_id BIGINT NOT NULL,
  file_id BIGINT NOT NULL,
  base_key VARCHAR(512) NOT NULL,
  version INTEGER NOT NULL,
  oss_key VARCHAR(512) NOT NULL,
  source_path VARCHAR(512),
  file_name VARCHAR(256),
  file_type VARCHAR(16),
  file_size BIGINT,
  file_hash VARCHAR(64) NOT NULL,
  upload_user_id VARCHAR(64) NOT NULL,
  status SMALLINT NOT NULL,
  expires_at TIMESTAMP WITHOUT TIME ZONE,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uk_file_version UNIQUE (file_id, version),
  CONSTRAINT uk_file_hash UNIQUE (file_id, file_hash)
);
COMMENT ON TABLE rag_file_version IS '文件版本历史（一个版本一行；同文档同内容唯一，幂等去重）';
COMMENT ON COLUMN rag_file_version.id IS '主键';
COMMENT ON COLUMN rag_file_version.tenant_id IS '租户ID';
COMMENT ON COLUMN rag_file_version.file_id IS '所属文档ID（rag_file.id）';
COMMENT ON COLUMN rag_file_version.base_key IS '文档业务键（冗余便查）';
COMMENT ON COLUMN rag_file_version.version IS '版本号';
COMMENT ON COLUMN rag_file_version.oss_key IS '该版本对象存储 key（含版本，不覆盖）';
COMMENT ON COLUMN rag_file_version.source_path IS '原始来源路径';
COMMENT ON COLUMN rag_file_version.file_name IS '文件名';
COMMENT ON COLUMN rag_file_version.file_type IS '文件类型';
COMMENT ON COLUMN rag_file_version.file_size IS '文件大小（字节）';
COMMENT ON COLUMN rag_file_version.file_hash IS '内容哈希（同文档内容去重/幂等）';
COMMENT ON COLUMN rag_file_version.upload_user_id IS '该版本上传人 user_id';
COMMENT ON COLUMN rag_file_version.status IS '状态（1=有效）';
COMMENT ON COLUMN rag_file_version.expires_at IS '过期时间';
COMMENT ON COLUMN rag_file_version.created_at IS '创建时间';
COMMENT ON COLUMN rag_file_version.updated_at IS '更新时间';
CREATE INDEX idx_tenant_base ON rag_file_version (tenant_id, base_key);


-- ==========================================================
-- 2) 用户级表（按 user_id 隔离；rag_turn 另带 tenant_id 仅统计用）
-- ==========================================================

CREATE TABLE rag_session (
  id BIGSERIAL NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(64) NOT NULL,
  title VARCHAR(255) NOT NULL,
  preview VARCHAR(512) NOT NULL,
  memory_summary VARCHAR(512) NOT NULL,
  turn_count INTEGER NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uk_user_session UNIQUE (user_id, session_id)
);
COMMENT ON TABLE rag_session IS '会话注册表（列表/元数据持久化；对话原文存 Redis）';
COMMENT ON COLUMN rag_session.id IS '主键';
COMMENT ON COLUMN rag_session.user_id IS '用户ID';
COMMENT ON COLUMN rag_session.session_id IS '会话ID';
COMMENT ON COLUMN rag_session.title IS '会话标题';
COMMENT ON COLUMN rag_session.preview IS '最近一条预览';
COMMENT ON COLUMN rag_session.memory_summary IS '记忆摘要';
COMMENT ON COLUMN rag_session.turn_count IS '对话轮数';
COMMENT ON COLUMN rag_session.created_at IS '创建时间';
COMMENT ON COLUMN rag_session.updated_at IS '更新时间';
CREATE INDEX idx_user ON rag_session (user_id);

CREATE TABLE rag_turn (
  id BIGSERIAL NOT NULL,
  turn_id VARCHAR(32) NOT NULL,
  user_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(64) NOT NULL,
  tenant_id BIGINT NOT NULL,
  role VARCHAR(16) NOT NULL,
  content TEXT NOT NULL,
  intent VARCHAR(32),
  source VARCHAR(16),
  strategy VARCHAR(32),
  model VARCHAR(64),
  chunks_json TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  latency_ms INTEGER,
  compress_method VARCHAR(16),
  compress_dropped INTEGER,
  eval_flag INTEGER NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id),
  CONSTRAINT uk_turn_role UNIQUE (turn_id, role)
);
COMMENT ON TABLE rag_turn IS '问答明细（一轮两条：user/assistant；与 Redis 会话原文 1:1）';
COMMENT ON COLUMN rag_turn.id IS '主键';
COMMENT ON COLUMN rag_turn.turn_id IS '一轮问答ID（同轮两条共享）';
COMMENT ON COLUMN rag_turn.user_id IS '用户ID';
COMMENT ON COLUMN rag_turn.session_id IS '会话ID';
COMMENT ON COLUMN rag_turn.tenant_id IS '租户ID（仅统计）';
COMMENT ON COLUMN rag_turn.role IS '消息角色：user|assistant';
COMMENT ON COLUMN rag_turn.content IS '消息正文';
COMMENT ON COLUMN rag_turn.intent IS '意图：通用知识|专业咨询';
COMMENT ON COLUMN rag_turn.source IS '产出来源：cache|faq|rag|empty';
COMMENT ON COLUMN rag_turn.strategy IS '检索策略';
COMMENT ON COLUMN rag_turn.model IS '生成模型';
COMMENT ON COLUMN rag_turn.chunks_json IS '召回片段引用 JSON';
COMMENT ON COLUMN rag_turn.prompt_tokens IS '提示词 token';
COMMENT ON COLUMN rag_turn.completion_tokens IS '生成 token';
COMMENT ON COLUMN rag_turn.latency_ms IS '端到端耗时';
COMMENT ON COLUMN rag_turn.compress_method IS '上下文压缩方式';
COMMENT ON COLUMN rag_turn.compress_dropped IS '压缩丢弃片段数';
COMMENT ON COLUMN rag_turn.eval_flag IS '评测采样标记';
COMMENT ON COLUMN rag_turn.created_at IS '创建时间';
CREATE INDEX idx_created ON rag_turn (created_at);
CREATE INDEX idx_tenant_created ON rag_turn (tenant_id, created_at);
CREATE INDEX idx_user_session ON rag_turn (user_id, session_id);


-- ==========================================================
-- 3) 系统级表（无 tenant_id；配置审批流，复用 Nacos）
-- ==========================================================

CREATE TABLE rag_config_change_request (
  id BIGSERIAL NOT NULL,
  namespace VARCHAR(128) NOT NULL,
  group_id VARCHAR(128) NOT NULL,
  data_id VARCHAR(256) NOT NULL,
  domain VARCHAR(32) NOT NULL,
  op_type SMALLINT NOT NULL,
  title VARCHAR(256),
  content_new TEXT,
  old_config_id BIGINT,
  old_config_md5 VARCHAR(64),
  old_history_nid BIGINT,
  content_format VARCHAR(16) NOT NULL,
  src_history_nid BIGINT,
  status SMALLINT NOT NULL,
  applicant VARCHAR(64),
  reviewer VARCHAR(64),
  reason VARCHAR(512),
  nacos_history_nid BIGINT,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id)
);
COMMENT ON TABLE rag_config_change_request IS '配置变更申请';
COMMENT ON COLUMN rag_config_change_request.id IS '主键';
COMMENT ON COLUMN rag_config_change_request.namespace IS 'Nacos 命名空间（取 config.NACOS_CONFIG_NAMESPACE）';
COMMENT ON COLUMN rag_config_change_request.group_id IS 'Nacos 分组';
COMMENT ON COLUMN rag_config_change_request.data_id IS 'Nacos 配置ID';
COMMENT ON COLUMN rag_config_change_request.domain IS '业务域（如 prompt）';
COMMENT ON COLUMN rag_config_change_request.op_type IS '操作类型（1=新增 2=修改 3=删除 4=回滚）';
COMMENT ON COLUMN rag_config_change_request.title IS '标题';
COMMENT ON COLUMN rag_config_change_request.content_new IS '拟下发内容（全文，审批前未进 Nacos）';
COMMENT ON COLUMN rag_config_change_request.old_config_id IS '变更前 Nacos config_info.id（CAS 基准引用）';
COMMENT ON COLUMN rag_config_change_request.old_config_md5 IS '申请时 Nacos 当前 md5（CAS 基准）';
COMMENT ON COLUMN rag_config_change_request.old_history_nid IS '执行后回填：变更前内容归档历史 nid';
COMMENT ON COLUMN rag_config_change_request.content_format IS '内容格式（json/text/yaml）';
COMMENT ON COLUMN rag_config_change_request.src_history_nid IS '回滚来源历史 nid';
COMMENT ON COLUMN rag_config_change_request.status IS '状态（0=待复核 1=通过 2=驳回 3=已执行）';
COMMENT ON COLUMN rag_config_change_request.applicant IS '申请人';
COMMENT ON COLUMN rag_config_change_request.reviewer IS '复核人';
COMMENT ON COLUMN rag_config_change_request.reason IS '变更原因';
COMMENT ON COLUMN rag_config_change_request.nacos_history_nid IS '执行后回填：新内容历史 nid';
COMMENT ON COLUMN rag_config_change_request.created_at IS '创建时间';
COMMENT ON COLUMN rag_config_change_request.updated_at IS '更新时间';
CREATE INDEX idx_data ON rag_config_change_request (namespace, group_id, data_id);
CREATE INDEX idx_domain ON rag_config_change_request (domain);
CREATE INDEX idx_status ON rag_config_change_request (status);

CREATE TABLE rag_config_review (
  id BIGSERIAL NOT NULL,
  request_id BIGINT NOT NULL,
  reviewer VARCHAR(64),
  action SMALLINT NOT NULL,
  comment VARCHAR(512),
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id)
);
COMMENT ON TABLE rag_config_review IS '配置复核记录';
COMMENT ON COLUMN rag_config_review.id IS '主键';
COMMENT ON COLUMN rag_config_review.request_id IS '关联申请ID';
COMMENT ON COLUMN rag_config_review.reviewer IS '复核人';
COMMENT ON COLUMN rag_config_review.action IS '动作（1=通过 2=驳回）';
COMMENT ON COLUMN rag_config_review.comment IS '复核意见';
COMMENT ON COLUMN rag_config_review.created_at IS '创建时间';
COMMENT ON COLUMN rag_config_review.updated_at IS '更新时间';
CREATE INDEX idx_request ON rag_config_review (request_id);


-- ==========================================================
-- 4) 系统级日志表（无 tenant_id；API 调用审计）
-- ==========================================================

CREATE TABLE rag_api_call_log (
  id BIGSERIAL NOT NULL,
  trace_id VARCHAR(64),
  caller VARCHAR(128),
  user_id VARCHAR(64),
  tenant_id BIGINT,
  method VARCHAR(16) NOT NULL,
  path VARCHAR(512) NOT NULL,
  route VARCHAR(512),
  query_string VARCHAR(1024),
  req_headers JSON,
  req_body TEXT,
  req_time TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  status_code INTEGER,
  resp_body TEXT,
  resp_time TIMESTAMP WITHOUT TIME ZONE,
  cost_ms INTEGER NOT NULL,
  created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
  PRIMARY KEY (id)
);
COMMENT ON TABLE rag_api_call_log IS 'API 调用日志（一条请求一行：请求侧 + 响应侧 + 耗时）';
COMMENT ON COLUMN rag_api_call_log.id IS '主键';
COMMENT ON COLUMN rag_api_call_log.trace_id IS '链路追踪ID（X-Trace-Id）';
COMMENT ON COLUMN rag_api_call_log.caller IS '调用方（X-Forwarded-For 首段 / client host）';
COMMENT ON COLUMN rag_api_call_log.user_id IS '用户ID（X-User-Id，网关下发）';
COMMENT ON COLUMN rag_api_call_log.tenant_id IS '租户ID（X-Tenant-Id，可为空）';
COMMENT ON COLUMN rag_api_call_log.method IS 'HTTP 方法（GET/POST/...）';
COMMENT ON COLUMN rag_api_call_log.path IS '请求路径（不含 query）';
COMMENT ON COLUMN rag_api_call_log.route IS '匹配路由模板（如 /admin/files/{oss_key}）';
COMMENT ON COLUMN rag_api_call_log.query_string IS '查询字符串';
COMMENT ON COLUMN rag_api_call_log.req_headers IS '请求头（JSON；敏感头已脱敏）';
COMMENT ON COLUMN rag_api_call_log.req_body IS '请求体（POST 记 JSON；multipart/超长不记录）';
COMMENT ON COLUMN rag_api_call_log.req_time IS '请求时间';
COMMENT ON COLUMN rag_api_call_log.status_code IS 'HTTP 状态码';
COMMENT ON COLUMN rag_api_call_log.resp_body IS '响应体（JSON；超长截断）';
COMMENT ON COLUMN rag_api_call_log.resp_time IS '响应时间';
COMMENT ON COLUMN rag_api_call_log.cost_ms IS '耗时（毫秒）';
COMMENT ON COLUMN rag_api_call_log.created_at IS '创建时间';
CREATE INDEX idx_path_time ON rag_api_call_log (path, req_time);
CREATE INDEX idx_req_time ON rag_api_call_log (req_time);
CREATE INDEX idx_trace ON rag_api_call_log (trace_id);

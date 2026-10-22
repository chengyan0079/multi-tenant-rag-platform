-- ============================================================
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


-- ==========================================================
-- 1) 租户级业务表（均带 tenant_id 做租户隔离）
-- ==========================================================

CREATE TABLE rag_document (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  tenant_id BIGINT NOT NULL COMMENT '租户ID',
  file_upload_id BIGINT NOT NULL COMMENT '来源上传记录ID（rag_file_upload.id）',
  doc_type SMALLINT NOT NULL COMMENT '文档类型（0=普通文档 1=FAQ来源；对应切片 scope）',
  file_hash VARCHAR(64) COMMENT '解析时文件内容哈希快照（幂等/去重）',
  status SMALLINT NOT NULL COMMENT '解析状态（0=待解析 1=解析中 2=已完成 3=失败）',
  chunk_count INTEGER NOT NULL COMMENT '生成切片数',
  err_msg VARCHAR(512) COMMENT '解析错误信息',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文档解析/索引记录（点击解析时创建，经 file_upload_id 引用上传记录，不重复文件元数据）';
CREATE INDEX idx_file_upload ON rag_document (file_upload_id);
CREATE INDEX idx_tenant_status ON rag_document (tenant_id, status);

CREATE TABLE rag_chunk (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  tenant_id BIGINT NOT NULL COMMENT '租户ID',
  doc_id BIGINT NOT NULL COMMENT '所属解析记录ID（rag_document.id）',
  scope SMALLINT NOT NULL COMMENT '范围（0=doc 1=faq）',
  ref_id BIGINT NOT NULL COMMENT '关联ID（scope=1 时指向 rag_faq.id）',
  seq INTEGER NOT NULL COMMENT '序号',
  content TEXT NOT NULL COMMENT '切片内容',
  token_count INTEGER NOT NULL COMMENT 'token 数',
  meta JSON NOT NULL COMMENT '元数据（JSON）',
  enabled SMALLINT NOT NULL COMMENT '是否启用（1=启用 0=停用）',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='切片（文档/FAQ 切分后的最小检索单元；scope 区分 doc/faq）';
CREATE INDEX idx_doc ON rag_chunk (tenant_id, doc_id);

CREATE TABLE rag_index_task (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  tenant_id BIGINT NOT NULL COMMENT '租户ID',
  doc_id BIGINT NOT NULL COMMENT '关联解析记录ID（rag_document.id）',
  stage VARCHAR(16) NOT NULL COMMENT '阶段（如 parse/embed）',
  status SMALLINT NOT NULL COMMENT '状态',
  retry_count INTEGER NOT NULL COMMENT '重试次数',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='索引任务（文档解析/嵌入入库的异步任务）';
CREATE INDEX idx_status ON rag_index_task (status, stage);

CREATE TABLE rag_faq (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  tenant_id BIGINT NOT NULL COMMENT '租户ID',
  question VARCHAR(512) NOT NULL COMMENT '标准问题',
  answer TEXT NOT NULL COMMENT '标准答案',
  similar_questions TEXT NOT NULL COMMENT '相似问题（多行文本，命中候选）',
  question_type SMALLINT NOT NULL COMMENT '问题类型（0=通用知识 1=专业咨询，与意图分类器标签对齐）',
  enabled SMALLINT NOT NULL COMMENT '是否启用（1=启用 0=停用）',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='每租户高频问题表（MySQL+Redis 字面匹配；question_type 为意图训练数据源）';
CREATE INDEX idx_tenant_enabled ON rag_faq (tenant_id, enabled);
CREATE INDEX idx_tenant_type ON rag_faq (tenant_id, question_type);

CREATE TABLE rag_file (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  tenant_id BIGINT NOT NULL COMMENT '租户ID',
  base_key VARCHAR(512) NOT NULL COMMENT '文档业务键（同文档多版本共用）',
  file_name VARCHAR(256) COMMENT '当前版本文件名',
  file_type VARCHAR(16) COMMENT '当前版本文件类型',
  create_user_id VARCHAR(64) NOT NULL COMMENT '创建人（首个上传者 user_id）',
  current_version INTEGER NOT NULL COMMENT '当前版本号',
  current_oss_key VARCHAR(512) COMMENT '当前版本对象存储 key',
  current_file_size BIGINT COMMENT '当前版本文件大小（字节）',
  current_file_hash VARCHAR(64) COMMENT '当前版本内容哈希',
  version_count INTEGER NOT NULL COMMENT '版本总数',
  status SMALLINT NOT NULL COMMENT '状态（1=有效 0=删除）',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id),
  CONSTRAINT uk_tenant_base UNIQUE (tenant_id, base_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文档/文件列表（一个 base_key 一行，冗余当前版本；版本历史见 rag_file_version）';
CREATE INDEX idx_tenant_status ON rag_file (tenant_id, status, updated_at);

CREATE TABLE rag_file_version (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  tenant_id BIGINT NOT NULL COMMENT '租户ID',
  file_id BIGINT NOT NULL COMMENT '所属文档ID（rag_file.id）',
  base_key VARCHAR(512) NOT NULL COMMENT '文档业务键（冗余便查）',
  version INTEGER NOT NULL COMMENT '版本号',
  oss_key VARCHAR(512) NOT NULL COMMENT '该版本对象存储 key（含版本，不覆盖）',
  source_path VARCHAR(512) COMMENT '原始来源路径',
  file_name VARCHAR(256) COMMENT '文件名',
  file_type VARCHAR(16) COMMENT '文件类型',
  file_size BIGINT COMMENT '文件大小（字节）',
  file_hash VARCHAR(64) NOT NULL COMMENT '内容哈希（同文档内容去重/幂等）',
  upload_user_id VARCHAR(64) NOT NULL COMMENT '该版本上传人 user_id',
  status SMALLINT NOT NULL COMMENT '状态（1=有效）',
  expires_at DATETIME COMMENT '过期时间',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id),
  CONSTRAINT uk_file_version UNIQUE (file_id, version),
  CONSTRAINT uk_file_hash UNIQUE (file_id, file_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='文件版本历史（一个版本一行；同文档同内容唯一，幂等去重）';
CREATE INDEX idx_tenant_base ON rag_file_version (tenant_id, base_key);


-- ==========================================================
-- 2) 用户级表（仅 user_id，无 tenant_id）
-- ==========================================================

CREATE TABLE rag_session (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  user_id VARCHAR(64) NOT NULL COMMENT '用户ID',
  session_id VARCHAR(64) NOT NULL COMMENT '会话ID',
  title VARCHAR(255) NOT NULL COMMENT '会话标题',
  preview VARCHAR(512) NOT NULL COMMENT '最近一条预览',
  memory_summary VARCHAR(512) NOT NULL COMMENT '记忆摘要',
  turn_count INTEGER NOT NULL COMMENT '对话轮数',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id),
  CONSTRAINT uk_user_session UNIQUE (user_id, session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='会话注册表（列表/元数据持久化；对话原文存 Redis）';
CREATE INDEX idx_user ON rag_session (user_id);


-- ==========================================================
-- 3) 系统级表（无 tenant_id；配置审批流，复用 Nacos）
-- ==========================================================

CREATE TABLE rag_config_change_request (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  namespace VARCHAR(128) NOT NULL COMMENT 'Nacos 命名空间',
  group_id VARCHAR(128) NOT NULL COMMENT 'Nacos 分组',
  data_id VARCHAR(256) NOT NULL COMMENT 'Nacos 配置ID',
  domain VARCHAR(32) NOT NULL COMMENT '业务域（如 prompt）',
  op_type SMALLINT NOT NULL COMMENT '操作类型（1=新增 2=修改 3=删除 4=回滚）',
  title VARCHAR(256) COMMENT '标题',
  content_new TEXT COMMENT '拟下发内容（全文，审批前未进 Nacos）',
  old_config_id BIGINT COMMENT '变更前 Nacos config_info.id（CAS 基准引用）',
  old_config_md5 VARCHAR(64) COMMENT '申请时 Nacos 当前 md5（CAS 基准）',
  old_history_nid BIGINT COMMENT '执行后回填：变更前内容归档历史 nid',
  content_format VARCHAR(16) NOT NULL COMMENT '内容格式（json/text/yaml）',
  src_history_nid BIGINT COMMENT '回滚来源历史 nid',
  status SMALLINT NOT NULL COMMENT '状态（0=待复核 1=通过 2=驳回 3=已执行）',
  applicant VARCHAR(64) COMMENT '申请人',
  reviewer VARCHAR(64) COMMENT '复核人',
  reason VARCHAR(512) COMMENT '变更原因',
  nacos_history_nid BIGINT COMMENT '执行后回填：新内容历史 nid',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='配置变更申请';
CREATE INDEX idx_data ON rag_config_change_request (namespace, group_id, data_id);
CREATE INDEX idx_domain ON rag_config_change_request (domain);
CREATE INDEX idx_status ON rag_config_change_request (status);

CREATE TABLE rag_config_review (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  request_id BIGINT NOT NULL COMMENT '关联申请ID',
  reviewer VARCHAR(64) COMMENT '复核人',
  action SMALLINT NOT NULL COMMENT '动作（1=通过 2=驳回）',
  comment VARCHAR(512) COMMENT '复核意见',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  updated_at DATETIME NOT NULL COMMENT '更新时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='配置复核记录';
CREATE INDEX idx_request ON rag_config_review (request_id);


-- ==========================================================
-- 4) 系统级日志表（无 tenant_id；API 调用审计）
-- ==========================================================

CREATE TABLE rag_api_call_log (
  id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT,
  trace_id VARCHAR(64) COMMENT '链路追踪ID（X-Trace-Id）',
  caller VARCHAR(128) COMMENT '调用方（X-Forwarded-For 首段 / client host）',
  user_id VARCHAR(64) COMMENT '用户ID（X-User-Id，网关下发）',
  tenant_id BIGINT COMMENT '租户ID（X-Tenant-Id，可为空）',
  method VARCHAR(16) NOT NULL COMMENT 'HTTP 方法（GET/POST/...）',
  path VARCHAR(512) NOT NULL COMMENT '请求路径（不含 query）',
  route VARCHAR(512) COMMENT '匹配路由模板（如 /admin/files/{oss_key}）',
  query_string VARCHAR(1024) COMMENT '查询字符串',
  req_headers JSON COMMENT '请求头（JSON；敏感头已脱敏）',
  req_body TEXT COMMENT '请求体（POST 记 JSON；multipart/超长不记录）',
  req_time DATETIME NOT NULL COMMENT '请求时间',
  status_code INTEGER COMMENT 'HTTP 状态码',
  resp_body TEXT COMMENT '响应体（JSON；超长截断）',
  resp_time DATETIME COMMENT '响应时间',
  cost_ms INTEGER NOT NULL COMMENT '耗时（毫秒）',
  created_at DATETIME NOT NULL COMMENT '创建时间',
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='API 调用日志（一条请求一行：请求侧 + 响应侧 + 耗时）';
CREATE INDEX idx_path_time ON rag_api_call_log (path, req_time);
CREATE INDEX idx_req_time ON rag_api_call_log (req_time);
CREATE INDEX idx_trace ON rag_api_call_log (trace_id);

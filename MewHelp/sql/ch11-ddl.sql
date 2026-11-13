-- ch11 · 失败案例诊断
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS diagnosis_cases (
  id                  BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  case_id             VARCHAR(36) NOT NULL COMMENT '应用级案例 ID,可在 Langfuse metadata 中搜索',
  trace_id            VARCHAR(64) NULL COMMENT '可选 Langfuse Trace ID;不可用时保留 NULL',
  conversation_id     BIGINT UNSIGNED NULL,
  question            TEXT NOT NULL,
  intent              VARCHAR(64) NULL,
  failure_type        VARCHAR(32) NOT NULL DEFAULT 'unknown',
  predicted_failure_type VARCHAR(32) NOT NULL DEFAULT 'unknown',
  human_failure_type  VARCHAR(32) NULL,
  diagnosis_status    VARCHAR(32) NOT NULL DEFAULT 'pending',
  diagnosis_reason    TEXT NOT NULL,
  retrieved_docs      JSON NULL COMMENT '结构化候选文档 ID、名次和分数',
  evidence_confidence FLOAT NULL,
  selfcheck_result    JSON NULL,
  tool_status         JSON NULL,
  final_answer        TEXT NULL,
  user_feedback       VARCHAR(8) NULL,
  config_version      VARCHAR(128) NULL,
  created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_diagnosis_case_id (case_id),
  KEY idx_diagnosis_status (diagnosis_status),
  KEY idx_diagnosis_conversation (conversation_id),
  KEY idx_diagnosis_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='需复核的客服失败案例';

-- Durable logical task state and per-request invocation audit for the
-- self-evolution flywheel. Apply after ch09-ddl.sql.
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS flywheel_tasks (
  task_id                 VARCHAR(64) NOT NULL,
  task_type               VARCHAR(32) NOT NULL,
  input_fingerprint       CHAR(64) NOT NULL,
  analysis_config_version CHAR(64) NOT NULL,
  output_schema_version   VARCHAR(32) NOT NULL,
  execution_status        VARCHAR(16) NOT NULL DEFAULT 'pending',
  attempts                INT UNSIGNED NOT NULL DEFAULT 0,
  result_json             JSON NULL,
  result_expires_at       DATETIME NULL,
  last_error_type         VARCHAR(128) NULL,
  created_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at              DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                      ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (task_id),
  KEY idx_flywheel_task_status (execution_status, updated_at),
  KEY idx_flywheel_task_type (task_type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Self-evolution logical task states';

CREATE TABLE IF NOT EXISTS flywheel_invocations (
  invocation_id VARCHAR(36) NOT NULL,
  task_id       VARCHAR(64) NOT NULL,
  call_type     VARCHAR(32) NOT NULL,
  status        VARCHAR(16) NOT NULL DEFAULT 'running',
  response_hash CHAR(64) NULL,
  error_type    VARCHAR(128) NULL,
  started_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at   DATETIME NULL,
  PRIMARY KEY (invocation_id),
  KEY idx_flywheel_invocation_task (task_id, started_at),
  KEY idx_flywheel_invocation_status (status, started_at),
  CONSTRAINT fk_flywheel_invocation_task
    FOREIGN KEY (task_id) REFERENCES flywheel_tasks (task_id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Per-request model invocation audit';

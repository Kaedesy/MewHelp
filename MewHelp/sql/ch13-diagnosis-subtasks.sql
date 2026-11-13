-- Multi-fault diagnosis state. Apply once after ch11-ddl.sql.
SET NAMES utf8mb4;

ALTER TABLE diagnosis_cases
  ADD COLUMN case_diagnosis_status VARCHAR(32) NOT NULL DEFAULT 'in_progress'
  COMMENT 'Workflow-derived case summary; separate from human review diagnosis_status',
  ADD COLUMN topic_labels JSON NULL
  COMMENT 'Non-binding RoBERTa business-topic labels used for diagnostic bucketing';

CREATE TABLE IF NOT EXISTS diagnosis_tasks (
  task_id           VARCHAR(64) NOT NULL,
  case_id           VARCHAR(36) NOT NULL,
  diagnostic_scope  VARCHAR(64) NOT NULL,
  execution_status  VARCHAR(16) NOT NULL DEFAULT 'pending',
  outcome_proposed  VARCHAR(32) NULL,
  outcome_final     VARCHAR(32) NULL,
  evidence_refs     JSON NULL,
  error_type        VARCHAR(128) NULL,
  attempts          INT UNSIGNED NOT NULL DEFAULT 0,
  created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (task_id),
  KEY idx_diagnosis_task_case (case_id, diagnostic_scope),
  KEY idx_diagnosis_task_state (execution_status, outcome_final),
  CONSTRAINT fk_diagnosis_task_case FOREIGN KEY (case_id)
    REFERENCES diagnosis_cases (case_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Independent diagnostic scope state';

CREATE TABLE IF NOT EXISTS diagnosis_faults (
  fault_id             VARCHAR(64) NOT NULL,
  task_id              VARCHAR(64) NOT NULL,
  fault_type           VARCHAR(32) NOT NULL,
  status               VARCHAR(16) NOT NULL DEFAULT 'proposed',
  proposal_source      VARCHAR(16) NOT NULL,
  review_source        VARCHAR(16) NULL,
  confirmation_source  VARCHAR(16) NULL,
  evidence_refs        JSON NULL,
  reason               TEXT NOT NULL,
  created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (fault_id),
  KEY idx_diagnosis_fault_task (task_id, status),
  KEY idx_diagnosis_fault_type (fault_type, status),
  CONSTRAINT fk_diagnosis_fault_task FOREIGN KEY (task_id)
    REFERENCES diagnosis_tasks (task_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Evidence-linked fault items';

CREATE TABLE IF NOT EXISTS diagnosis_review_events (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  fault_id    VARCHAR(64) NOT NULL,
  decision    VARCHAR(16) NOT NULL,
  reviewer_id VARCHAR(128) NULL,
  comment     TEXT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_diagnosis_review_fault (fault_id, created_at),
  CONSTRAINT fk_diagnosis_review_fault FOREIGN KEY (fault_id)
    REFERENCES diagnosis_faults (fault_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Append-only diagnosis review history';

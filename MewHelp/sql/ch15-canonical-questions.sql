-- Stable canonical question identities and immutable flywheel group results.
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS canonical_questions (
  canonical_question_id VARCHAR(36) NOT NULL,
  normalized_question   VARCHAR(512) NOT NULL,
  status                VARCHAR(16) NOT NULL DEFAULT 'active',
  split_from_id         VARCHAR(36) NULL,
  created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (canonical_question_id),
  KEY idx_canonical_status (status),
  CONSTRAINT fk_canonical_split_from FOREIGN KEY (split_from_id)
    REFERENCES canonical_questions (canonical_question_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Long-lived canonical question identities';

ALTER TABLE review_queue
  ADD COLUMN canonical_question_id VARCHAR(36) NULL;

UPDATE review_queue SET canonical_question_id = UUID()
WHERE canonical_question_id IS NULL;

INSERT INTO canonical_questions (canonical_question_id, normalized_question)
SELECT canonical_question_id, normalized_question FROM review_queue;

ALTER TABLE review_queue
  MODIFY COLUMN canonical_question_id VARCHAR(36) NOT NULL,
  ADD UNIQUE KEY uk_review_canonical_question (canonical_question_id),
  ADD CONSTRAINT fk_review_canonical_question FOREIGN KEY (canonical_question_id)
    REFERENCES canonical_questions (canonical_question_id);

CREATE TABLE IF NOT EXISTS flywheel_group_results (
  group_result_id       CHAR(64) NOT NULL,
  merge_task_id         CHAR(64) NOT NULL,
  canonical_question_id VARCHAR(36) NOT NULL,
  member_case_ids      JSON NOT NULL,
  normalized_question  VARCHAR(512) NOT NULL,
  content_sha256       CHAR(64) NOT NULL,
  result_revision      INT UNSIGNED NOT NULL DEFAULT 1,
  evidence_status      VARCHAR(24) NOT NULL DEFAULT 'needs_verification',
  review_status        VARCHAR(16) NOT NULL DEFAULT 'pending',
  created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (group_result_id),
  KEY idx_group_result_task (merge_task_id),
  KEY idx_group_result_canonical (canonical_question_id, created_at),
  CONSTRAINT fk_group_result_canonical FOREIGN KEY (canonical_question_id)
    REFERENCES canonical_questions (canonical_question_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Immutable batch grouping results';

CREATE TABLE IF NOT EXISTS flywheel_pair_relations (
  relation_id        CHAR(64) NOT NULL,
  merge_task_id      CHAR(64) NOT NULL,
  case_id_a          VARCHAR(64) NOT NULL,
  case_id_b          VARCHAR(64) NOT NULL,
  relation_proposed  VARCHAR(16) NOT NULL,
  relation_final     VARCHAR(16) NULL,
  review_status      VARCHAR(16) NOT NULL DEFAULT 'pending',
  review_source      VARCHAR(16) NULL,
  confirmation_source VARCHAR(16) NULL,
  rationale          TEXT NOT NULL,
  created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                               ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (relation_id),
  UNIQUE KEY uk_pair_relation_task (merge_task_id, case_id_a, case_id_b),
  KEY idx_pair_relation_review (review_status, created_at),
  KEY idx_pair_relation_pair (case_id_a, case_id_b)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Immutable model pair proposal and human final relation';

CREATE TABLE IF NOT EXISTS flywheel_pair_review_events (
  id          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  relation_id CHAR(64) NOT NULL,
  decision    VARCHAR(16) NOT NULL,
  reviewer_id VARCHAR(128) NOT NULL,
  comment     TEXT NULL,
  created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_pair_review_relation (relation_id, created_at),
  CONSTRAINT fk_pair_review_relation FOREIGN KEY (relation_id)
    REFERENCES flywheel_pair_relations (relation_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Append-only case-pair review history';

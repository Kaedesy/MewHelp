-- ch11 · confirmed knowledge-gap diagnosis → existing review flywheel.
-- One-time migration for databases where ch11-ddl.sql was already applied.
SET NAMES utf8mb4;

ALTER TABLE low_confidence_questions
  MODIFY COLUMN source ENUM(
    'retrieval_low_conf','self_check','user_feedback','diagnosis_confirmed'
  ) NOT NULL COMMENT '入池入口:检索证据低 / 生成自评不足 / 用户反馈未解决 / 人工确认知识缺口',
  ADD COLUMN diagnosis_case_id VARCHAR(36) NULL COMMENT '来源诊断案例;每个案例最多进入一次飞轮',
  ADD UNIQUE KEY uk_lcq_diagnosis_case (diagnosis_case_id),
  ADD CONSTRAINT fk_lcq_diagnosis_case
    FOREIGN KEY (diagnosis_case_id) REFERENCES diagnosis_cases (case_id) ON DELETE SET NULL;

ALTER TABLE diagnosis_cases
  ADD COLUMN review_queue_id BIGINT UNSIGNED NULL COMMENT '归一化查重后关联的人工审核队列项',
  ADD KEY idx_diagnosis_review_queue (review_queue_id),
  ADD CONSTRAINT fk_diagnosis_review_queue
    FOREIGN KEY (review_queue_id) REFERENCES review_queue (id) ON DELETE SET NULL;

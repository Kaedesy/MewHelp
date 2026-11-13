-- Preserve malformed/noise reports without repeatedly feeding them to the flywheel.
ALTER TABLE low_confidence_questions
  ADD COLUMN processing_status VARCHAR(16) NOT NULL DEFAULT 'pending',
  ADD COLUMN processing_note VARCHAR(255) NULL,
  ADD KEY idx_lcq_processing_status (processing_status, id);

UPDATE low_confidence_questions
SET processing_status = 'queued'
WHERE matched_review_id IS NOT NULL;

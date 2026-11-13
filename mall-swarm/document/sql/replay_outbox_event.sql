-- Inspect last_error and event details before replaying a DEAD outbox event.
-- Replace 123 with the event id selected for manual replay.
UPDATE oms_outbox_event
SET status = 'RETRY',
    attempts = 0,
    next_attempt_time = NOW(),
    locked_until = NULL,
    last_error = NULL,
    update_time = NOW()
WHERE id = 123
  AND status = 'DEAD';

CREATE TABLE IF NOT EXISTS `oms_order_idempotency` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `member_id` bigint(20) NOT NULL,
  `idempotency_key` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
  `request_hash` char(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `order_id` bigint(20) DEFAULT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'PROCESSING',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_member_idempotency_key` (`member_id`, `idempotency_key`),
  KEY `idx_order_id` (`order_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='下单幂等请求记录';

CREATE TABLE IF NOT EXISTS `oms_outbox_event` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `event_type` varchar(64) NOT NULL,
  `order_id` bigint(20) NOT NULL,
  `delay_millis` bigint(20) NOT NULL,
  `status` varchar(16) NOT NULL DEFAULT 'PENDING',
  `attempts` int(11) NOT NULL DEFAULT 0,
  `next_attempt_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `locked_until` datetime DEFAULT NULL,
  `last_error` varchar(1000) DEFAULT NULL,
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_outbox_due` (`status`, `next_attempt_time`, `id`),
  KEY `idx_outbox_lease` (`status`, `locked_until`, `id`),
  KEY `idx_outbox_order` (`order_id`, `event_type`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci COMMENT='Transactional outbox events';

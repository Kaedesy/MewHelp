-- Immutable knowledge/configuration/release catalog for the self-evolution cycle.
SET NAMES utf8mb4;

CREATE TABLE IF NOT EXISTS knowledge_snapshots (
  snapshot_id      VARCHAR(64) NOT NULL,
  collection_name  VARCHAR(128) NOT NULL,
  manifest_sha256  CHAR(64) NOT NULL,
  manifest         JSON NOT NULL,
  status           VARCHAR(16) NOT NULL DEFAULT 'building',
  created_by       VARCHAR(128) NOT NULL,
  created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (snapshot_id),
  UNIQUE KEY uk_snapshot_collection (collection_name),
  KEY idx_snapshot_status_time (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Immutable knowledge index snapshots';

CREATE TABLE IF NOT EXISTS system_config_versions (
  config_version  VARCHAR(64) NOT NULL,
  config_sha256   CHAR(64) NOT NULL,
  config          JSON NOT NULL,
  created_by      VARCHAR(128) NOT NULL,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (config_version),
  UNIQUE KEY uk_config_hash (config_sha256)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Immutable runtime configuration versions';

CREATE TABLE IF NOT EXISTS evolution_releases (
  release_id      VARCHAR(36) NOT NULL,
  snapshot_id     VARCHAR(64) NOT NULL,
  config_version  VARCHAR(64) NOT NULL,
  evaluation      JSON NOT NULL,
  approved_by     VARCHAR(128) NOT NULL,
  status          VARCHAR(16) NOT NULL DEFAULT 'prepared',
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (release_id),
  KEY idx_release_snapshot (snapshot_id),
  KEY idx_release_config (config_version),
  CONSTRAINT fk_release_snapshot FOREIGN KEY (snapshot_id)
    REFERENCES knowledge_snapshots (snapshot_id),
  CONSTRAINT fk_release_config FOREIGN KEY (config_version)
    REFERENCES system_config_versions (config_version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Immutable evaluated and approved releases';

CREATE TABLE IF NOT EXISTS active_release (
  singleton_id TINYINT UNSIGNED NOT NULL,
  release_id   VARCHAR(36) NULL,
  updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (singleton_id),
  CONSTRAINT ck_active_release_singleton CHECK (singleton_id = 1),
  CONSTRAINT fk_active_release_release FOREIGN KEY (release_id)
    REFERENCES evolution_releases (release_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Current production release pointer';

CREATE TABLE IF NOT EXISTS release_events (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  from_release_id VARCHAR(36) NULL,
  to_release_id   VARCHAR(36) NOT NULL,
  event_type      VARCHAR(16) NOT NULL,
  actor           VARCHAR(128) NOT NULL,
  reason          TEXT NOT NULL,
  created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_release_event_target (to_release_id, created_at),
  KEY idx_release_event_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Append-only release and rollback audit';

BEGIN TRANSACTION;
CREATE TABLE asset_people (
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  confidence REAL,
  source TEXT NOT NULL DEFAULT 'manual', review_state TEXT NOT NULL DEFAULT 'pending', reviewed INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, model_name TEXT, model_version TEXT, producer_version TEXT, observed_at TEXT,
  PRIMARY KEY(asset_id, person_id)
);
CREATE TABLE asset_tags (
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  confidence REAL,
  source TEXT NOT NULL DEFAULT 'manual', review_state TEXT NOT NULL DEFAULT 'pending', reviewed INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, model_name TEXT, model_version TEXT, producer_version TEXT, observed_at TEXT,
  PRIMARY KEY(asset_id, tag_id)
);
CREATE TABLE assets (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  sha256 TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL,
  captured_at TEXT,
  latitude REAL,
  longitude REAL,
  width INTEGER,
  height INTEGER,
  media_type TEXT NOT NULL DEFAULT 'image',
  indexed_at TEXT NOT NULL,
  missing_at TEXT
);
CREATE TABLE enrichment_state (asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE, external_id TEXT NOT NULL, next_attempt_at TEXT NOT NULL, synced_at TEXT, error TEXT);
CREATE TABLE event_assets (
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  confidence REAL,
  PRIMARY KEY(event_id, asset_id)
);
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  starts_at TEXT,
  ends_at TEXT,
  latitude REAL,
  longitude REAL,
  level TEXT NOT NULL DEFAULT 'day',
  confirmed INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'candidate',
  created_at TEXT NOT NULL
, review_state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE external_albums (
  smart_album_id INTEGER NOT NULL REFERENCES smart_albums(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY(smart_album_id, provider),
  UNIQUE(provider, external_id)
);
CREATE TABLE external_assets (
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY(asset_id, provider),
  UNIQUE(provider, external_id)
);
CREATE TABLE external_people (external_id TEXT PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE);
CREATE TABLE external_tags (external_id TEXT PRIMARY KEY, tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE);
CREATE TABLE integration_state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE people (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  confirmed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
, name_locked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO "schema_version" VALUES(4);
CREATE TABLE smart_albums (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  query_json TEXT NOT NULL,
  materialized_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE tags (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL
);
CREATE TABLE task_status (name TEXT PRIMARY KEY, state TEXT NOT NULL, started_at TEXT, finished_at TEXT, result_json TEXT, error TEXT);
CREATE INDEX idx_assets_captured_at ON assets(captured_at);
CREATE INDEX idx_assets_sha256 ON assets(sha256);
CREATE INDEX idx_enrichment_due ON enrichment_state(next_attempt_at);
COMMIT;

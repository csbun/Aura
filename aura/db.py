from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
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
CREATE INDEX IF NOT EXISTS idx_assets_captured_at ON assets(captured_at);
CREATE INDEX IF NOT EXISTS idx_assets_sha256 ON assets(sha256);
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  confirmed INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_people (
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
  confidence REAL,
  source TEXT NOT NULL DEFAULT 'manual',
  PRIMARY KEY(asset_id, person_id)
);
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL DEFAULT 'user',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS asset_tags (
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
  confidence REAL,
  source TEXT NOT NULL DEFAULT 'manual',
  PRIMARY KEY(asset_id, tag_id)
);
CREATE TABLE IF NOT EXISTS events (
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
);
CREATE TABLE IF NOT EXISTS event_assets (
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  confidence REAL,
  PRIMARY KEY(event_id, asset_id)
);
CREATE TABLE IF NOT EXISTS smart_albums (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  query_json TEXT NOT NULL,
  materialized_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS external_assets (
  asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY(asset_id, provider),
  UNIQUE(provider, external_id)
);
CREATE TABLE IF NOT EXISTS external_albums (
  smart_album_id INTEGER NOT NULL REFERENCES smart_albums(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  external_id TEXT NOT NULL,
  synced_at TEXT NOT NULL,
  PRIMARY KEY(smart_album_id, provider),
  UNIQUE(provider, external_id)
);
"""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Database:
    """Deep storage module: owns schema creation and transaction boundaries."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            for table in ("events", "asset_tags", "asset_people"):
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                if "review_state" not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN review_state TEXT NOT NULL DEFAULT 'pending'")
                    if table == "events":
                        connection.execute("UPDATE events SET review_state = 'confirmed' WHERE confirmed = 1")
                    else:
                        connection.execute(f"UPDATE {table} SET review_state = 'confirmed' WHERE source = 'manual'")
            connection.execute("CREATE TABLE IF NOT EXISTS task_status (name TEXT PRIMARY KEY, state TEXT NOT NULL, started_at TEXT, finished_at TEXT, result_json TEXT, error TEXT)")
            for table in ("asset_people", "asset_tags"):
                columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                for name, definition in {
                    "reviewed": "INTEGER NOT NULL DEFAULT 0",
                    "active": "INTEGER NOT NULL DEFAULT 1",
                    "model_name": "TEXT", "model_version": "TEXT",
                    "producer_version": "TEXT", "observed_at": "TEXT",
                }.items():
                    if name not in columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                        if name == "reviewed":
                            connection.execute(f"UPDATE {table} SET reviewed=1 WHERE source='manual' OR review_state IN ('confirmed','rejected')")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(people)")}
            if "name_locked" not in columns:
                connection.execute("ALTER TABLE people ADD COLUMN name_locked INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE people SET name_locked=1")
            connection.execute("CREATE TABLE IF NOT EXISTS external_people (external_id TEXT PRIMARY KEY, person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE)")
            connection.execute("CREATE TABLE IF NOT EXISTS external_tags (external_id TEXT PRIMARY KEY, tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE)")
            connection.execute("CREATE TABLE IF NOT EXISTS enrichment_state (asset_id INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE, external_id TEXT NOT NULL, next_attempt_at TEXT NOT NULL, synced_at TEXT, error TEXT)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_due ON enrichment_state(next_attempt_at)")
            connection.execute("CREATE TABLE IF NOT EXISTS integration_state (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("DELETE FROM schema_version")
            connection.execute("INSERT INTO schema_version(version) VALUES (4)")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.connect() as connection:
            cursor = connection.execute(sql, tuple(params))
            connection.commit()
            return cursor

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]

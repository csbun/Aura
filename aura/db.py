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
"""


class Database:
    """Deep storage module: owns schema creation and transaction boundaries."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            if connection.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                connection.execute("INSERT INTO schema_version(version) VALUES (1)")

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self.connect() as connection:
            cursor = connection.execute(sql, tuple(params))
            connection.commit()
            return cursor

    def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(sql, tuple(params)).fetchall()]

from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 6
SCHEMA = [
    'CREATE TABLE schema_version (version INTEGER NOT NULL)',
    "CREATE TABLE settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
       starts_at TEXT, ends_at TEXT, review_state TEXT NOT NULL DEFAULT 'confirmed',
       source TEXT NOT NULL DEFAULT 'automatic', issue TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE event_assets (event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
       asset_id TEXT NOT NULL, PRIMARY KEY(event_id, asset_id))""",
    'CREATE INDEX event_assets_asset ON event_assets(asset_id)',
    """CREATE TABLE smart_albums (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
       query_json TEXT NOT NULL, remote_id TEXT UNIQUE, sync_mode TEXT NOT NULL DEFAULT 'mirror',
       migration_issue TEXT, materialized_at TEXT, matched_assets INTEGER, error TEXT,
       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    """CREATE TABLE task_status (name TEXT PRIMARY KEY, state TEXT NOT NULL, started_at TEXT,
       finished_at TEXT, result_json TEXT, error TEXT)""",
]


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        return connection

    @contextmanager
    def lock(self, name):
        with self.path.with_suffix(f'.{name}.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def initialize(self):
        with self.lock('schema'), self.connect() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = c.execute('SELECT version FROM schema_version').fetchone() if 'schema_version' in tables else None
            if version and version[0] > SCHEMA_VERSION:
                raise RuntimeError('Aura database is newer than this application; downgrade refused')
            if version and version[0] == SCHEMA_VERSION:
                return
            if version and version[0] == 5:
                c.execute('BEGIN IMMEDIATE')
                self._automatic_events(c)
                c.execute('UPDATE schema_version SET version=?', [SCHEMA_VERSION])
                return
            if tables:
                legacy_tables = {'schema_version', 'assets', 'people', 'asset_people', 'tags', 'asset_tags',
                                 'events', 'event_assets', 'smart_albums', 'external_assets', 'external_albums',
                                 'task_status', 'external_people', 'external_tags', 'enrichment_state', 'integration_state'}
                if tables - legacy_tables - {'sqlite_sequence'}:
                    raise RuntimeError('Unknown tables in legacy database; migration refused')
                if not version or version[0] not in (1, 2, 3, 4):
                    raise RuntimeError('Unrecognized Aura database; migration refused')
                # sqlite backup provides a consistent, complete archive, not a second photo copy.
                archive = self.path.with_suffix('.pre-0.4.sqlite')
                if not archive.exists():
                    partial = archive.with_suffix('.tmp')
                    with sqlite3.connect(partial) as backup:
                        c.backup(backup)
                    partial.chmod(0o600)
                    partial.replace(archive)
                from .migration import read_legacy
                events, memberships, albums, report = read_legacy(c, archive.name)
            else:
                events, memberships, albums, report = [], [], [], None
            c.execute('PRAGMA foreign_keys=OFF')
            c.execute('BEGIN IMMEDIATE')
            for name in tables - {'sqlite_sequence'}:
                c.execute('DROP TABLE "' + name.replace('"', '""') + '"')
            for statement in SCHEMA:
                c.execute(statement)
            c.execute('INSERT INTO schema_version VALUES (?)', [SCHEMA_VERSION])
            c.execute('INSERT INTO settings VALUES (?,?)', ['instance_id', str(uuid4())])
            for row in events:
                c.execute('INSERT INTO events(id,name,starts_at,ends_at,review_state,source,issue,created_at) VALUES (?,?,?,?,?,?,?,?)', row)
            c.executemany('INSERT INTO event_assets VALUES (?,?)', memberships)
            for row in albums:
                c.execute('INSERT INTO smart_albums(id,name,query_json,remote_id,sync_mode,migration_issue,created_at) VALUES (?,?,?,?,?,?,?)', row)
            if report:
                c.execute('INSERT INTO settings VALUES (?,?)', ['migration_report', json.dumps(report, ensure_ascii=False)])
            self._automatic_events(c)
            c.commit()

    @staticmethod
    def _automatic_events(c):
        # Previously confirmed events were human decisions; keep their membership fixed.
        c.execute("UPDATE events SET source='manual' WHERE review_state='confirmed' AND source='candidate'")
        c.execute("UPDATE events SET source=CASE WHEN source='candidate' THEN 'automatic' ELSE source END, review_state='confirmed' WHERE review_state='pending'")
        c.execute("UPDATE events SET review_state='deleted' WHERE review_state='rejected'")

    def execute(self, sql, params=()):
        with self.connect() as c:
            return c.execute(sql, tuple(params))

    def fetch_all(self, sql, params=()):
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, tuple(params))]

    def get_setting(self, name, default=None):
        rows = self.fetch_all('SELECT value FROM settings WHERE name=?', [name])
        return rows[0]['value'] if rows else default

    def set_setting(self, name, value):
        self.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', [name, value])

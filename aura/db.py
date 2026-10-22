from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 8
LEGACY_VERSION = 6
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

    def _initialize_v6(self):
        with self.lock('schema'), self.connect() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = c.execute('SELECT version FROM schema_version').fetchone() if 'schema_version' in tables else None
            if version and version[0] > SCHEMA_VERSION:
                raise RuntimeError('Aura database is newer than this application; downgrade refused')
            if version and version[0] >= LEGACY_VERSION:
                return
            if version and version[0] == 5:
                c.execute('BEGIN IMMEDIATE')
                self._automatic_events(c)
                c.execute('UPDATE schema_version SET version=?', [LEGACY_VERSION])
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
            c.execute('INSERT INTO schema_version VALUES (?)', [LEGACY_VERSION])
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

    def initialize(self):
        self._initialize_v6()
        with self.lock('schema'), self.connect() as c:
            version = c.execute('SELECT version FROM schema_version').fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            archive = self.path.with_suffix('.pre-0.4.3.sqlite')
            if not archive.exists():
                partial = archive.with_suffix('.tmp')
                with sqlite3.connect(partial) as backup:
                    c.backup(backup)
                partial.chmod(0o600)
                partial.replace(archive)
            c.execute('BEGIN IMMEDIATE')
            if version == 6:
                c.execute('ALTER TABLE events RENAME TO import_events')
                c.execute('ALTER TABLE event_assets RENAME TO import_event_assets')
            c.execute("""CREATE TABLE events (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                starts_at TEXT, ends_at TEXT, review_state TEXT NOT NULL DEFAULT 'confirmed',
                source TEXT NOT NULL DEFAULT 'automatic', issue TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE event_assets (event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                asset_id TEXT NOT NULL, PRIMARY KEY(event_id, asset_id))""")
            c.execute('CREATE TABLE IF NOT EXISTS event_processed_assets (asset_id TEXT PRIMARY KEY)')
            if version == 6:
                mapping = {}
                for row in c.execute('SELECT * FROM import_events').fetchall():
                    event_id = str(uuid4()); mapping[row['id']] = event_id
                    c.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?,?)',
                              [event_id, *[row[k] for k in ('name', 'starts_at', 'ends_at', 'review_state', 'source', 'issue', 'created_at')]])
                    c.execute('INSERT INTO event_assets SELECT ?,asset_id FROM import_event_assets WHERE event_id=?', [event_id, row['id']])
                c.execute('INSERT OR IGNORE INTO event_processed_assets SELECT asset_id FROM event_assets')
                for row in c.execute('SELECT id,query_json FROM smart_albums').fetchall():
                    query = json.loads(row['query_json'])
                    if 'event' in query:
                        query['event'] = mapping.get(query['event'], str(uuid4()))
                        c.execute('UPDATE smart_albums SET query_json=? WHERE id=?', [json.dumps(query), row['id']])
                c.execute('DROP TABLE import_event_assets')
                c.execute('DROP TABLE import_events')
            c.execute('CREATE INDEX aura_event_assets_asset ON event_assets(asset_id)')
            report = {'complete': version == 6, 'source': 'local' if version == 6 else 'immich_tags', 'archive': archive.name}
            c.execute('INSERT INTO settings VALUES (?,?)', ['local_event_migration', json.dumps(report)])
            c.execute("DELETE FROM task_status WHERE name='event_tags'")
            c.execute('UPDATE schema_version SET version=?', [SCHEMA_VERSION])

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

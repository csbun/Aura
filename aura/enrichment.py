"""Import recognition results without replacing local review decisions.

One worker processes bounded batches. Each asset and its checkpoint commit
atomically, so restarts resume unfinished work without repeating completed work.
"""
from __future__ import annotations

import fcntl
import time
from datetime import datetime, timedelta, timezone

from .db import Database
from .immich import ImmichClient, ImmichError


def _now():
    return datetime.now(timezone.utc)


def _assignment(connection, table, column, asset_id, identity, source, state, version, model=None):
    connection.execute(
        f"INSERT INTO {table}(asset_id,{column},source,review_state,producer_version,model_name,model_version,observed_at) "
        "VALUES (?,?,?,?,?,?,?,?) "
        f"ON CONFLICT(asset_id,{column}) DO UPDATE SET active=1, "
        f"review_state=CASE WHEN {table}.reviewed=1 THEN {table}.review_state ELSE excluded.review_state END, "
        "producer_version=excluded.producer_version, observed_at=excluded.observed_at "
        f"WHERE {table}.source=excluded.source",
        [asset_id, identity, source, state, version, model, "1" if model else None, _now().isoformat()],
    )


def _tag(connection, name, kind):
    connection.execute("INSERT OR IGNORE INTO tags(name,kind,created_at) VALUES (?,?,?)", [name, kind, _now().isoformat()])
    return connection.execute("SELECT id FROM tags WHERE name=?", [name]).fetchone()[0]


def generate_metadata_tags(database: Database) -> int:
    """Small controlled vocabulary; no model download or image decoding."""
    count = 0
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE asset_tags SET active=0 WHERE source='aura.metadata' AND reviewed=0")
        for asset in connection.execute("SELECT id,captured_at,width,height,latitude,longitude FROM assets WHERE missing_at IS NULL").fetchall():
            names = []
            if asset['captured_at']:
                try:
                    year = datetime.fromisoformat(asset['captured_at']).year
                    names.append(f"系统/年份/{year}")
                except ValueError:
                    pass
            w, h = asset['width'], asset['height']
            if w and h:
                names.append('系统/画幅/' + ('横图' if w > h else '竖图' if h > w else '方图'))
            if asset['latitude'] is not None and asset['longitude'] is not None:
                names.append('系统/有定位')
            for name in names:
                _assignment(connection, 'asset_tags', 'tag_id', asset['id'], _tag(connection, name, 'system'), 'aura.metadata', 'confirmed', '1', 'metadata-rules')
                count += 1
    return count


class RecognitionSync:
    def __init__(self, database: Database):
        self.database = database

    def apply_asset(self, asset_id: int, remote: dict, version: str) -> None:
        with self.database.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            # A mapping can change while the network request is in flight.
            mapping = connection.execute("SELECT external_id FROM external_assets WHERE asset_id=? AND provider='immich'", [asset_id]).fetchone()
            if not mapping or mapping['external_id'] != remote['id']:
                raise ValueError('asset mapping changed during recognition import')
            for table in ('asset_people', 'asset_tags'):
                connection.execute(f"UPDATE {table} SET active=0 WHERE asset_id=? AND source='immich' AND reviewed=0", [asset_id])
            for person in remote['people']:
                mapped = connection.execute('SELECT person_id FROM external_people WHERE external_id=?', [person['id']]).fetchone()
                name = person['name'].strip()
                display = name or f"未命名人物 {person['id']}"
                if mapped:
                    person_id = mapped['person_id']
                    connection.execute('UPDATE people SET name=?,confirmed=? WHERE id=? AND name_locked=0', [display, int(bool(name)), person_id])
                else:
                    person_id = connection.execute('INSERT INTO people(name,confirmed,created_at) VALUES (?,?,?)', [display, int(bool(name)), _now().isoformat()]).lastrowid
                    connection.execute('INSERT INTO external_people(external_id,person_id) VALUES (?,?)', [person['id'], person_id])
                named = connection.execute('SELECT confirmed FROM people WHERE id=?', [person_id]).fetchone()[0]
                _assignment(connection, 'asset_people', 'person_id', asset_id, person_id, 'immich', 'confirmed' if named else 'pending', version)
            for tag in remote['tags']:
                name = tag.get('value') or tag['name']
                mapped = connection.execute('SELECT tag_id FROM external_tags WHERE external_id=?', [tag['id']]).fetchone()
                if mapped:
                    tag_id = mapped['tag_id']
                    # The local label is stable; external renames must not break album rules.
                else:
                    tag_id = _tag(connection, name, 'immich')
                    connection.execute('INSERT INTO external_tags(external_id,tag_id) VALUES (?,?)', [tag['id'], tag_id])
                _assignment(connection, 'asset_tags', 'tag_id', asset_id, tag_id, 'immich', 'confirmed', version)
            now = _now()
            connection.execute(
                'INSERT INTO enrichment_state(asset_id,external_id,next_attempt_at,synced_at,error) VALUES (?,?,?,?,NULL) '
                'ON CONFLICT(asset_id) DO UPDATE SET external_id=excluded.external_id,next_attempt_at=excluded.next_attempt_at,synced_at=excluded.synced_at,error=NULL',
                [asset_id, remote['id'], (now + timedelta(hours=6)).isoformat(), now.isoformat()],
            )

    def progress(self) -> dict:
        return self.database.fetch_all(
            "SELECT COUNT(*) AS total, COALESCE(SUM(s.synced_at IS NOT NULL AND s.external_id=e.external_id),0) AS completed, "
            "COALESCE(SUM(s.error IS NOT NULL AND s.external_id=e.external_id),0) AS failed "
            "FROM external_assets e JOIN assets a ON a.id=e.asset_id LEFT JOIN enrichment_state s ON s.asset_id=e.asset_id "
            "WHERE e.provider='immich' AND a.missing_at IS NULL"
        )[0]

    def run_batch(self, client: ImmichClient, limit: int = 100, budget_seconds: float = 20) -> dict:
        with self.database.path.with_suffix('.recognition.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {**self.progress(), 'busy': True, 'processed': 0, 'errors': []}
            deadline = time.monotonic() + budget_seconds
            rows = self.database.fetch_all(
                "SELECT e.asset_id,e.external_id FROM external_assets e JOIN assets a ON a.id=e.asset_id "
                "LEFT JOIN enrichment_state s ON s.asset_id=e.asset_id WHERE e.provider='immich' AND a.missing_at IS NULL "
                "AND (s.asset_id IS NULL OR s.external_id<>e.external_id OR s.next_attempt_at<=?) "
                "ORDER BY COALESCE(s.next_attempt_at,''),e.asset_id LIMIT ?", [_now().isoformat(), limit])
            version = client.server_version() if rows else None
            processed = 0
            errors = []
            for row in rows:
                if time.monotonic() >= deadline:
                    break
                try:
                    remote = client.asset_info(row['external_id'])
                    self.apply_asset(row['asset_id'], remote, version)
                    processed += 1
                except Exception as error:
                    errors.append(f"asset {row['asset_id']}: {error}")
                    self.database.execute(
                        'INSERT INTO enrichment_state(asset_id,external_id,next_attempt_at,error) VALUES (?,?,?,?) '
                        'ON CONFLICT(asset_id) DO UPDATE SET external_id=excluded.external_id,next_attempt_at=excluded.next_attempt_at,error=excluded.error',
                        [row['asset_id'], row['external_id'], (_now()+timedelta(minutes=5)).isoformat(), str(error)])
            return {**self.progress(), 'processed': processed, 'errors': errors}


def _face_result(database: Database, client: ImmichClient, previous: str) -> dict:
    # Immich removes finished queue entries, and handler errors can leave zero
    # failed entries. An idle queue alone cannot prove recognition succeeded.
    count = client.people_count()
    phase = 'done' if count else 'no_people'
    if phase == 'done' and previous != 'done':
        database.execute("UPDATE enrichment_state SET next_attempt_at=?", [_now().isoformat()])
    database.execute("INSERT INTO integration_state(name,value) VALUES ('face_bootstrap',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", [phase])
    result = {'phase': phase, 'people': count}
    if not count:
        result['warning'] = 'Immich 队列已空闲，但未生成人物。请检查人脸模型下载和机器学习日志；队列空闲不代表识别成功。'
    return result


def ensure_face_jobs(database: Database, client: ImmichClient) -> dict:
    """Bootstrap missing recognition work once; never force-reprocess the library."""
    rows = database.fetch_all("SELECT value FROM integration_state WHERE name='face_bootstrap'")
    phase = rows[0]['value'] if rows else 'new'
    if not database.fetch_all("SELECT 1 FROM external_assets WHERE provider='immich' LIMIT 1"):
        return {'phase': 'waiting_for_library'}
    queues = client.face_queues()
    if phase in {'done', 'no_people'}:
        # A user or Immich may start another pass after the first bootstrap.
        # Continue observing it instead of treating the old checkpoint as final.
        for stage, queue_name in (('detection', 'faceDetection'), ('recognition', 'facialRecognition')):
            queue = queues[queue_name]
            counts = queue['jobCounts']
            if queue['queueStatus'].get('isPaused'):
                raise ImmichError(f"Immich {queue_name} is paused")
            if counts.get('failed', 0):
                raise ImmichError(f"Immich {queue_name}: {counts['failed']} failed jobs")
            if queue['queueStatus'].get('isActive') or any(counts.get(k, 0) for k in ('active', 'waiting', 'delayed', 'paused')):
                database.execute("UPDATE integration_state SET value=? WHERE name='face_bootstrap'", [stage])
                return {'phase': stage, 'active': counts.get('active', 0), 'waiting': counts.get('waiting', 0)}
        return _face_result(database, client, phase)
    name = 'facialRecognition' if phase == 'recognition' else 'faceDetection'
    queue = queues[name]
    counts = queue['jobCounts']
    if queue['queueStatus'].get('isPaused'):
        raise ImmichError(f"Immich {name} is paused; recognition cannot proceed")
    if counts.get('failed', 0):
        raise ImmichError(f"Immich {name}: {counts['failed']} failed jobs; check Immich machine-learning logs")
    busy = queue['queueStatus'].get('isActive') or any(counts.get(k,0) for k in ('active','waiting','delayed','paused'))
    if phase == 'new':
        if not busy:
            client.start_missing_faces('faceDetection')
        phase = 'detection'
    elif phase == 'detection' and not busy:
        recognition = queues['facialRecognition']
        if recognition['queueStatus'].get('isPaused'):
            raise ImmichError('Immich facialRecognition is paused')
        if not recognition['queueStatus'].get('isActive') and not any(recognition['jobCounts'].get(k,0) for k in ('active','waiting','delayed')):
            client.start_missing_faces('facialRecognition')
        phase = 'recognition'
    elif phase == 'recognition' and not busy:
        return _face_result(database, client, phase)
    database.execute("INSERT INTO integration_state(name,value) VALUES ('face_bootstrap',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",[phase])
    return {'phase':phase, 'active':counts.get('active',0), 'waiting':counts.get('waiting',0)}

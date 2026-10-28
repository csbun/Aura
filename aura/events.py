"""Aura owns events; photo metadata is read from Immich and never replicated."""
from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .activity import captured, group_activities, location, _visual
from .immich import ImmichError


def bounds(assets):
    dates = sorted(date for a in assets if (date := captured(a)))
    return (dates[0].isoformat(), dates[-1].isoformat()) if dates else (None, None)


class Events:
    # The service serializes all mutations with the shared albums lock.
    def __init__(self, database, immich):
        self.db, self.remote = database, immich

    def migration(self):
        return json.loads(self.db.get_setting('local_event_migration', '{"complete":true}'))

    def require_ready(self):
        if not self.migration()['complete']:
            raise ValueError('事件正在迁回 Aura，请等待迁移完成')

    def migrate(self):
        report = self.migration()
        if report['complete']:
            return report
        if self.db.fetch_all('SELECT id FROM event_operations'):
            raise ImmichError('旧版标签操作尚未完成；保留原数据并暂停迁移，请先停服务、恢复 pre-0.4.3 归档，再使用 0.4.2 处理待完成操作')
        catalog = {t['id']: t for t in self.remote.tags()}
        records = self.db.fetch_all('SELECT * FROM managed_event_tags')
        imported, mapping = [], {}
        for row in records:
            event_id = row['tag_id']
            if row['legacy_id'] is not None:
                mapping[row['legacy_id']] = event_id
            if row['state'] == 'deleted':
                imported.append((event_id, '已删除事件', None, None, 'deleted', 'manual', None, []))
                continue
            tag = catalog.get(event_id)
            if not tag:
                # Absence may mean a permissions change; never silently import empty membership.
                raise ImmichError('待迁移事件标签不存在或无读取权限；迁移暂停，原数据保留：' + event_id)
            assets = self.remote.tag_assets(event_id)
            fingerprint = sha256(json.dumps([tag['name'], tag.get('parentId'), sorted({a['id'] for a in assets})], ensure_ascii=False).encode()).hexdigest()
            mode = 'manual' if row['fingerprint'] and fingerprint != row['fingerprint'] else row['mode']
            imported.append((event_id, tag['name'], *bounds(assets), 'confirmed', mode, None, [a['id'] for a in assets]))
        tables = {r['name'] for r in self.db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'legacy_events' in tables:
            for row in self.db.fetch_all('SELECT * FROM legacy_events'):
                if row['id'] in mapping:
                    continue
                event_id = str(uuid4()); mapping[row['id']] = event_id
                members = [a['asset_id'] for a in self.db.fetch_all('SELECT asset_id FROM legacy_event_assets WHERE event_id=?', [row['id']])]
                imported.append((event_id, row['name'], row['starts_at'], row['ends_at'], row['review_state'], row['source'], row['issue'], members))
        managed = {row['tag_id'] for row in records}
        # No remote mutations: commit all imported events and rule conversions together.
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for event_id, name, start, end, state, source, issue, members in imported:
                c.execute('INSERT INTO events(id,name,starts_at,ends_at,review_state,source,issue) VALUES (?,?,?,?,?,?,?)',
                          [event_id, name, start, end, state, source, issue])
                c.executemany('INSERT OR IGNORE INTO event_assets VALUES (?,?)', [(event_id, i) for i in members])
                c.executemany('INSERT OR IGNORE INTO event_processed_assets VALUES (?)', [(i,) for i in members])
            for row in c.execute('SELECT id,query_json FROM smart_albums').fetchall():
                query = json.loads(row['query_json'])
                events = [i for i in query.get('tag_ids', []) if i in managed]
                tags = [i for i in query.get('tag_ids', []) if i not in managed]
                if 'event' in query:
                    old = query.pop('event')
                    events.append(mapping.get(old, old if isinstance(old, str) else str(uuid4())))
                if events:
                    query['event_ids'] = list(dict.fromkeys([*query.get('event_ids', []), *events]))
                if tags:
                    query['tag_ids'] = tags
                else:
                    query.pop('tag_ids', None)
                c.execute('UPDATE smart_albums SET query_json=?,materialized_at=NULL WHERE id=?', [json.dumps(query), row['id']])
            for name in ('legacy_event_assets', 'legacy_events', 'event_operations', 'managed_event_tags'):
                if name in tables:
                    c.execute('DROP TABLE ' + name)
            report.update(complete=True, imported=len(imported), retired_tag_ids=sorted(managed))
            c.execute('UPDATE settings SET value=? WHERE name=?', [json.dumps(report), 'local_event_migration'])
            c.execute("DELETE FROM settings WHERE name IN ('event_tag_migration','event_root_tag')")
        return report

    def list(self):
        self.require_ready()
        rows = self.db.fetch_all("""SELECT e.*, COUNT(a.asset_id) AS asset_count FROM events e
            LEFT JOIN event_assets a ON a.event_id=e.id WHERE e.review_state='confirmed'
            GROUP BY e.id ORDER BY e.starts_at DESC,e.id""")
        for row in rows:
            try:
                row['evidence'] = json.loads(row.pop('evidence_json') or '{}')
            except (TypeError, json.JSONDecodeError):
                row['evidence'] = {}
        return rows

    def record(self, event_id):
        self.require_ready()
        rows = self.db.fetch_all("SELECT * FROM events WHERE id=? AND review_state='confirmed'", [str(event_id)])
        if not rows:
            raise LookupError('事件不存在或已删除；相关规则暂停')
        if rows[0]['issue']:
            raise ValueError(rows[0]['issue'])
        return rows[0]

    def members(self, event_id):
        self.record(event_id)
        return {r['asset_id'] for r in self.db.fetch_all('SELECT asset_id FROM event_assets WHERE event_id=?', [str(event_id)])}

    @staticmethod
    def name(value):
        if not value or not value.strip() or len(value.strip()) > 200:
            raise ValueError('事件名称须为 1 到 200 个字符')
        return value.strip()

    def rename(self, event_id, name):
        row = self.record(event_id)
        name = self.name(name) if name is not None else row['name']
        self.db.execute("UPDATE events SET name=?,source='manual' WHERE id=?", [name, str(event_id)])
        return next(e for e in self.list() if e['id'] == str(event_id))

    def merge(self, event_ids, name=None):
        ids = [str(i) for i in event_ids]
        if len(set(ids)) < 2 or len(ids) != len(set(ids)):
            raise ValueError('请选择至少两个不同事件')
        rows = [self.record(i) for i in ids]
        target = ids[0]
        name = self.name(name) if name is not None else rows[0]['name']
        members = set().union(*(self.members(i) for i in ids))
        starts = [r['starts_at'] for r in rows if r['starts_at']]
        ends = [r['ends_at'] for r in rows if r['ends_at']]
        updated_albums = 0
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE events SET name=?,starts_at=?,ends_at=?,source='manual',algorithm_version=2 WHERE id=?", [name, min(starts) if starts else None, max(ends) if ends else None, target])
            c.executemany('INSERT OR IGNORE INTO event_assets VALUES (?,?)', [(target, i) for i in members])
            c.executemany("UPDATE events SET review_state='deleted' WHERE id=?", [(i,) for i in ids[1:]])
            for album in c.execute('SELECT id,query_json FROM smart_albums').fetchall():
                query = json.loads(album['query_json']); before = json.dumps(query)
                if query.get('event') in ids:
                    query['event'] = target
                if query.get('event_ids'):
                    query['event_ids'] = list(dict.fromkeys(target if i in ids else i for i in query['event_ids']))
                if before != json.dumps(query):
                    updated_albums += 1
                    c.execute('UPDATE smart_albums SET query_json=?,materialized_at=NULL WHERE id=?', [json.dumps(query), album['id']])
        return {**next(e for e in self.list() if e['id'] == target), 'updated_albums': updated_albums}

    def delete(self, event_id):
        self.record(event_id)
        self.db.execute("UPDATE events SET review_state='deleted',source='manual' WHERE id=?", [str(event_id)])
        return {'id': str(event_id), 'review_state': 'deleted'}

    def build(self):
        self.require_ready()
        # Complete the remote read before changing any local membership.
        try:
            assets = self.remote.search({}, with_exif=True, with_people=True)
        except TypeError:  # small test doubles and older adapters
            assets = self.remote.search({})
        by_id = {a['id']: a for a in assets}
        processed = {r['asset_id'] for r in self.db.fetch_all('SELECT asset_id FROM event_processed_assets')}
        fresh = set(by_id) - processed
        existing = {e['id']: e for e in self.list()}
        automatic = {i: self.members(i) for i, e in existing.items() if e['source'] == 'automatic' and not e['issue']}
        visual_index = self._visual_index(assets)
        activities = group_activities(assets, visual_index)
        created = changed = 0
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for activity in activities:
                group = activity['assets']; ids = {a['id'] for a in group}; new = ids & fresh
                if not new:
                    continue
                matches = [i for i, members in automatic.items() if members & ids]
                # Preserve existing events; extend the strongest overlap rather than silently merging them.
                target = max(matches, key=lambda i: len(automatic[i] & ids)) if matches else str(uuid4())
                start, end = bounds(group)
                labels = [location(a)[1] for a in group if location(a)[1]]
                kind = next((k for k in ('会议', '旅行', '聚餐', '风景') if k in activity['name']), None)
                payload = {'reasons': activity['reasons'], 'confidence': activity['confidence'],
                           'uncertain': activity['uncertain'], 'asset_count': len(ids)}
                if not matches:
                    c.execute('INSERT INTO events(id,name,starts_at,ends_at,source,location_label,activity_kind,confidence,evidence_json,algorithm_version) VALUES (?,?,?,?,?,?,?,?,?,?)',
                              [target, activity['name'], start, end, 'automatic', labels[0] if labels else None, kind, activity['confidence'], json.dumps(payload, ensure_ascii=False), 2])
                    automatic[target] = set(); created += 1
                else:
                    changed += 1
                automatic[target] |= new
                c.executemany('INSERT INTO event_assets VALUES (?,?)', [(target, i) for i in new])
                c.executemany('INSERT INTO event_processed_assets VALUES (?)', [(i,) for i in new])
                all_items = [by_id[i] for i in automatic[target] if i in by_id]
                all_activity = group_activities(all_items, visual_index)[0] if all_items else activity
                c.execute('UPDATE events SET starts_at=?,ends_at=?,location_label=COALESCE(?,location_label),activity_kind=COALESCE(?,activity_kind),confidence=?,evidence_json=?,algorithm_version=2 WHERE id=?',
                          [bounds(all_items)[0], bounds(all_items)[1], labels[0] if labels else None, kind, all_activity['confidence'], json.dumps({**payload, 'asset_count': len(automatic[target])}, ensure_ascii=False), target])
                fresh -= new
            if fresh:
                # Photos without capture time still remain visible in Immich, but
                # are not silently dropped from the processed set forever.
                c.executemany('INSERT OR IGNORE INTO event_processed_assets VALUES (?)', [(i,) for i in fresh])
            c.execute("INSERT INTO settings VALUES ('events_built_at',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", [datetime.now(timezone.utc).isoformat()])
        return {'created': created, 'updated': changed, 'events': len(self.list())}

    def _visual_index(self, assets):
        """Ask Immich Smart Search for coarse visual evidence when supported.

        Search results are evidence only; they never replace the time/location
        and people checks in :func:`group_activities`.
        """
        index = {}
        if not hasattr(self.remote, '_request'):
            return index
        for label, query in {'会议': 'meeting conference', '旅行': 'travel trip',
                             '聚餐': 'food dinner restaurant', '风景': 'landscape scenery'}.items():
            try:
                for asset in self.remote.search({'q': query}):
                    index.setdefault(asset['id'], set()).add(label)
            except ImmichError:
                continue
        return index

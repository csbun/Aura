from __future__ import annotations

import json
from datetime import datetime, timezone

from .events import build_candidates
from .immich import ImmichError
from .models import AlbumQuery
from .query import parse_query


def now():
    return datetime.now(timezone.utc).isoformat()


class AuraService:
    def __init__(self, database, immich):
        self.database, self.immich = database, immich
        database.initialize()

    def check_integration(self):
        version = self.immich.require_supported()
        identity = json.dumps({'url': self.immich.settings.base_url, 'user': self.immich.identity()}, sort_keys=True)
        with self.database.lock('binding'):
            previous = self.database.get_setting('immich_identity')
            if previous and previous != identity:
                raise ImmichError('Immich 地址或 API key 所属用户已改变；请使用原配置，或为新实例配置独立 AURA_DATA_DIR')
            if not previous:
                self.database.set_setting('immich_identity', identity)
        return version

    def search_assets(self, query):
        query = AlbumQuery.model_validate(query).compact()
        members = None
        if query.get('event'):
            event = self.database.fetch_all('SELECT * FROM events WHERE id=?', [query['event']])
            if not event:
                raise LookupError('事件不存在')
            if event[0]['issue']:
                raise ValueError(event[0]['issue'])
            members = {r['asset_id'] for r in self.database.fetch_all('SELECT asset_id FROM event_assets WHERE event_id=?', [query['event']])}
            if not members:
                return []
        # Nothing is stored: Immich is authoritative even when people/tags change.
        rows = self.immich.search(query)
        return [r for r in rows if members is None or r['id'] in members]

    def preview_album(self, query):
        query = AlbumQuery.model_validate(query).compact()
        rows = self.search_assets(query)
        counts = {}
        if not rows:
            for key, value in query.items():
                counts[key] = len(self.search_assets({key: value}))
        return {'matched_assets': len(rows), 'condition_counts': counts,
                'semantic_limit': 100 if query.get('q') else None,
                'assets': [{'id': r['id']} for r in rows[:12]]}

    def list_events(self):
        return self.database.fetch_all("SELECT e.*, (SELECT COUNT(*) FROM event_assets WHERE event_id=e.id) AS asset_count FROM events e WHERE review_state!='superseded' ORDER BY starts_at DESC,id DESC")

    def review_event(self, event_id, state, name=None):
        with self.database.lock('albums'):
            if not self.database.fetch_all('SELECT id FROM events WHERE id=?', [event_id]):
                raise LookupError('事件不存在')
            if name is not None and not name.strip():
                raise ValueError('事件名称不能为空')
            self.database.execute('UPDATE events SET review_state=?,name=COALESCE(?,name) WHERE id=?', [state, name.strip() if name else None, event_id])
        return {'id': event_id, 'review_state': state}

    def build_events(self):
        self.check_integration()
        # Fetch the complete snapshot before changing any candidates; outages keep previous events intact.
        rows = self.immich.search({})
        with self.database.lock('albums'):
            return build_candidates(self.database, rows)

    def list_smart_albums(self):
        rows = self.database.fetch_all('SELECT * FROM smart_albums ORDER BY id DESC')
        for row in rows:
            row['query'] = json.loads(row.pop('query_json'))
        return rows

    def save_album(self, name, query, sync_mode, album_id=None):
        query = AlbumQuery.model_validate(query).compact()
        self.check_integration()
        rows = self.search_assets(query)
        if query.get('event'):
            event = self.database.fetch_all('SELECT review_state FROM events WHERE id=?', [query['event']])[0]
            if event['review_state'] != 'confirmed':
                raise ValueError('请先确认事件')
        with self.database.lock('albums'):
            if album_id is None:
                album_id = self.database.execute('INSERT INTO smart_albums(name,query_json,sync_mode,matched_assets) VALUES (?,?,?,?)',
                                                [name, json.dumps(query, ensure_ascii=False), sync_mode, len(rows)]).lastrowid
            else:
                if not self.database.fetch_all('SELECT id FROM smart_albums WHERE id=?', [album_id]):
                    raise LookupError('相册规则不存在')
                self.database.execute('UPDATE smart_albums SET name=?,query_json=?,sync_mode=?,migration_issue=NULL,error=NULL,materialized_at=NULL,matched_assets=? WHERE id=?',
                                      [name, json.dumps(query, ensure_ascii=False), sync_mode, len(rows), album_id])
        return {'id': album_id, 'matched_assets': len(rows)}

    def materialize(self, album_id):
        with self.database.lock('albums'):
            self.check_integration()
            rows = self.database.fetch_all('SELECT * FROM smart_albums WHERE id=?', [album_id])
            if not rows:
                raise LookupError('相册规则不存在')
            album = rows[0]
            if album['migration_issue']:
                raise ValueError(album['migration_issue'])
            query = json.loads(album['query_json'])
            if query.get('event'):
                events = self.database.fetch_all('SELECT review_state FROM events WHERE id=?', [query['event']])
                if not events or events[0]['review_state'] != 'confirmed':
                    raise ValueError('关联事件尚未确认；相册更新已暂停')
            desired = {r['id'] for r in self.search_assets(query)}
            remote_id = album['remote_id']
            if not remote_id:
                marker = f"Aura rule {self.database.get_setting('instance_id')}:{album_id}"
                existing = [r for r in self.immich.albums() if r.get('description') == marker]
                if len(existing) > 1:
                    raise ImmichError('发现重复 Aura 相册标记，请核对 Immich 相册')
                remote_id = existing[0]['id'] if existing else self.immich.create_album(album['name'], marker)
                self.database.execute('UPDATE smart_albums SET remote_id=? WHERE id=?', [remote_id, album_id])
            current = self.immich.album_assets(remote_id)
            added = desired - current
            removed = current - desired if album['sync_mode'] == 'mirror' else set()
            # Add first; a partial failure never triggers removals. Retry computes a fresh delta.
            self.immich.change_album_assets(remote_id, added)
            self.immich.change_album_assets(remote_id, removed, remove=True)
            self.database.execute('UPDATE smart_albums SET materialized_at=?,matched_assets=?,error=NULL WHERE id=?', [now(), len(desired), album_id])
            return {'remote_id': remote_id, 'matched_assets': len(desired), 'added': len(added), 'removed': len(removed)}

    def finish_migration(self):
        self.check_integration()
        from .migration import finish_names
        return finish_names(self.database, self.immich)

    def sync_albums(self):
        self.check_integration()
        synced, errors, paused = 0, [], 0
        for album in self.list_smart_albums():
            if album['migration_issue']:
                paused += 1
                continue
            try:
                self.materialize(album['id'])
                synced += 1
            except Exception as error:
                self.database.execute('UPDATE smart_albums SET error=? WHERE id=?', [str(error), album['id']])
                errors.append(f"相册 {album['id']}: {error}")
        return {'albums': synced, 'paused': paused, 'errors': errors}

    def parse_search(self, text):
        people, tags = self.immich.people(), self.immich.tags()
        plan = parse_query(text, [p.get('name', '') for p in people], [t.get('value') or t.get('name', '') for t in tags])
        query, ambiguous = {}, []
        for kind, catalog, field in [('people', people, 'person_ids'), ('tags', tags, 'tag_ids')]:
            ids = []
            for name in plan[kind]:
                matches = [r['id'] for r in catalog if (r.get('value') or r.get('name')) == name]
                if len(matches) == 1:
                    ids.extend(matches)
                else:
                    ambiguous.append(name)
            if ids:
                query[field] = ids
        for key in ('start', 'end'):
            if plan[key]:
                query[key] = plan[key]
        if plan['terms']:
            query['q'] = ' '.join(plan['terms'])
        return {'query': query, 'ambiguous_names': ambiguous, 'needs_confirmation': True,
                'note': '请核对条件。语义搜索取 Immich 最相关的前 100 张，再与事件求交集；中文效果取决于 Immich 模型。'}

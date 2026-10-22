from __future__ import annotations

import json
from datetime import datetime, timezone

from .event_tags import EventTags
from .immich import ImmichError
from .models import AlbumQuery
from .query import parse_query


def now():
    return datetime.now(timezone.utc).isoformat()


class AuraService:
    def __init__(self, database, immich):
        self.database, self.immich = database, immich
        database.initialize()
        self.event_tags = EventTags(database, immich)

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

    def _query(self, query):
        self.event_tags.require_ready()
        query = AlbumQuery.model_validate(query).compact()
        event = query.pop('event', None)
        if event:
            query['tag_ids'] = list(dict.fromkeys([*query.get('tag_ids', []), event]))
        if query.get('tag_ids'):
            available = {t['id'] for t in self.immich.tags()}
            if set(query['tag_ids']) - available:
                raise LookupError('关联的 Immich 标签已删除或无读取权限；规则暂停，请重新选择条件')
        return query

    def search_assets(self, query):
        return self.immich.search(self._query(query))

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
        with self.database.lock('albums'):
            return self.event_tags.list()

    def review_event(self, event_id, state='confirmed', name=None):
        if state == 'rejected':
            return self.delete_event(event_id)
        if state != 'confirmed':
            raise ValueError('事件默认通过，无需设为待确认')
        with self.database.lock('albums'):
            self.check_integration()
            return self.event_tags.rename(str(event_id), name)

    def merge_events(self, event_ids, name=None):
        with self.database.lock('albums'):
            self.check_integration()
            return self.event_tags.merge(event_ids, name)

    def delete_event(self, event_id):
        with self.database.lock('albums'):
            self.check_integration()
            return self.event_tags.delete(str(event_id))

    def cancel_event_operation(self, key):
        with self.database.lock('albums'):
            self.check_integration()
            self.event_tags.cancel(key)

    def process_event_operations(self):
        with self.database.lock('albums'):
            self.check_integration()
            migration = self.event_tags.migrate()
            result = self.event_tags.retry()
            return {**result, 'migration': migration}

    def build_events(self):
        with self.database.lock('albums'):
            self.check_integration()
            return self.event_tags.build()

    def list_smart_albums(self):
        rows = self.database.fetch_all('SELECT * FROM smart_albums ORDER BY id DESC')
        for row in rows:
            row['query'] = json.loads(row.pop('query_json'))
        return rows

    def save_album(self, name, query, sync_mode, album_id=None):
        self.check_integration()
        with self.database.lock('albums'):
            query = self._query(query)
            rows = self.immich.search(query)
            if album_id is None:
                album_id = self.database.execute('INSERT INTO smart_albums(name,query_json,sync_mode,matched_assets) VALUES (?,?,?,?)',
                                                [name, json.dumps(query, ensure_ascii=False), sync_mode, len(rows)]).lastrowid
            else:
                if not self.database.fetch_all('SELECT id FROM smart_albums WHERE id=?', [album_id]):
                    raise LookupError('相册规则不存在')
                self.database.execute('UPDATE smart_albums SET name=?,query_json=?,sync_mode=?,migration_issue=NULL,error=NULL,materialized_at=NULL,matched_assets=? WHERE id=?',
                                      [name, json.dumps(query, ensure_ascii=False), sync_mode, len(rows), album_id])
        return {'id': album_id, 'matched_assets': len(rows)}

    def delete_album(self, album_id):
        # Serialize against in-flight writes; deletion never calls Immich.
        with self.database.lock('albums'):
            self.database.execute('DELETE FROM smart_albums WHERE id=?', [album_id])

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
            desired = {r['id'] for r in self.search_assets(query)}
            remote_id = album['remote_id']
            if remote_id and remote_id not in {a['id'] for a in self.immich.albums()}:
                raise ImmichError('关联的 Immich 相册已删除或无读取权限；请检查 Immich 相册及 API key，或新建相册规则')
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
        if not self.event_tags.migration()['complete']:
            return {'albums': 0, 'paused': len(self.list_smart_albums()), 'errors': []}
        for album in self.list_smart_albums():
            if album['migration_issue']:
                paused += 1
                continue
            try:
                self.materialize(album['id'])
                synced += 1
            except Exception as error:
                updated = self.database.execute('UPDATE smart_albums SET error=? WHERE id=?', [str(error), album['id']])
                if not updated.rowcount:
                    continue
                if isinstance(error, LookupError):
                    paused += 1
                else:
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

"""Immich owns event names and membership; SQLite owns only automation state.

Pending operations temporarily hold intended writes for recovery. Successful
operations are removed, and no current name/member catalog is persisted locally.
All public mutations are called under the service's shared album lock.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .immich import ImmichError


def fingerprint(tag, ids):
    value = [tag['name'], tag.get('parentId'), sorted(ids)]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def captured(asset):
    raw = asset.get('localDateTime') or asset.get('fileCreatedAt')
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).replace(tzinfo=None) if raw else None
    except (ValueError, TypeError):
        return None


def groups(assets):
    ordered = sorted((date, a['id']) for a in assets if (date := captured(a)))
    result = []
    for date, asset_id in ordered:
        if not result or date.date() != result[-1][-1][0].date() or date-result[-1][-1][0] > timedelta(hours=6):
            result.append([])
        result[-1].append((date, asset_id))
    return result


class EventTags:
    def __init__(self, database, immich):
        self.db, self.remote = database, immich

    @property
    def enabled(self):
        return os.getenv('AURA_EVENT_TAGS_ENABLED', '').lower() == 'true'

    def require_enabled(self):
        if not self.enabled:
            raise ImmichError('事件标签写入未启用：请先验证图库可持久保存 XMP，再设置 AURA_EVENT_TAGS_ENABLED=true')

    def migration(self):
        return json.loads(self.db.get_setting('event_tag_migration', '{"complete":true}'))

    def require_ready(self):
        if not self.migration()['complete']:
            raise ValueError('事件正在迁移到 Immich 标签，请等待迁移完成')

    def _record(self, tag_id):
        rows = self.db.fetch_all('SELECT * FROM managed_event_tags WHERE tag_id=?', [str(tag_id)])
        if not rows or rows[0]['state'] != 'active':
            raise LookupError('事件标签不存在或已删除')
        return rows[0]

    def _snapshot(self):
        catalog = {t['id']: t for t in self.remote.tags()}
        result = {}
        for row in self.db.fetch_all("SELECT * FROM managed_event_tags WHERE state='active' ORDER BY rowid"):
            tag = catalog.get(row['tag_id'])
            if not tag:
                # A previously processed asset never becomes new just because a tag disappeared.
                self.db.execute("UPDATE managed_event_tags SET state='deleted' WHERE tag_id=?", [row['tag_id']])
                continue
            members = self.remote.tag_assets(tag['id'])
            ids = {a['id'] for a in members}
            current = fingerprint(tag, ids)
            mode = row['mode']
            if row['fingerprint'] and current != row['fingerprint']:
                mode = 'manual'
                self.db.execute("UPDATE managed_event_tags SET mode='manual' WHERE tag_id=?", [tag['id']])
            self._mark_processed(ids)
            result[tag['id']] = {'tag': tag, 'assets': members, 'ids': ids, 'fingerprint': current, 'mode': mode}
        return result

    def list(self):
        events = []
        for tag_id, item in self._snapshot().items():
            dates = sorted(date for a in item['assets'] if (date := captured(a)))
            events.append({'id': tag_id, 'name': item['tag']['name'], 'tag_path': item['tag']['value'],
                           'asset_count': len(item['ids']), 'source': item['mode'], 'review_state': 'confirmed',
                           'starts_at': dates[0].isoformat() if dates else None,
                           'ends_at': dates[-1].isoformat() if dates else None, 'issue': None})
        return sorted(events, key=lambda e: (e['starts_at'] or '', e['id']), reverse=True)

    def _mark_processed(self, ids):
        with self.db.connect() as c:
            c.executemany('INSERT OR IGNORE INTO event_processed_assets VALUES (?)', [(i,) for i in ids])

    def _save(self, key, payload):
        self.db.execute('INSERT INTO event_operations(id,payload_json) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,error=NULL',
                        [key, json.dumps(payload, ensure_ascii=False)])

    def _root(self):
        catalog = self.remote.tags()
        root_id = self.db.get_setting('event_root_tag')
        if root_id:
            if not any(t['id'] == root_id for t in catalog):
                raise ImmichError('Immich 事件父标签已删除；不会自动重建，请检查事件标签')
            return root_id
        marker = 'Aura pending root ' + self.db.get_setting('instance_id')
        matches = [t for t in catalog if t['name'] == marker and not t.get('parentId')]
        if len(matches) > 1:
            raise ImmichError('事件父标签创建结果不唯一')
        root = matches[0] if matches else self.remote.create_tag(marker)
        names = {t['name'] for t in catalog if not t.get('parentId') and t['id'] != root['id']}
        name, index = '事件', 2
        while name in names:
            name = f'事件（Aura {index}）'; index += 1
        # Persist recovery intent before renaming the remote root.
        with self.db.connect() as c:
            c.execute('INSERT INTO settings VALUES (?,?)', ['event_root_tag', root['id']])
            c.execute('INSERT INTO settings VALUES (?,?)', ['event_root_name_pending', name])
        self._finish_root()
        return root['id']

    def _finish_root(self):
        name = self.db.get_setting('event_root_name_pending')
        if name:
            self.require_enabled()
            self.remote.wait_tag_writes()
            root_id = self.db.get_setting('event_root_tag')
            tag = self.remote.tag(root_id)
            if tag['name'].startswith('Aura pending root ') or tag['name'] == name:
                self.remote.rename_tag(root_id, name)
                self.remote.wait_tag_writes()
            self.db.execute("DELETE FROM settings WHERE name='event_root_name_pending'")

    def _unique_name(self, name, parent_id, target_id=None):
        name = name.strip().replace('/', '／')
        if not name:
            raise ValueError('事件名称不能为空')
        names = {t['name'] for t in self.remote.tags() if t.get('parentId') == parent_id and t['id'] != target_id}
        candidate, index = name, 2
        while candidate in names:
            candidate = f'{name}（{index}）'; index += 1
        return candidate

    def _prepare(self, kind, ids=(), name=None, members=(), origin=None, mode='manual', legacy_id=None, snapshot=None):
        self.require_enabled()
        self.remote.wait_tag_writes()
        self.require_ready()
        snapshot = self._snapshot() if snapshot is None else snapshot
        selected = {}
        for tag_id in ids:
            self._record(tag_id)
            if tag_id not in snapshot:
                raise LookupError('事件标签已在 Immich 删除')
            selected[tag_id] = snapshot[tag_id]
        busy = set()
        for row in self.db.fetch_all('SELECT payload_json FROM event_operations'):
            p = json.loads(row['payload_json'])
            busy.update(p.get('inputs', {}))
            if p.get('target'): busy.add(p['target'])
        if busy & set(ids):
            raise ValueError('所选事件有未完成任务，请先处理任务错误')
        target = next(iter(selected), None)
        members = set(members) | {i for item in selected.values() for i in item['ids']}
        baseline = selected[target]['tag']['name'] if target else None
        payload = {'kind': kind, 'target': target, 'name': name or baseline,
                   'members': sorted(members), 'inputs': {i: v['fingerprint'] for i, v in selected.items()},
                   'baseline_name': baseline, 'original_target_members': sorted(selected[target]['ids']) if target else [], 'mode': mode, 'origin': origin or str(uuid4()),
                   'legacy_id': legacy_id, 'stage': 'planned'}
        key = str(uuid4())
        self._save(key, payload)
        return self._execute(key, payload)

    def _execute(self, key, p):
        try:
            result = self._apply(key, p)
            self.db.execute('DELETE FROM event_operations WHERE id=?', [key])
            return result
        except Exception as error:
            self.db.execute('UPDATE event_operations SET error=? WHERE id=?', [str(error), key])
            raise

    def _apply(self, key, p):
        self.require_enabled()
        self.remote.wait_tag_writes()
        catalog = {t['id']: t for t in self.remote.tags()}
        target = p.get('target')
        if p['stage'] == 'planned':
            for tag_id, expected in p.get('inputs', {}).items():
                if tag_id not in catalog:
                    raise ImmichError('任务中的事件已在 Immich 删除，已停止执行')
                current = fingerprint(catalog[tag_id], {a['id'] for a in self.remote.tag_assets(tag_id)})
                if current != expected:
                    raise ImmichError('事件已被其他操作修改，任务暂停；请取消旧任务后重新操作')
            # This durable stage distinguishes a retry after partially successful writes.
            p['stage'] = 'writing'; self._save(key, p)
        if p['kind'] == 'delete':
            if target in catalog:
                self._remove_tag(key, p, target)
            self.db.execute("UPDATE managed_event_tags SET state='deleted' WHERE tag_id=?", [target])
            self._pause_rules(target)
            return {'id': target, 'review_state': 'deleted'}
        if not target:
            root = self._root()
            marker = 'Aura pending ' + key
            catalog = {t['id']: t for t in self.remote.tags()}
            matches = [t for t in catalog.values() if t['name'] == marker and t.get('parentId') == root]
            if len(matches) > 1:
                raise ImmichError('事件标签创建结果不唯一')
            tag = matches[0] if matches else self.remote.create_tag(marker, root)
            target = p['target'] = tag['id']
            p['baseline_name'] = marker
            p['name'] = self._unique_name(p['name'], root, target)
            self._save(key, p)
        tag = self.remote.tag(target)
        ids = {a['id'] for a in self.remote.tag_assets(target)}
        desired = set(p['members'])
        if tag['name'] not in {p['baseline_name'], p['name']} or not ids <= desired:
            raise ImmichError('目标标签在任务期间被人工修改，任务暂停；请取消后重新操作')
        if not set(p.get('original_target_members', [])) <= ids:
            raise ImmichError('目标标签成员被人工移除，任务暂停')
        if p['stage'] == 'cleanup' and ids != desired:
            raise ImmichError('已核验的标签成员发生变化，停止清理旧标签')
        if p['kind'] == 'rename':
            # Renaming does not change any photo memberships.
            if fingerprint(tag, ids) != p['inputs'][target] and tag['name'] != p['name']:
                raise ImmichError('事件已被修改，未覆盖其名称')
        if tag['name'] != p['name']:
            tag = self.remote.rename_tag(target, p['name'])
            self.remote.wait_tag_writes()
        if p['kind'] != 'rename':
            self.remote.change_tag_assets(target, desired-ids)
            self.remote.wait_tag_writes()
        members = {a['id'] for a in self.remote.tag_assets(target)}
        if members != desired:
            raise ImmichError('事件标签成员核验未通过，旧标签和旧数据保留')
        # Verify each source immediately before committing its replacement.
        for source, expected in p.get('inputs', {}).items():
            if source == target or source in p.get('deleted_sources', []) or source in p.get('clearing', {}): continue
            current_catalog = {t['id']: t for t in self.remote.tags()}
            if source not in current_catalog:
                if p['stage'] == 'cleanup': continue
                raise ImmichError('合并来源标签已被删除，任务暂停')
            actual = fingerprint(current_catalog[source], {a['id'] for a in self.remote.tag_assets(source)})
            if actual != expected:
                raise ImmichError('合并来源标签已被修改，旧标签保留')
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("INSERT INTO managed_event_tags(tag_id,origin,mode,fingerprint,legacy_id) VALUES (?,?,?,?,?) ON CONFLICT(tag_id) DO UPDATE SET mode=excluded.mode,fingerprint=excluded.fingerprint,state='active'",
                      [target, p['origin'], p['mode'], fingerprint(tag, members), p.get('legacy_id')])
            c.executemany('INSERT OR IGNORE INTO event_processed_assets VALUES (?)', [(i,) for i in members])
            updated = self._redirect_rules(c, set(p.get('inputs', {})), target, p.get('legacy_id'))
        p['stage'] = 'cleanup'; self._save(key, p)
        for source in p.get('inputs', {}):
            if source == target: continue
            if any(t['id'] == source for t in self.remote.tags()):
                self._remove_tag(key, p, source)
            self.db.execute("UPDATE managed_event_tags SET state='merged' WHERE tag_id=?", [source])
            p.setdefault('deleted_sources', []).append(source); self._save(key, p)
        return {'id': target, 'asset_count': len(members), 'updated_albums': updated}

    def _remove_tag(self, key, p, tag_id):
        # Remove memberships before the tag so sidecar writers see the final list.
        # Journal the original membership first: interrupted removals are resumable.
        tag = self.remote.tag(tag_id)
        ids = {a['id'] for a in self.remote.tag_assets(tag_id)}
        clearing = p.setdefault('clearing', {})
        if tag_id not in clearing:
            if fingerprint(tag, ids) != p['inputs'][tag_id]:
                raise ImmichError('待删除标签已被修改，任务暂停')
            clearing[tag_id] = {'name': tag['name'], 'parentId': tag.get('parentId'), 'members': sorted(ids)}
            self._save(key, p)
        original = clearing[tag_id]
        if (tag['name'] != original['name'] or tag.get('parentId') != original['parentId']
                or not ids <= set(original['members'])):
            raise ImmichError('清理中的标签被人工修改，任务暂停')
        self._mark_processed(original['members'])
        self.remote.change_tag_assets(tag_id, ids, remove=True)
        self.remote.wait_tag_writes()
        if self.remote.tag_assets(tag_id):
            raise ImmichError('标签成员尚未移除，保留标签并稍后重试')
        self.remote.delete_tag(tag_id)
        self.remote.wait_tag_writes()

    @staticmethod
    def _redirect_rules(c, sources, target, legacy_id=None):
        updated = 0
        for row in c.execute('SELECT id,query_json FROM smart_albums').fetchall():
            query = json.loads(row['query_json'])
            ids = set(query.get('tag_ids', []))
            legacy = legacy_id is not None and query.get('event') == legacy_id
            if not (ids & sources or legacy): continue
            if legacy: query.pop('event')
            query['tag_ids'] = sorted((ids-sources) | {target})
            c.execute('UPDATE smart_albums SET query_json=?,materialized_at=NULL,matched_assets=NULL,error=NULL WHERE id=?', [json.dumps(query, ensure_ascii=False), row['id']])
            updated += 1
        return updated

    def _pause_rules(self, tag_id):
        for row in self.db.fetch_all('SELECT id,query_json FROM smart_albums'):
            if tag_id in json.loads(row['query_json']).get('tag_ids', []):
                self.db.execute('UPDATE smart_albums SET error=? WHERE id=?', ['关联事件标签已删除；规则暂停，请重新选择条件', row['id']])

    def retry(self):
        self._finish_root()
        errors, completed = [], 0
        for row in self.db.fetch_all('SELECT * FROM event_operations ORDER BY created_at,id'):
            try:
                self._execute(row['id'], json.loads(row['payload_json'])); completed += 1
            except Exception as error:
                errors.append(str(error))
        return {'completed': completed, 'errors': errors}

    def cancel(self, key):
        rows = self.db.fetch_all('SELECT payload_json FROM event_operations WHERE id=?', [key])
        if not rows: return
        p = json.loads(rows[0]['payload_json'])
        if p.get('legacy_id') is not None:
            raise ValueError('迁移任务不能取消；请处理上游错误后重试，旧数据仍保留')
        # No rollback of remote writes: freeze any surviving target as a manual event.
        target = p.get('target')
        catalog = {t['id']: t for t in self.remote.tags()}
        if target in catalog:
            ids = {a['id'] for a in self.remote.tag_assets(target)}
            self.db.execute("INSERT INTO managed_event_tags(tag_id,origin,mode,fingerprint) VALUES (?,?,'manual',?) ON CONFLICT(tag_id) DO UPDATE SET mode='manual',fingerprint=excluded.fingerprint",
                            [target, p['origin'], fingerprint(catalog[target], ids)])
            self._mark_processed(ids)
        self.db.execute('DELETE FROM event_operations WHERE id=?', [key])

    def rename(self, tag_id, name):
        if not name or not name.strip() or '/' in name:
            raise ValueError('事件名称不能为空或包含 /')
        return self._prepare('rename', [str(tag_id)], name.strip())

    def merge(self, tag_ids, name=None):
        ids = list(dict.fromkeys(str(i) for i in tag_ids))
        if len(ids) < 2 or len(ids) != len(tag_ids):
            raise ValueError('请选择至少两个不同事件')
        if name is not None and (not name.strip() or '/' in name):
            raise ValueError('事件名称不能为空或包含 /')
        return self._prepare('merge', ids, name.strip() if name else None)

    def delete(self, tag_id):
        tag_id = str(tag_id)
        rows = self.db.fetch_all('SELECT state FROM managed_event_tags WHERE tag_id=?', [tag_id])
        if rows and rows[0]['state'] == 'deleted': return {'id': tag_id, 'review_state': 'deleted'}
        return self._prepare('delete', [tag_id])

    def migrate(self):
        report = self.migration()
        if report['complete']: return report
        self.require_enabled()
        retry = self.retry()
        if retry['errors']: raise ImmichError('; '.join(retry['errors']))
        events = self.db.fetch_all('SELECT * FROM legacy_events ORDER BY id')
        for event in events:
            if self.db.fetch_all('SELECT tag_id FROM managed_event_tags WHERE legacy_id=?', [event['id']]): continue
            ids = {r['asset_id'] for r in self.db.fetch_all('SELECT asset_id FROM legacy_event_assets WHERE event_id=?', [event['id']])}
            self._mark_processed(ids)
            if event['review_state'] not in ('confirmed', 'pending'):
                tag_id = str(uuid4())
                with self.db.connect() as c:
                    c.execute("INSERT INTO managed_event_tags(tag_id,origin,mode,state,legacy_id) VALUES (?,?,'manual','deleted',?)", [tag_id, 'legacy:'+str(event['id']), event['id']])
                    self._redirect_rules(c, set(), tag_id, event['id'])
            else:
                if event['issue']: raise ImmichError(event['issue'])
                p = {'kind': 'create', 'target': None, 'name': event['name'], 'members': sorted(ids),
                     'inputs': {}, 'mode': 'automatic' if event['source'] == 'automatic' else 'manual',
                     'origin': 'legacy:'+str(event['id']), 'legacy_id': event['id'], 'stage': 'planned'}
                key = 'legacy-'+str(event['id']); self._save(key, p); self._execute(key, p)
            report['migrated'] = self.db.fetch_all('SELECT COUNT(*) AS n FROM managed_event_tags WHERE legacy_id IS NOT NULL')[0]['n']
            self.db.set_setting('event_tag_migration', json.dumps(report))
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute('SELECT COUNT(*) FROM event_operations').fetchone()[0]:
                raise ImmichError('事件迁移仍有未完成任务')
            c.execute('DROP TABLE legacy_event_assets'); c.execute('DROP TABLE legacy_events')
            report['migrated'] = c.execute('SELECT COUNT(*) FROM managed_event_tags WHERE legacy_id IS NOT NULL').fetchone()[0]
            report['complete'] = True
            c.execute('UPDATE settings SET value=? WHERE name=?', [json.dumps(report), 'event_tag_migration'])
        return report

    def build(self):
        self.require_enabled()
        self.migrate()
        retry = self.retry()
        if retry['errors']: raise ImmichError('; '.join(retry['errors']))
        snapshot = self._snapshot()
        processed = {r['asset_id'] for r in self.db.fetch_all('SELECT asset_id FROM event_processed_assets')}
        assets = self.remote.search({})
        fresh = {a['id'] for a in assets if a['id'] not in processed}
        automatic = {i: item for i, item in snapshot.items() if item['mode'] == 'automatic'}
        auto_ids = {i for item in automatic.values() for i in item['ids']}
        created = changed = 0
        for group in groups([a for a in assets if a['id'] in fresh | auto_ids]):
            ids = {i for _, i in group}
            new = ids & fresh
            if not new: continue
            matches = [i for i, item in automatic.items() if item['ids'] & ids]
            if matches:
                result = self._prepare('merge', matches, members=new, mode='automatic', snapshot=snapshot)
                changed += 1
            else:
                result = self._prepare('create', name=group[0][0].date().isoformat(), members=new, mode='automatic', snapshot=snapshot)
                created += 1
            fresh -= new
            for tag_id in matches: snapshot.pop(tag_id, None)
            tag = self.remote.tag(result['id']); members = self.remote.tag_assets(result['id'])
            member_ids = {a['id'] for a in members}
            snapshot[tag['id']] = {'tag': tag, 'assets': members, 'ids': member_ids,
                                   'fingerprint': fingerprint(tag, member_ids), 'mode': 'automatic'}
            automatic = {i: item for i, item in snapshot.items() if item['mode'] == 'automatic'}
        self.db.set_setting('events_built_at', datetime.now(timezone.utc).isoformat())
        return {'created': created, 'updated': changed, 'events': len(self.list())}

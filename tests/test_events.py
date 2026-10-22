import json
from uuid import uuid4

import pytest

from aura.db import Database
from aura.immich import ImmichError
from aura.service import AuraService
from conftest import A, B, C, TAG, FakeImmich


def test_local_events_never_write_remote_tags_or_create_albums(service, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('event operation wrote to Immich')
    for method in ('create_tag', 'rename_tag', 'change_tag_assets', 'delete_tag', 'create_album', 'wait_tag_writes'):
        monkeypatch.setattr(service.immich, method, forbidden)
    service.build_events()
    events = service.list_events()
    service.review_event(events[0]['id'], name='会议/旅行')
    merged = service.merge_events([e['id'] for e in events], '完整旅行')
    assert merged['asset_count'] == 2
    service.delete_event(merged['id'])
    assert service.build_events()['created'] == 0
    assert service.list_events() == []
    assert service.immich.tags() == [{'id': TAG, 'name': '海边', 'value': '海边'}]


def test_automatic_members_grow_manual_members_stay_fixed(service):
    service.build_events()
    event = next(e for e in service.list_events() if e['name'] == '2025-05-01')
    service.immich.assets.append({'id': C, 'localDateTime': '2025-05-01T09:00:00'})
    service.build_events()
    assert service.events.members(event['id']) == {A, C}
    service.review_event(event['id'], name='人工整理')
    extra = str(uuid4())
    service.immich.assets.append({'id': extra, 'localDateTime': '2025-05-01T10:00:00'})
    service.build_events()
    assert service.events.members(event['id']) == {A, C}
    assert any(service.events.members(e['id']) == {extra} for e in service.list_events())


def test_merge_redirects_both_query_forms_and_delete_pauses_album(service):
    service.build_events(); ids = [e['id'] for e in service.list_events()]
    first = service.save_album('one', {'event': ids[1]}, 'mirror')['id']
    second = service.save_album('all conditions', {'event_ids': ids}, 'mirror')['id']
    merged = service.merge_events(ids, '旅行')
    assert merged['updated_albums'] == 2
    assert service.materialize(first)['matched_assets'] == 2
    assert service.materialize(second)['matched_assets'] == 2
    remote = service.list_smart_albums()[0]['remote_id']
    service.delete_event(merged['id'])
    with pytest.raises(LookupError):
        service.materialize(second)
    assert service.immich.remote[remote]['assets'] == {A, B}
    assert service.build_events()['created'] == 0


def test_local_event_and_tag_conditions_are_independent(service):
    service.build_events()
    event = next(e for e in service.list_events() if A in service.events.members(e['id']))
    assert service.preview_album({'event_ids': [event['id']], 'tag_ids': [TAG]})['matched_assets'] == 1
    assert service.preview_album({'event_ids': [e['id'] for e in service.list_events()]})['matched_assets'] == 0
    service.immich.assets = []
    assert service.search_assets({'event': event['id']}) == []
    assert service.events.members(event['id']) == {A}  # No destructive pruning on visibility changes.


def v7_database(tmp_path, *, legacy=False):
    db = Database(tmp_path/'aura.db')
    db._initialize_v6()
    with db.connect() as c:
        c.execute('UPDATE schema_version SET version=7')
        c.execute('ALTER TABLE events RENAME TO legacy_events')
        c.execute('ALTER TABLE event_assets RENAME TO legacy_event_assets')
        if not legacy:
            c.execute('DROP TABLE legacy_event_assets'); c.execute('DROP TABLE legacy_events')
        c.execute('CREATE TABLE managed_event_tags(tag_id TEXT PRIMARY KEY,origin TEXT,mode TEXT,state TEXT,fingerprint TEXT,legacy_id INTEGER)')
        c.execute('CREATE TABLE event_processed_assets(asset_id TEXT PRIMARY KEY)')
        c.execute('CREATE TABLE event_operations(id TEXT PRIMARY KEY,payload_json TEXT,error TEXT,created_at TEXT)')
        c.execute("INSERT INTO managed_event_tags VALUES (?,'legacy:1','automatic','active',NULL,1)", [TAG])
        c.execute('INSERT INTO event_processed_assets VALUES (?)', [A])
        c.execute('INSERT INTO smart_albums(name,query_json) VALUES (?,?)', ['old rule', json.dumps({'tag_ids': [TAG]})])
    return db


def test_tag_migration_preserves_current_names_members_ids_and_rules(tmp_path):
    db = v7_database(tmp_path); remote = FakeImmich()
    remote.tag_rows[0]['name'] = '用户改过的名称'
    service = AuraService(db, remote)
    service.migrate_events()
    event = service.list_events()[0]
    assert (event['id'], event['name'], event['asset_count']) == (TAG, '用户改过的名称', 1)
    assert service.events.members(TAG) == {A}
    assert service.list_smart_albums()[0]['query'] == {'event_ids': [TAG]}
    assert remote.tags()[0]['name'] == '用户改过的名称'
    assert not remote.remote
    assert db.path.with_suffix('.pre-0.4.3.sqlite').exists()
    tables = {r['name'] for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {'managed_event_tags', 'event_operations', 'assets', 'people', 'tags'}
    service.migrate_events(); db.initialize()
    assert len(service.list_events()) == 1
    # Subsequent tag changes or disappearance no longer affect the migrated event.
    remote.tag_rows = []
    assert service.search_assets({'event': TAG})[0]['id'] == A


@pytest.mark.parametrize('failure', ['missing', 'read', 'pending'])
def test_tag_migration_failures_keep_original_data_and_pause_album(tmp_path, failure):
    db = v7_database(tmp_path); remote = FakeImmich()
    if failure == 'missing': remote.tag_rows = []
    if failure == 'read': remote.fail_search = True
    if failure == 'pending': db.execute("INSERT INTO event_operations VALUES ('pending','{}',NULL,'now')")
    service = AuraService(db, remote)
    with pytest.raises(ImmichError): service.migrate_events()
    assert db.fetch_all('SELECT * FROM events') == []
    assert db.fetch_all('SELECT * FROM managed_event_tags')
    assert service.list_smart_albums()[0]['query'] == {'tag_ids': [TAG]}
    with pytest.raises(ValueError): service.materialize(1)
    remote.tag_rows = FakeImmich().tag_rows; remote.fail_search = False
    if failure != 'pending':
        service.migrate_events()
        assert len(service.list_events()) == 1


def test_migration_handles_deleted_and_unmigrated_legacy_events(tmp_path):
    db = v7_database(tmp_path, legacy=True)
    deleted = str(uuid4())
    db.execute("INSERT INTO managed_event_tags VALUES (?,'legacy:2','manual','deleted',NULL,2)", [deleted])
    db.execute('INSERT INTO event_processed_assets VALUES (?)', [B])
    db.execute("INSERT INTO legacy_events(id,name,source) VALUES (3,'旧事件','manual')")
    db.execute('INSERT INTO legacy_event_assets VALUES (3,?)', [C])
    db.execute('INSERT INTO smart_albums(name,query_json) VALUES (?,?)', ['legacy rule', json.dumps({'event': 3})])
    service = AuraService(db, FakeImmich()); service.migrate_events()
    assert len(service.list_events()) == 2
    assert service.build_events()['created'] == 0
    event = next(e for e in service.list_events() if e['name'] == '旧事件')
    assert service.events.members(event['id']) == {C}
    assert service.list_smart_albums()[0]['query'] == {'event_ids': [event['id']]}
    with pytest.raises(LookupError): service.events.members(deleted)


def test_migration_transaction_rolls_back_and_can_retry(tmp_path):
    db = v7_database(tmp_path)
    second = str(uuid4())
    db.execute("INSERT INTO managed_event_tags VALUES (?,'new','automatic','active',NULL,NULL)", [second])
    remote = FakeImmich(); remote.tag_rows.append({'id': second, 'name': 'two', 'value': 'two'})
    remote.assets[1]['tag_ids'] = [second]
    service = AuraService(db, remote)
    db.execute("CREATE TRIGGER reject_second BEFORE INSERT ON events WHEN NEW.name='two' BEGIN SELECT RAISE(ABORT,'test failure'); END")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError): service.migrate_events()
    assert not db.fetch_all('SELECT * FROM events')
    assert len(db.fetch_all('SELECT * FROM managed_event_tags')) == 2
    assert service.list_smart_albums()[0]['query'] == {'tag_ids': [TAG]}
    db.execute('DROP TRIGGER reject_second')
    service.migrate_events()
    assert len(service.list_events()) == 2


def test_migration_preserves_external_manual_edits_and_ordinary_conditions(tmp_path):
    db = v7_database(tmp_path)
    db.execute('UPDATE managed_event_tags SET fingerprint=?', ['old fingerprint'])
    ordinary = str(uuid4())
    db.execute('UPDATE smart_albums SET query_json=?', [json.dumps({'tag_ids': [TAG, ordinary]})])
    remote = FakeImmich()
    remote.tag_rows.append({'id': ordinary, 'name': '普通标签', 'value': '普通标签'})
    service = AuraService(db, remote); service.migrate_events()
    assert service.list_events()[0]['source'] == 'manual'
    assert service.list_smart_albums()[0]['query'] == {'tag_ids': [ordinary], 'event_ids': [TAG]}

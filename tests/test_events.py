import json
from uuid import uuid4

import pytest

from aura.immich import ImmichError
from conftest import A, B, C


def test_event_names_and_membership_only_live_in_immich(service):
    service.build_events()
    events=service.list_events()
    assert len(events)==2 and all(e['source']=='automatic' for e in events)
    tables={r['name'] for r in service.database.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {'events','event_assets','legacy_events','legacy_event_assets'}
    columns={r['name'] for r in service.database.fetch_all('PRAGMA table_info(managed_event_tags)')}
    assert not columns & {'name','starts_at','asset_ids','query_json'}
    event=events[0]
    service.immich.rename_tag(event['id'],'Immich中的新名字')
    assert service.list_events()[0]['name']=='Immich中的新名字'
    assert service.list_events()[0]['source']=='manual'


def test_automatic_event_grows_and_rules_persist_tag_ids(service):
    service.build_events()
    event=next(e for e in service.list_events() if e['name']=='2025-05-01')
    album=service.save_album('自动事件',{'event':event['id']},'mirror')['id']
    assert service.list_smart_albums()[0]['query']=={'tag_ids':[event['id']]}
    service.immich.assets.append({'id':C,'localDateTime':'2025-05-01T09:00:00'})
    service.build_events()
    assert next(e for e in service.list_events() if e['id']==event['id'])['asset_count']==2
    assert service.materialize(album)['matched_assets']==2
    service.build_events();assert len(service.list_events())==2


def test_merge_retargets_rules_and_removes_old_tag_after_verification(service):
    service.build_events();ids=[e['id'] for e in service.list_events()]
    albums=[service.save_album('event',{'event':i},'mirror')['id'] for i in ids]
    remotes=[service.materialize(i)['remote_id'] for i in albums]
    service.immich.change_tag_assets(ids[1],{A,B})  # overlap is deduplicated
    result=service.merge_events(ids,'旅行')
    assert result['id']==ids[0] and result['asset_count']==2 and result['updated_albums']==2
    assert len(service.list_events())==1
    assert ids[1] not in {t['id'] for t in service.immich.tags()}
    for album,remote in zip(albums,remotes):
        assert service.materialize(album)['remote_id']==remote
        assert service.immich.remote[remote]['assets']=={A,B}
    service.build_events();assert len(service.list_events())==1
    assert service.database.fetch_all('SELECT id FROM event_operations')==[]


def test_delete_preserves_photos_and_album_and_suppresses_regeneration(service):
    service.build_events();event=service.list_events()[0]['id']
    album=service.save_album('keep',{'event':event},'mirror')['id']
    remote=service.materialize(album)['remote_id'];members=set(service.immich.remote[remote]['assets'])
    service.delete_event(event);service.delete_event(event);service.build_events()
    assert len(service.list_events())==1
    assert service.sync_albums()=={'albums':0,'paused':1,'errors':[]}
    assert service.immich.remote[remote]['assets']==members
    with pytest.raises(LookupError):service.search_assets({'event':event})
    with pytest.raises(LookupError):service.review_event(event,name='restore')
    assert len(service.immich.assets)==2


def test_external_deletion_and_member_edits_are_respected(service):
    service.build_events()
    events=service.list_events();first=events[0]['id'];second=events[1]['id']
    service.immich.delete_tag(first)
    service.immich.change_tag_assets(second,{A,B},remove=True)
    service.build_events()
    assert first not in {e['id'] for e in service.list_events()}
    assert len(service.list_events())==1 and service.list_events()[0]['asset_count']==0
    service.build_events();assert len(service.list_events())==1


def test_manual_rename_does_not_get_overwritten(service):
    service.build_events();event=service.list_events()[0]['id']
    service.review_event(event,name='自定义名称');service.build_events()
    assert next(e for e in service.list_events() if e['id']==event)['name']=='自定义名称'


def test_merge_failure_keeps_sources_and_retries_same_target(service,monkeypatch):
    service.build_events();ids=[e['id'] for e in service.list_events()]
    original=service.immich.change_tag_assets
    def fail(*a,**kw):raise ImmichError('temporary failure')
    monkeypatch.setattr(service.immich,'change_tag_assets',fail)
    with pytest.raises(ImmichError):service.merge_events(ids,'旅行')
    assert all(i in {t['id'] for t in service.immich.tags()} for i in ids)
    assert len(service.database.fetch_all('SELECT * FROM event_operations'))==1
    monkeypatch.setattr(service.immich,'change_tag_assets',original)
    assert service.process_event_operations()['errors']==[]
    assert service.list_events()[0]['id']==ids[0] and len(service.list_events())==1


def test_lost_create_response_is_recovered_without_duplicate(service,monkeypatch):
    original=service.immich.create_tag
    failed=False
    def flaky(name,parent_id=None):
        nonlocal failed
        result=original(name,parent_id)
        if parent_id and not failed:
            failed=True;raise ImmichError('response lost')
        return result
    monkeypatch.setattr(service.immich,'create_tag',flaky)
    with pytest.raises(ImmichError):service.build_events()
    service.build_events()
    assert len(service.list_events())==2
    assert len([t for t in service.immich.tags() if t.get('parentId')])==2


def test_failed_operation_can_be_cancelled_without_undoing_user_edits(service,monkeypatch):
    service.build_events();ids=[e['id'] for e in service.list_events()]
    original=service.immich.change_tag_assets
    monkeypatch.setattr(service.immich,'change_tag_assets',lambda *a,**kw: (_ for _ in ()).throw(ImmichError('offline')))
    with pytest.raises(ImmichError):service.merge_events(ids)
    monkeypatch.setattr(service.immich,'change_tag_assets',original)
    service.immich.rename_tag(ids[1],'人工改名')
    assert service.process_event_operations()['errors']
    key=service.database.fetch_all('SELECT id FROM event_operations')[0]['id']
    service.cancel_event_operation(key)
    assert not service.database.fetch_all('SELECT * FROM event_operations')
    assert service.immich.tag(ids[1])['name']=='人工改名'


def test_ordinary_tags_never_become_managed_events(service):
    ordinary=service.immich.create_tag('事件')
    child=service.immich.create_tag('私人活动',ordinary['id'])
    service.immich.change_tag_assets(child['id'],{A})
    service.build_events()
    assert child['id'] not in {e['id'] for e in service.list_events()}
    with pytest.raises(LookupError):service.delete_event(child['id'])
    assert service.immich.tag(child['id'])['name']=='私人活动'


def test_schema6_migration_preserves_members_and_rules(tmp_path):
    from aura.db import Database
    from aura.service import AuraService
    from conftest import FakeImmich
    db=Database(tmp_path/'old.db');db._initialize_v6()
    db.execute("INSERT INTO events(id,name,starts_at,source) VALUES (1,'旅行','2025-05-01','manual')")
    db.execute('INSERT INTO event_assets VALUES (1,?)',[A])
    db.execute("INSERT INTO smart_albums(name,query_json) VALUES ('旧相册',?)",[json.dumps({'event':1})])
    service=AuraService(db,FakeImmich())
    assert db.path.with_suffix('.pre-0.4.2.sqlite').exists()
    assert service.event_tags.migration()['complete'] is False
    with pytest.raises(ValueError):service.preview_album({'event':str(uuid4())})
    service.process_event_operations()
    events=service.list_events();assert len(events)==1 and events[0]['name']=='旅行'
    assert service.list_smart_albums()[0]['query']=={'tag_ids':[events[0]['id']]}
    assert {a['id'] for a in service.immich.tag_assets(events[0]['id'])}=={A}
    assert service.event_tags.migration()['complete'] is True
    tables={r['name'] for r in db.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {'events','event_assets','legacy_events','legacy_event_assets'}
    assert db.fetch_all('SELECT * FROM event_operations')==[]


def test_migration_failure_keeps_old_tables_until_verified(tmp_path,monkeypatch):
    from aura.db import Database
    from aura.service import AuraService
    from conftest import FakeImmich
    db=Database(tmp_path/'old.db');db._initialize_v6()
    db.execute("INSERT INTO events(id,name) VALUES (1,'day')")
    db.execute('INSERT INTO event_assets VALUES (1,?)',[A])
    service=AuraService(db,FakeImmich());original=service.immich.change_tag_assets
    monkeypatch.setattr(service.immich,'change_tag_assets',lambda *a,**kw: (_ for _ in ()).throw(ImmichError('offline')))
    with pytest.raises(ImmichError):service.process_event_operations()
    assert db.fetch_all('SELECT * FROM legacy_event_assets')
    monkeypatch.setattr(service.immich,'change_tag_assets',original)
    service.process_event_operations()
    assert service.event_tags.migration()['complete'] and len(service.list_events())==1


def test_disabled_tag_migration_never_writes_upstream(service,monkeypatch):
    monkeypatch.delenv('AURA_EVENT_TAGS_ENABLED')
    original=service.immich.tags()
    with pytest.raises(ImmichError,match='未启用'):service.build_events()
    assert service.immich.tags()==original
    assert not service.database.fetch_all('SELECT * FROM managed_event_tags')


def test_source_deleted_after_cleanup_failure_retries_safely(service,monkeypatch):
    service.build_events();ids=[e['id'] for e in service.list_events()]
    original=service.immich.delete_tag
    def lost_reply(tag_id):
        original(tag_id)
        raise ImmichError('response lost after delete')
    monkeypatch.setattr(service.immich,'delete_tag',lost_reply)
    with pytest.raises(ImmichError):service.merge_events(ids,'旅行')
    monkeypatch.setattr(service.immich,'delete_tag',original)
    assert service.process_event_operations()['errors']==[]
    assert len(service.list_events())==1


def test_manual_member_change_during_cleanup_is_not_overwritten(service,monkeypatch):
    service.build_events();ids=[e['id'] for e in service.list_events()]
    original=service.immich.delete_tag
    monkeypatch.setattr(service.immich,'delete_tag',lambda tag_id: (_ for _ in ()).throw(ImmichError('offline')))
    with pytest.raises(ImmichError):service.merge_events(ids,'旅行')
    service.immich.change_tag_assets(ids[0],{A},remove=True)
    monkeypatch.setattr(service.immich,'delete_tag',original)
    assert service.process_event_operations()['errors']
    assert A not in {a['id'] for a in service.immich.tag_assets(ids[0])}
    assert any(t['id']==ids[1] for t in service.immich.tags())


def test_missing_queue_permission_blocks_migration_before_any_tag_write(service, monkeypatch):
    before = service.immich.tags()
    def denied():
        raise ImmichError('queue permission denied')
    monkeypatch.setattr(service.immich, 'wait_tag_writes', denied)
    with pytest.raises(ImmichError, match='permission denied'):
        service.build_events()
    assert service.immich.tags() == before
    assert not service.database.fetch_all('SELECT * FROM managed_event_tags')


def test_delete_resumes_after_members_removed_but_reply_lost(service, monkeypatch):
    service.build_events()
    event = service.list_events()[0]['id']
    original = service.immich.change_tag_assets
    def lost_reply(tag_id, ids, *, remove=False):
        original(tag_id, ids, remove=remove)
        if remove:
            raise ImmichError('reply lost after untag')
    monkeypatch.setattr(service.immich, 'change_tag_assets', lost_reply)
    with pytest.raises(ImmichError):
        service.delete_event(event)
    assert service.immich.tag_assets(event) == []
    monkeypatch.setattr(service.immich, 'change_tag_assets', original)
    assert service.process_event_operations()['errors'] == []
    assert event not in {e['id'] for e in service.list_events()}


def test_waits_between_rename_add_remove_and_delete(service, monkeypatch):
    service.build_events()
    ids = [e['id'] for e in service.list_events()]
    pending = []
    def drain():
        pending.clear()
    monkeypatch.setattr(service.immich, 'wait_tag_writes', drain)
    for name in ('rename_tag', 'change_tag_assets', 'delete_tag'):
        original = getattr(service.immich, name)
        def mutate(*args, _name=name, _original=original, **kwargs):
            assert not pending, 'another tag mutation started before XMP writes drained'
            result = _original(*args, **kwargs)
            pending.append(_name)
            return result
        monkeypatch.setattr(service.immich, name, mutate)
    service.merge_events(ids, '旅行')
    assert not pending

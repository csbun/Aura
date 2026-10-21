import json
import sqlite3
from pathlib import Path

import pytest

from aura.db import Database
from aura.immich import ImmichError
from aura.service import AuraService
from aura.worker import run_once
from aura.config import Settings
from conftest import A,B,C,PERSON,TAG,REMOTE,FakeImmich


def test_no_photo_replica_and_live_filters(service):
    tables = {r['name'] for r in service.database.fetch_all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {'assets','people','tags','external_assets','asset_people','enrichment_state'}
    assert service.preview_album({'person_ids':[PERSON]})['matched_assets']==1
    service.immich.assets[1]['person_ids']=[PERSON]
    assert service.preview_album({'person_ids':[PERSON]})['matched_assets']==2
    service.immich.people_rows[0]['name']='新名字'
    assert service.immich.people()[0]['name']=='新名字'


def test_event_review_identity_and_outage(service):
    assert service.build_events()['created']==2
    events=service.list_events()
    event=next(e for e in events if e['name']=='2025-05-01')
    service.review_event(event['id'],'confirmed','旅行')
    service.immich.assets.append({'id':C,'localDateTime':'2025-05-01T09:00:00'})
    service.build_events()
    assert next(e for e in service.list_events() if e['id']==event['id'])['name']=='旅行'
    assert service.database.fetch_all('SELECT asset_id FROM event_assets WHERE event_id=?',[event['id']])==[{'asset_id':A}]
    before=service.list_events()
    service.immich.fail_search=True
    with pytest.raises(ImmichError):service.build_events()
    assert service.list_events()==before


def test_empty_intersection_preview(service):
    service.build_events()
    event=next(e for e in service.list_events() if e['name']=='2025-05-02')
    result=service.preview_album({'person_ids':[PERSON],'event':event['id']})
    assert result['matched_assets']==0
    assert result['condition_counts']=={'person_ids':1,'event':1}


def test_managed_membership_edit_and_retry(service):
    album=service.save_album('person',{'person_ids':[PERSON]},'mirror')['id']
    result=service.materialize(album);remote=result['remote_id']
    assert service.immich.remote[remote]['assets']=={A}
    assert service.materialize(album)['added']==0
    service.immich.assets[0]['person_ids']=[]
    service.immich.assets[1]['person_ids']=[PERSON]
    service.immich.fail_add=True
    with pytest.raises(ImmichError):service.materialize(album)
    assert service.immich.remote[remote]['assets']=={A}
    service.immich.fail_add=False
    result=service.materialize(album)
    assert result['added']==1 and result['removed']==1
    assert service.immich.remote[remote]['assets']=={B}
    assert len(service.immich.remote)==1
    service.save_album('person',{'tag_ids':[TAG]},'mirror',album)
    assert service.materialize(album)['remote_id']==remote
    assert service.immich.remote[remote]['assets']=={A}


def test_search_failure_never_clears_album(service):
    album=service.save_album('all',{},'mirror')['id'];remote=service.materialize(album)['remote_id']
    service.immich.fail_search=True
    with pytest.raises(ImmichError):service.materialize(album)
    assert service.immich.remote[remote]['assets']=={A,B}


def test_unknown_version_and_changed_owner_block_writes(service):
    album=service.save_album('all',{},'mirror')['id']
    service.immich.version='4.0.0'
    with pytest.raises(ImmichError):service.materialize(album)
    assert not service.immich.remote
    service.immich.version='3.3.4';service.immich.user='other'
    with pytest.raises(ImmichError,match='所属用户'):service.materialize(album)
    assert not service.immich.remote


def test_append_mode_preserved(service):
    album=service.save_album('all',{},'append')['id'];remote=service.materialize(album)['remote_id']
    service.immich.assets=[]
    service.materialize(album)
    assert service.immich.remote[remote]['assets']=={A,B}


def test_recover_ambiguous_album_creation_without_name_adoption(service):
    album=service.save_album('same name',{},'mirror')['id']
    marker=f"Aura rule {service.database.get_setting('instance_id')}:{album}"
    original=service.immich.create_album('same name','unrelated')
    recovered=service.immich.create_album('same name',marker)
    assert service.materialize(album)['remote_id']==recovered
    assert service.immich.remote[original]['assets']==set()
    assert len(service.immich.remote)==2


def test_rejected_event_pauses_existing_album(service):
    service.build_events();event=service.list_events()[0]['id']
    service.review_event(event,'confirmed')
    album=service.save_album('event',{'event':event},'mirror')['id'];service.materialize(album)
    service.review_event(event,'rejected')
    with pytest.raises(ValueError,match='尚未确认'):service.materialize(album)


def test_schedule_does_not_rescan_for_every_sync(service):
    settings=Settings(service.database.path)
    run_once(service,settings)
    assert len([c for c in service.immich.calls if c[0]=='search'])==1
    run_once(service,settings)
    assert len([c for c in service.immich.calls if c[0]=='search'])==1
    assert service.database.fetch_all("SELECT state FROM task_status WHERE name='albums'")[0]['state']=='complete'


def legacy_database(path):
    with sqlite3.connect(path) as c:
        c.executescript(Path('tests/fixtures/legacy-v4.sql').read_text())
        c.execute("INSERT INTO assets(id,path,sha256,size_bytes,mtime_ns,indexed_at) VALUES (1,'/photos/a.jpg','hash',1,1,'now')")
        c.execute("INSERT INTO external_assets VALUES (1,'immich',?,'now')",[A])
        c.execute("INSERT INTO people(id,name,confirmed,created_at,name_locked) VALUES (1,'本地名字',1,'now',1)")
        c.execute('INSERT INTO external_people VALUES (?,1)',[PERSON])
        c.execute("INSERT INTO events(id,name,confirmed,review_state,created_at) VALUES (8,'旅行',1,'confirmed','now')")
        c.execute('INSERT INTO event_assets(event_id,asset_id) VALUES (8,1)')
        c.execute("INSERT INTO smart_albums(id,name,query_json,created_at) VALUES (4,'旅行人物',?,'now')",[json.dumps({'person_id':1,'event':8})])
        c.execute("INSERT INTO external_albums VALUES (4,'immich',?,'now')",[REMOTE])


def test_legacy_migration_archives_and_preserves_ids(tmp_path):
    path=tmp_path/'aura.db';legacy_database(path)
    service=AuraService(Database(path),FakeImmich())
    album=service.list_smart_albums()[0]
    assert album['id']==4 and album['remote_id']==REMOTE and album['sync_mode']=='append'
    assert album['query']=={'person_ids':[PERSON],'event':8}
    assert album['migration_issue']
    assert service.list_events()[0]['id']==8
    assert service.database.fetch_all('SELECT asset_id FROM event_assets')==[{'asset_id':A}]
    archive=path.with_suffix('.pre-0.4.sqlite')
    with sqlite3.connect(archive) as c:assert c.execute('SELECT COUNT(*) FROM assets').fetchone()[0]==1
    report=json.loads(service.database.get_setting('migration_report'))
    assert report['local_names'][0]['name']=='本地名字'
    assert report['paused_albums']==1
    assert service.sync_albums()['paused']==1
    service.database.initialize()
    assert len(service.list_smart_albums())==1
    service.save_album('旅行人物',album['query'],'append',4)
    assert not service.list_smart_albums()[0]['migration_issue']


def test_missing_mappings_are_blocked_not_widened(tmp_path):
    path=tmp_path/'aura.db';legacy_database(path)
    with sqlite3.connect(path) as c:
        c.execute('DELETE FROM external_people')
        c.execute('DELETE FROM external_assets')
    service=AuraService(Database(path),FakeImmich())
    assert service.list_events()[0]['issue']
    assert service.list_smart_albums()[0]['migration_issue']
    with pytest.raises(ValueError):service.materialize(4)
    assert not service.immich.remote


def test_future_schema_refused_without_changes(tmp_path):
    path=tmp_path/'aura.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE schema_version(version INTEGER)');c.execute('INSERT INTO schema_version VALUES(100)')
    with pytest.raises(RuntimeError,match='newer'):Database(path).initialize()
    with sqlite3.connect(path) as c:assert c.execute('SELECT version FROM schema_version').fetchone()[0]==100


def test_natural_language_uses_live_ids_and_flags_ambiguity(service):
    plan=service.parse_search('2025年小明的沙滩照片')
    assert plan['query']['person_ids']==[PERSON]
    assert plan['query']['q']=='沙滩'
    assert plan['query']['start'].startswith('2025')
    service.immich.people_rows.append({'id':REMOTE,'name':'小明'})
    plan=service.parse_search('小明')
    assert plan['ambiguous_names'] and 'person_ids' not in plan['query']


def test_migration_transaction_rolls_back_on_failure(tmp_path,monkeypatch):
    path=tmp_path/'aura.db';legacy_database(path)
    import aura.db as module
    monkeypatch.setattr(module,'SCHEMA',[*module.SCHEMA,'INVALID SQL'])
    with pytest.raises(sqlite3.OperationalError):Database(path).initialize()
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT version FROM schema_version').fetchone()[0]==4
        assert c.execute('SELECT COUNT(*) FROM assets').fetchone()[0]==1
    assert path.with_suffix('.pre-0.4.sqlite').exists()


def test_one_time_name_migration_resumes_only_safe_rules(tmp_path,monkeypatch):
    path=tmp_path/'aura.db';legacy_database(path)
    service=AuraService(Database(path),FakeImmich())
    calls=[]
    def migrate(person_id,name):
        calls.append((person_id,name));return 'done'
    monkeypatch.setattr(service.immich,'migrate_person_name',migrate,raising=False)
    assert service.finish_migration()['migrated_names']==1
    assert not service.list_smart_albums()[0]['migration_issue']
    service.finish_migration()
    assert len(calls)==1
    assert json.loads(service.database.get_setting('migration_report'))['names_finished']


def test_album_creation_persists_before_membership_failure(service):
    album=service.save_album('new',{},'mirror')['id']
    service.immich.fail_add=True
    with pytest.raises(ImmichError):service.materialize(album)
    remote=service.list_smart_albums()[0]['remote_id']
    assert remote
    service.immich.fail_add=False
    assert service.materialize(album)['remote_id']==remote
    assert len(service.immich.remote)==1


def test_name_migration_failure_retries_without_unpausing(tmp_path,monkeypatch):
    path=tmp_path/'aura.db';legacy_database(path)
    service=AuraService(Database(path),FakeImmich())
    def fail(*args):raise ImmichError('no permission')
    monkeypatch.setattr(service.immich,'migrate_person_name',fail,raising=False)
    assert service.finish_migration()['pending']==1
    assert service.list_smart_albums()[0]['migration_issue']
    monkeypatch.setattr(service.immich,'migrate_person_name',lambda *args:'done')
    assert service.finish_migration()['pending']==0
    assert service.list_smart_albums()[0]['migration_issue'] is None

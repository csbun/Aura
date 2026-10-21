from pathlib import Path

import pytest

from aura.db import Database, SCHEMA
from aura.events import build_day_candidates
from aura.immich import ImmichClient, ImmichError, ImmichSettings
from aura.service import AuraService
from aura.config import Settings, ImmichConfig
from aura.worker import run_once, _run_task


def add_asset(db, path, captured='2026-10-01T10:00:00+00:00'):
    return db.execute("INSERT INTO assets(path, sha256, size_bytes, mtime_ns, captured_at, indexed_at) VALUES (?, 'hash', 1, 1, ?, 'now')", [path, captured]).lastrowid


@pytest.fixture
def service(tmp_path):
    return AuraService(Database(tmp_path / 'aura.db'))


def test_review_survives_rescan_and_earlier_new_event(service):
    db = service.database
    first = add_asset(db, '/photos/first.jpg')
    second = add_asset(db, '/photos/second.jpg', '2026-10-02T10:00:00+00:00')
    assert build_day_candidates(db) == 2
    events = service.list_events()
    confirmed, rejected = events[0]['id'], events[1]['id']
    service.review_event(confirmed, 'confirmed', '旅行')
    service.review_event(rejected, 'rejected')
    add_asset(db, '/photos/earlier.jpg', '2025-01-01T10:00:00+00:00')
    assert build_day_candidates(db) == 1
    assert build_day_candidates(db) == 0
    assert service.search_assets(event=confirmed)[0]['id'] == first
    assert service.search_assets(event=rejected)[0]['id'] == second
    assert db.fetch_all('SELECT name,review_state FROM events WHERE id=?', [confirmed]) == [{'name':'旅行','review_state':'confirmed'}]
    assert len(service.list_events()) == 3


def test_pending_ids_stay_stable_and_superseded_ids_not_reused(service):
    db = service.database
    asset = add_asset(db, '/photos/one.jpg')
    build_day_candidates(db)
    event = service.list_events()[0]['id']
    add_asset(db, '/photos/earlier.jpg', '2020-01-01T10:00:00+00:00')
    build_day_candidates(db)
    assert service.search_assets(event=event)[0]['id'] == asset
    db.execute('UPDATE assets SET missing_at="now" WHERE id=?', [asset])
    build_day_candidates(db)
    assert db.fetch_all('SELECT review_state FROM events WHERE id=?', [event])[0]['review_state'] == 'superseded'
    add_asset(db, '/photos/new.jpg', '2030-01-01T10:00:00+00:00')
    build_day_candidates(db)
    assert service.search_assets(event=event) == []


def test_v2_upgrade_preserves_decisions(tmp_path):
    db = Database(tmp_path/'old.db')
    with db.connect() as connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT INTO schema_version VALUES (2)')
        connection.execute("INSERT INTO events(name,confirmed,created_at) VALUES ('kept',1,'now')")
        asset = connection.execute("INSERT INTO assets(path,sha256,size_bytes,mtime_ns,indexed_at) VALUES ('p','h',1,1,'now')").lastrowid
        tag = connection.execute("INSERT INTO tags(name,created_at) VALUES ('t','now')").lastrowid
        connection.execute('INSERT INTO asset_tags(asset_id,tag_id) VALUES (?,?)', [asset,tag])
    db.initialize()
    db.initialize()
    assert db.fetch_all('SELECT review_state FROM events')[0]['review_state'] == 'confirmed'
    assert db.fetch_all('SELECT review_state FROM asset_tags')[0]['review_state'] == 'confirmed'
    assert db.fetch_all('SELECT version FROM schema_version') == [{'version':4}]


def test_tag_review_controls_search(service):
    db=service.database
    asset=add_asset(db, '/photos/one.jpg')
    tag=service.create_tag('海边')['id']
    db.execute("INSERT INTO asset_tags(asset_id,tag_id,source) VALUES (?,?,'model')",[asset,tag])
    assert service.search_assets(tag='海边') == []
    service.review_tag(asset,tag,'confirmed')
    assert len(service.search_assets(tag='海边')) == 1
    service.review_tag(asset,tag,'rejected')
    build_day_candidates(db)
    assert service.search_assets(tag='海边') == []
    assert service.list_tag_assignments()[0]['review_state'] == 'rejected'


class FakeImmich:
    def __init__(self):
        self.created=[]
        self.members={}
        self.fail=False

    def create_album(self,name):
        album={'id':str(len(self.created)+1),'albumName':name}
        self.created.append(album)
        self.members[album['id']]=set()
        return album

    def add_assets(self,album_id,ids):
        if self.fail:
            raise ImmichError('offline')
        self.members[album_id].update(ids)


def test_album_retry_uses_saved_identity_even_after_rename(service):
    asset=add_asset(service.database,'/photos/one.jpg')
    service.map_external_asset(asset,'immich','remote-photo')
    album=service.create_smart_album('旅行',{})['id']
    remote=FakeImmich()
    remote.create_album('旅行')  # unrelated user album must remain untouched
    remote.fail=True
    with pytest.raises(ImmichError):
        service.materialize_smart_album(album,remote)
    service.database.execute('UPDATE smart_albums SET name="renamed" WHERE id=?',[album])
    remote.fail=False
    service.materialize_smart_album(album,remote)
    service.materialize_smart_album(album,remote)
    assert len(remote.created)==2
    assert remote.members=={'1':set(),'2':{'remote-photo'}}


def test_mapping_uses_nested_relative_path_not_basename(service):
    a=add_asset(service.database,'/mnt/nas/trip/a.jpg')
    b=add_asset(service.database,'/mnt/nas/a.jpg')
    class Remote:
        def all_assets(self):
            return [{'id':'root','originalPath':'/photos/a.jpg'}, {'id':'nested','originalPath':'/photos/trip/a.jpg'}]
    result=service.sync_immich_asset_mappings(Remote(),'/photos','/mnt/nas')
    assert result['matched']==2
    assert {row['asset_id']:row['external_id'] for row in service.database.fetch_all('SELECT * FROM external_assets')}=={a:'nested',b:'root'}


def test_partial_album_response_is_error(monkeypatch):
    client=ImmichClient(ImmichSettings('http://localhost','key'))
    monkeypatch.setattr(client,'_request',lambda *args:[{'id':'a','success':False,'error':'duplicate'}])
    client.add_assets('album',['a'])
    with pytest.raises(ImmichError):
        client.add_assets('album',['a','b'])


def test_worker_runs_without_immich_and_records_failures(tmp_path):
    from PIL import Image
    photos=tmp_path/'photos'; photos.mkdir()
    Image.new('RGB',(10,10)).save(photos/'one.jpg')
    settings=Settings(photos,tmp_path/'aura.db')
    run_once(settings,ImmichConfig('','','/photos'))
    service=AuraService(Database(settings.db_path))
    status=service.task_status()
    assert status['assets']==1
    assert all(t['state']=='complete' for t in status['tasks'])
    def fail():
        raise ImmichError('offline')
    assert not _run_task(service.database,'sync',fail)
    assert len(service.search_assets())==1
    assert next(t for t in service.task_status()['tasks'] if t['name']=='sync')['error']=='offline'


def test_concurrent_album_sync_creates_once(service):
    from concurrent.futures import ThreadPoolExecutor
    asset=add_asset(service.database,'/photos/one.jpg')
    service.map_external_asset(asset,'immich','remote-photo')
    album=service.create_smart_album('旅行',{})['id']
    remote=FakeImmich()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: service.materialize_smart_album(album,remote), range(2)))
    assert len(remote.created)==1
    assert remote.members['1']=={'remote-photo'}


def test_worker_sync_does_not_wait_for_daily_scan(tmp_path,monkeypatch):
    from aura import worker
    photos=tmp_path/'photos'; photos.mkdir()
    monkeypatch.setenv('PHOTO_ROOT',str(photos))
    monkeypatch.setenv('AURA_DB_PATH',str(tmp_path/'worker.db'))
    monkeypatch.setenv('AURA_SCAN_INTERVAL','86400')
    calls=[]
    monkeypatch.setattr(worker,'scan_library',lambda *args: calls.append('scan') or {})
    monkeypatch.setattr(worker,'sync_albums',lambda *args: calls.append('sync') or {})
    clock=[0]
    monkeypatch.setattr(worker.time,'monotonic',lambda:clock[0])
    def sleep(seconds):
        clock[0]+=seconds
        if clock[0]>=120:
            raise SystemExit
    monkeypatch.setattr(worker.time,'sleep',sleep)
    with pytest.raises(SystemExit):
        worker.main()
    assert calls==['scan','sync','sync']

from datetime import datetime, timedelta, timezone
import json

import pytest

from aura.db import Database
from aura.enrichment import RecognitionSync, generate_metadata_tags
from aura.immich import ImmichClient, ImmichError, ImmichSettings
from aura.service import AuraService


@pytest.fixture
def service(tmp_path):
    return AuraService(Database(tmp_path / 'aura.db'))


def asset(service, name='one', remote='remote-one'):
    row = service.database.execute("INSERT INTO assets(path,sha256,size_bytes,mtime_ns,captured_at,width,height,indexed_at) VALUES (?,'hash',1,1,'2026-10-01T10:00:00+00:00',640,480,'now')", [f'/photos/{name}.jpg']).lastrowid
    service.map_external_asset(row,'immich',remote)
    return row


def detail(remote='remote-one', name='小明', tags=True):
    return {'id':remote,'people':[{'id':'person-one','name':name}], 'tags':[{'id':'tag-one','name':'海边','value':'旅行/海边'}] if tags else []}


class Remote:
    def __init__(self, records):
        self.records=records
        self.calls=[]

    def server_version(self):
        return '3.3.4'

    def asset_info(self, key):
        self.calls.append(key)
        result=self.records[key]
        if isinstance(result, Exception):
            raise result
        return result


def test_import_filters_and_provenance(service):
    a=asset(service)
    sync=RecognitionSync(service.database)
    sync.apply_asset(a,detail(),'3.3.4')
    assert len(service.search_assets(person='小明',tag='旅行/海边'))==1
    relation=service.list_person_assignments()[0]
    assert relation['source']=='immich'
    assert relation['producer_version']=='3.3.4'
    assert relation['confidence'] is None and relation['model_version'] is None
    assert relation['reviewed']==0
    assert service.list_tag_assignments()[0]['review_state']=='confirmed'


def test_rename_correction_and_rejection_survive_remote_changes(service):
    a=asset(service)
    sync=RecognitionSync(service.database)
    sync.apply_asset(a,detail(),'3.3.4')
    p=service.list_people()[0]['id']; t=service.list_tags()[0]['id']
    album=service.create_smart_album('旅行',{'person':'小明','tag':'旅行/海边'})
    service.rename_person(p,'小明本地')
    service.review_tag(a,t,'rejected')
    sync.apply_asset(a,detail(name='远端改名'),'3.4.0')
    assert service.list_people()[0]['name']=='小明本地'
    assert service.search_assets(tag='旅行/海边')==[]
    assert json.loads(album['query_json'])=={'person_id':p,'tag_id':t}
    replacement=service.create_person('小红',True)['id']
    service.correct_person(a,p,replacement)
    sync.apply_asset(a,detail(),'3.4.0')
    assert service.search_assets(person_id=p)==[]
    assert len(service.search_assets(person='小红'))==1
    assert service.list_person_assignments('rejected')[0]['reviewed']==1


def test_unknown_people_can_be_named_without_losing_cluster(service):
    a=asset(service)
    sync=RecognitionSync(service.database)
    sync.apply_asset(a,detail(name=''),'3.3.4')
    p=service.list_people()[0]['id']
    assert service.search_assets(person_id=p)==[]
    service.rename_person(p,'家人')
    assert len(service.search_assets(person_id=p))==1
    sync.apply_asset(a,detail(name=''),'3.3.4')
    assert len(service.list_people())==1
    assert service.list_people()[0]['name']=='家人'


def test_disappearing_remote_relations_preserve_only_manual_decisions(service):
    a=asset(service)
    sync=RecognitionSync(service.database)
    sync.apply_asset(a,detail(),'3.3.4')
    p=service.list_people()[0]['id']; t=service.list_tags()[0]['id']
    service.review_tag(a,t,'confirmed')
    sync.apply_asset(a,{'id':'remote-one','people':[],'tags':[]},'3.3.4')
    assert service.search_assets(person_id=p)==[]
    assert len(service.search_assets(tag_id=t))==1


def test_metadata_tags_are_automatic_and_keep_rejections(service):
    a=asset(service)
    generate_metadata_tags(service.database)
    assert len(service.search_assets(tag='系统/年份/2026'))==1
    tag=next(t for t in service.list_tags() if t['name']=='系统/画幅/横图')
    service.review_tag(a,tag['id'],'rejected')
    generate_metadata_tags(service.database)
    assert service.search_assets(tag_id=tag['id'])==[]
    assert len(service.list_tags())==2
    service.database.execute('UPDATE assets SET width=10,height=100 WHERE id=?',[a])
    generate_metadata_tags(service.database)
    assert len(service.search_assets(tag='系统/画幅/竖图'))==1


def test_restart_resumes_and_failure_does_not_starve_other_assets(service):
    for n in range(3): asset(service,str(n),str(n))
    remote=Remote({'0':ImmichError('offline'), '1':detail('1'), '2':detail('2')})
    result=RecognitionSync(service.database).run_batch(remote,limit=2)
    assert result['processed']==1 and result['failed']==1
    result=RecognitionSync(service.database).run_batch(remote,limit=2)
    assert remote.calls==['0','1','2']
    assert result['completed']==2
    service.database.execute("UPDATE enrichment_state SET next_attempt_at='2000-01-01' WHERE error IS NOT NULL")
    remote.records['0']=detail('0')
    result=RecognitionSync(service.database).run_batch(remote)
    assert result['completed']==3 and result['failed']==0
    assert remote.calls==['0','1','2','0']


def test_budget_and_changed_mapping(service):
    a=asset(service)
    remote=Remote({'remote-one':detail(),'remote-two':detail('remote-two')})
    sync=RecognitionSync(service.database)
    assert sync.run_batch(remote,budget_seconds=0)['processed']==0
    assert remote.calls==[]
    sync.run_batch(remote)
    service.map_external_asset(a,'immich','remote-two')
    with pytest.raises(ValueError,match='mapping changed'):
        sync.apply_asset(a,detail(),'3.3.4')
    assert sync.run_batch(remote)['processed']==1
    assert remote.calls==['remote-one','remote-two']


def test_malformed_response_does_not_clear_saved_relationships(service,monkeypatch):
    a=asset(service)
    sync=RecognitionSync(service.database)
    sync.apply_asset(a,detail(),'3.3.4')
    service.database.execute("UPDATE enrichment_state SET next_attempt_at='2000-01-01'")
    client=ImmichClient(ImmichSettings('http://local','key'))
    monkeypatch.setattr(client,'server_version',lambda:'3.3.4')
    monkeypatch.setattr(client,'_request',lambda *args:{'id':'remote-one','people':[]})
    result=sync.run_batch(client)
    assert result['failed']==1
    assert len(service.search_assets(person='小明'))==1
    assert len(service.search_assets(tag='旅行/海边'))==1


def test_same_name_people_keep_distinct_identities(service):
    a=asset(service)
    data=detail()
    data['people'].append({'id':'person-two','name':'小明'})
    RecognitionSync(service.database).apply_asset(a,data,'3.3.4')
    assert len(service.list_people())==2
    assert len(service.search_assets(person='小明'))==1
    assert len(service.database.fetch_all('SELECT * FROM external_people'))==2


def test_upgrade_preserves_v3_reviews_and_is_idempotent(tmp_path):
    import sqlite3
    db=Database(tmp_path/'v3.db')
    from aura.db import SCHEMA
    with sqlite3.connect(db.path) as c:
        c.executescript(SCHEMA)
        c.execute('INSERT INTO schema_version VALUES (3)')
        c.execute("ALTER TABLE asset_tags ADD COLUMN review_state TEXT NOT NULL DEFAULT 'pending'")
        c.execute("INSERT INTO assets(id,path,sha256,size_bytes,mtime_ns,indexed_at) VALUES (1,'p','h',1,1,'now')")
        c.execute("INSERT INTO tags(id,name,created_at) VALUES (1,'t','now')")
        c.execute("INSERT INTO asset_tags(asset_id,tag_id,source,review_state) VALUES (1,1,'model','rejected')")
        c.execute("INSERT INTO people(id,name,confirmed,created_at) VALUES (1,'existing person',1,'now')")
        c.execute("INSERT INTO asset_people(asset_id,person_id) VALUES (1,1)")
    db.initialize();db.initialize()
    assert db.fetch_all('SELECT review_state,reviewed FROM asset_tags')==[{'review_state':'rejected','reviewed':1}]
    assert db.fetch_all('SELECT version FROM schema_version')==[{'version':4}]
    assert db.fetch_all('SELECT review_state,reviewed FROM asset_people')==[{'review_state':'confirmed','reviewed':1}]
    assert len(AuraService(db).search_assets(person='existing person'))==1


def test_face_bootstrap_runs_in_order_and_survives_restart(service):
    from aura.enrichment import ensure_face_jobs
    a=asset(service)
    RecognitionSync(service.database).apply_asset(a,detail(),'3.3.4')
    class Jobs:
        busy=False
        starts=[]
        def face_queues(self):
            return {name:{'queueStatus':{'isPaused':False,'isActive':False},'jobCounts':{'active':int(self.busy),'waiting':0,'failed':0}} for name in ['faceDetection','facialRecognition']}
        def start_missing_faces(self,name): self.starts.append(name)
        def people_count(self): return 1
    client=Jobs()
    assert ensure_face_jobs(service.database,client)['phase']=='detection'
    client.busy=True
    assert ensure_face_jobs(service.database,client)['phase']=='detection'
    client.busy=False
    restarted=Database(service.database.path);restarted.initialize()
    assert ensure_face_jobs(restarted,client)['phase']=='recognition'
    assert ensure_face_jobs(restarted,client)['phase']=='done'
    assert ensure_face_jobs(restarted,client)['phase']=='done'
    assert client.starts==['faceDetection','facialRecognition']
    assert service.database.fetch_all('SELECT next_attempt_at FROM enrichment_state')[0]['next_attempt_at'] <= datetime.now(timezone.utc).isoformat()


def test_paused_or_failed_face_jobs_are_not_overridden(service):
    from aura.enrichment import ensure_face_jobs
    asset(service)
    class Jobs:
        def face_queues(self):
            return {'faceDetection':{'queueStatus':{'isPaused':True},'jobCounts':{}}}
        def start_missing_faces(self,name): raise AssertionError('paused jobs must not be resumed automatically')
    with pytest.raises(ImmichError,match='paused'):
        ensure_face_jobs(service.database,Jobs())
    assert service.database.fetch_all('SELECT * FROM integration_state')==[]


def test_worker_imports_before_materializing_combined_album(service,monkeypatch):
    from aura.worker import sync_albums
    from aura.config import Settings, ImmichConfig
    a=asset(service)
    # Named rules made before the initial import are resolved by names at sync time.
    service.create_smart_album('小明海边',{'person':'小明','tag':'旅行/海边'})
    class Client(Remote):
        members=[]
        def all_assets(self): return [{'id':'remote-one','originalPath':'/photos/one.jpg'}]
        def face_queues(self): raise ImmichError('job permission denied')
        def create_album(self,name): return {'id':'album-one'}
        def add_assets(self,album_id,ids): self.members.extend(ids)
    client=Client({'remote-one':detail()})
    monkeypatch.setattr('aura.worker.ImmichClient',lambda *args:client)
    result=sync_albums(Settings(__import__('pathlib').Path('/photos'),service.database.path),ImmichConfig('http://fake','key','/photos'),service.database)
    assert result['albums']==1
    assert client.members==['remote-one']
    tasks={row['name']:row for row in service.task_status()['tasks']}
    assert tasks['face_jobs']['state']=='failed'
    assert tasks['recognition']['state']=='complete'


def test_empty_queues_without_people_do_not_report_success(service):
    from aura.enrichment import ensure_face_jobs
    asset(service)
    service.database.execute("INSERT INTO integration_state(name,value) VALUES ('face_bootstrap','recognition')")
    class Jobs:
        count = 0
        def face_queues(self):
            return {name:{'queueStatus':{'isPaused':False,'isActive':False},'jobCounts':{'active':0,'waiting':0,'failed':0}} for name in ['faceDetection','facialRecognition']}
        def people_count(self): return self.count
    client = Jobs()
    result = ensure_face_jobs(service.database, client)
    assert result['phase'] == 'no_people'
    assert result['people'] == 0
    assert 'warning' in result
    # Results appearing later must clear the warning and refresh imported details.
    client.count = 2
    assert ensure_face_jobs(service.database, client)['phase'] == 'done'


def test_previous_false_done_state_is_rechecked(service):
    from aura.enrichment import ensure_face_jobs
    asset(service)
    service.database.execute("INSERT INTO integration_state(name,value) VALUES ('face_bootstrap','done')")
    class Jobs:
        def people_count(self): return 0
        def face_queues(self):
            return {name:{'queueStatus':{'isPaused':False,'isActive':False},'jobCounts':{'active':0,'waiting':0,'failed':0}} for name in ['faceDetection','facialRecognition']}
    assert ensure_face_jobs(service.database, Jobs())['phase'] == 'no_people'


def test_completed_bootstrap_tracks_new_running_face_jobs(service):
    from aura.enrichment import ensure_face_jobs
    asset(service)
    service.database.execute("INSERT INTO integration_state(name,value) VALUES ('face_bootstrap','no_people')")
    class Jobs:
        def people_count(self): return 0
        def face_queues(self):
            return {name:{'queueStatus':{'isPaused':False,'isActive':name=='faceDetection'},'jobCounts':{'active':1 if name=='faceDetection' else 0,'waiting':5 if name=='faceDetection' else 0,'failed':0}} for name in ['faceDetection','facialRecognition']}
    assert ensure_face_jobs(service.database, Jobs())['phase'] == 'detection'

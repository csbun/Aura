import importlib

from fastapi.testclient import TestClient
from aura.service import AuraService
from conftest import A,PERSON,FakeImmich


def app_client(tmp_path,monkeypatch):
    monkeypatch.setenv('AURA_DB_PATH',str(tmp_path/'app.db'))
    from aura import app as module
    importlib.reload(module)
    fake=FakeImmich()
    fake.settings.configured=True
    monkeypatch.setattr(module,'immich',fake)
    monkeypatch.setattr(module,'service',AuraService(module.database,fake))
    return TestClient(module.app),module


def test_extension_routes_and_removed_management(tmp_path,monkeypatch):
    client,module=app_client(tmp_path,monkeypatch)
    assert client.get('/health').json()['version']=='0.4.2'
    assert '高级整理' in client.get('/').text
    assert client.get('/static/app.js').status_code==200
    assert client.get('/api/v1/people').json()[0]['id']==PERSON
    module.immich.people_rows[0]['name']='最新名字'
    assert client.get('/api/v1/people').json()[0]['name']=='最新名字'
    assert client.post('/api/v1/scans').status_code==404
    assert client.post('/api/v1/integrations/immich/sync-assets').status_code==404
    assert client.patch('/api/v1/people/'+PERSON,json={'name':'x'}).status_code==404
    assert client.get('/api/v1/assets/'+A+'/thumbnail').headers['content-type']=='image/jpeg'
    assert client.get('/api/v1/assets/not-uuid/thumbnail').status_code==422
    assert 'key' not in client.get('/api/v1/config').text


def test_preview_edit_and_validation(tmp_path,monkeypatch):
    client,module=app_client(tmp_path,monkeypatch)
    query={'person_ids':[PERSON]}
    assert client.post('/api/v1/smart-albums/preview',json={'query':query}).json()['matched_assets']==1
    album=client.post('/api/v1/smart-albums',json={'name':'one','query':query}).json()['id']
    assert client.patch('/api/v1/smart-albums/'+str(album),json={'name':'one','query':{},'sync_mode':'append'}).status_code==200
    assert client.post(f'/api/v1/smart-albums/{album}/materialize').status_code==200
    assert client.get('/api/v1/smart-albums').json()[0]['remote_id']
    assert client.post('/api/v1/smart-albums',json={'name':'bad','query':{'person_id':2}}).status_code==422
    assert client.post('/api/v1/smart-albums/preview',json={'query':{'start':'2026-01-01','end':'2025-01-01'}}).status_code==422
    assert client.patch('/api/v1/events/'+A,json={'name':'missing'}).status_code==404
    module.immich.fail_search=True
    assert client.post('/api/v1/smart-albums/preview',json={'query':{}}).status_code==502
    assert client.get('/api/v1/events').status_code==200
    assert client.get('/api/v1/smart-albums').status_code==200


def test_default_event_workflow_merge_and_delete(tmp_path,monkeypatch):
    client,module=app_client(tmp_path,monkeypatch)
    module.service.build_events()
    events=client.get('/api/v1/events').json()
    assert all(e['review_state']=='confirmed' for e in events)
    ids=[e['id'] for e in events]
    assert client.post('/api/v1/events/merge',json={'event_ids':[ids[0],ids[0]]}).status_code==422
    assert client.post('/api/v1/events/merge',json={'event_ids':[ids[0]]}).status_code==422
    result=client.post('/api/v1/events/merge',json={'event_ids':ids,'name':'旅行'})
    assert result.status_code==200 and result.json()['asset_count']==2
    event=result.json()['id']
    assert client.patch(f'/api/v1/events/{event}',json={'name':'新名称'}).status_code==200
    assert client.delete(f'/api/v1/events/{event}').status_code==200
    assert client.get('/api/v1/events').json()==[]
    assert client.get(f'/api/v1/events/{event}/assets').status_code==404
    assert client.delete('/api/v1/events/'+A).status_code==404


def test_delete_album_rule_works_offline_and_is_idempotent(tmp_path,monkeypatch):
    client,module=app_client(tmp_path,monkeypatch)
    album=client.post('/api/v1/smart-albums',json={'name':'失效规则','query':{}}).json()['id']
    remote=client.post(f'/api/v1/smart-albums/{album}/materialize').json()['remote_id']
    members=set(module.immich.remote[remote]['assets'])
    module.database.execute('UPDATE smart_albums SET error=? WHERE id=?',['关联的 Immich 相册已删除或无读取权限',album])
    def offline(*args,**kwargs):raise RuntimeError('Immich is offline')
    monkeypatch.setattr(module.immich,'require_supported',offline)
    assert client.get('/api/v1/smart-albums').json()[0]['error']
    response=client.delete(f'/api/v1/smart-albums/{album}')
    assert response.status_code==204 and response.content==b''
    assert client.delete(f'/api/v1/smart-albums/{album}').status_code==204
    assert client.get('/api/v1/smart-albums').json()==[]
    assert module.immich.remote[remote]['assets']==members

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
    assert client.get('/health').json()['version']=='0.4.0'
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
    assert client.patch('/api/v1/events/999',json={'state':'confirmed'}).status_code==404
    module.immich.fail_search=True
    assert client.post('/api/v1/smart-albums/preview',json={'query':{}}).status_code==502
    assert client.get('/api/v1/events').status_code==200
    assert client.get('/api/v1/smart-albums').status_code==200

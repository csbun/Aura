import importlib

from fastapi.testclient import TestClient
from PIL import Image


def test_review_and_ui_work_without_immich(tmp_path,monkeypatch):
    photos=tmp_path/'photos';photos.mkdir()
    Image.new('RGB',(20,10)).save(photos/'photo.jpg')
    monkeypatch.setenv('PHOTO_ROOT',str(photos))
    monkeypatch.setenv('AURA_DB_PATH',str(tmp_path/'aura.db'))
    monkeypatch.setenv('IMMICH_URL','')
    monkeypatch.setenv('IMMICH_API_KEY','')
    from aura import app as module
    importlib.reload(module)
    client=TestClient(module.app)
    from aura.worker import run_once
    run_once(module.settings,module.immich_config)
    assert client.get('/').status_code==200
    assert '事件审核' in client.get('/').text
    assert client.get('/static/app.js').status_code==200
    event=client.get('/api/v1/events').json()[0]
    assert client.patch(f"/api/v1/events/{event['id']}",json={'state':'confirmed','name':'旅行'}).status_code==200
    asset=client.get('/api/v1/assets',params={'event':event['id']}).json()[0]
    assert client.get(f"/api/v1/assets/{asset['id']}/thumbnail").headers['content-type']=='image/jpeg'
    assert client.post('/api/v1/smart-albums',json={'name':'旅行','query':{'event':event['id']}}).status_code==201
    assert len(client.get('/api/v1/smart-albums').json())==1
    assert client.post('/api/v1/smart-albums',json={'name':'typo','query':{'persno':'x'}}).status_code==422
    assert client.patch('/api/v1/events/999',json={'state':'confirmed'}).status_code==404
    assert client.patch(f"/api/v1/events/{event['id']}",json={'state':'oops'}).status_code==422
    assert client.get('/api/v1/tasks').json()['assets']==1
    assert client.get('/api/v1/integrations/immich').json()['configured'] is False
    # An indexed symlink/legacy path outside the root must never be served.
    module.database.execute('UPDATE assets SET path=? WHERE id=?',[str(tmp_path/'private.jpg'),asset['id']])
    assert client.get(f"/api/v1/assets/{asset['id']}/thumbnail").status_code==403


def test_people_review_and_paged_tags(tmp_path,monkeypatch):
    monkeypatch.setenv('PHOTO_ROOT',str(tmp_path))
    monkeypatch.setenv('AURA_DB_PATH',str(tmp_path/'review.db'))
    from aura import app as module
    importlib.reload(module)
    client=TestClient(module.app)
    service=module.service
    a=service.database.execute("INSERT INTO assets(path,sha256,size_bytes,mtime_ns,indexed_at) VALUES (?,'h',1,1,'now')",[str(tmp_path/'one.jpg')]).lastrowid
    service.map_external_asset(a,'immich','remote')
    from aura.enrichment import RecognitionSync
    RecognitionSync(service.database).apply_asset(a,{'id':'remote','people':[{'id':'p','name':'小明'}],'tags':[{'id':'t','name':'旅行'}]},'3.3.4')
    person=client.get('/api/v1/people').json()[0]['id']
    assert client.patch(f'/api/v1/people/{person}',json={'name':'家人'}).status_code==200
    assert client.get('/api/v1/assets',params={'person_id':person}).json()[0]['id']==a
    assert len(client.get('/api/v1/person-assignments',params={'state':'confirmed'}).json())==1
    assert client.patch(f'/api/v1/assets/{a}/people/{person}',json={'state':'rejected'}).status_code==200
    assert client.get('/api/v1/assets',params={'person_id':person}).json()==[]
    other=client.post('/api/v1/people',json={'name':'另一人物'}).json()['id']
    assert client.post(f'/api/v1/assets/{a}/people/{person}/correct',json={'person_id':other}).status_code==200
    assert len(client.get('/api/v1/assets',params={'person_id':other}).json())==1
    assert len(client.get('/api/v1/tag-assignments',params={'state':'confirmed','limit':1}).json())==1
    assert client.get('/api/v1/tag-assignments',params={'offset':1}).json()==[]
    assert client.get('/api/v1/tag-assignments',params={'limit':0}).status_code==422
    assert client.patch('/api/v1/people/99999',json={'name':'name'}).status_code==404


def test_album_preview_and_rule_edit(tmp_path,monkeypatch):
    monkeypatch.setenv('AURA_DB_PATH',str(tmp_path/'albums.db'))
    from aura import app as module
    importlib.reload(module)
    client=TestClient(module.app)
    service=module.service
    a=service.database.execute("INSERT INTO assets(path,sha256,size_bytes,mtime_ns,indexed_at) VALUES ('/photos/a.jpg','h',1,1,'now')").lastrowid
    b=service.database.execute("INSERT INTO assets(path,sha256,size_bytes,mtime_ns,indexed_at) VALUES ('/photos/b.jpg','h',1,1,'now')").lastrowid
    p=service.create_person('person',True)['id'];e=service.create_event('event',confirmed=True)['id']
    service.database.execute("INSERT INTO asset_people(asset_id,person_id,review_state) VALUES (?,?,'confirmed')",[a,p])
    service.database.execute('INSERT INTO event_assets(event_id,asset_id) VALUES (?,?)',[e,b])
    query={'person_id':p,'event':e}
    preview=client.post('/api/v1/smart-albums/preview',json={'query':query})
    assert preview.status_code==200
    assert preview.json()['matched_assets']==0
    assert preview.json()['condition_counts']=={'person_id':1,'event':1}
    album=client.post('/api/v1/smart-albums',json={'name':'album','query':query}).json()['id']
    assert client.get('/api/v1/smart-albums').json()[0]['matched_assets']==0
    assert client.patch(f'/api/v1/smart-albums/{album}',json={'query':{'person_id':p}}).status_code==200
    assert client.get('/api/v1/smart-albums').json()[0]['matched_assets']==1
    assert client.patch('/api/v1/smart-albums/9999',json={'query':{}}).status_code==404
    assert client.post('/api/v1/smart-albums/preview',json={'query':{'persno':p}}).status_code==422

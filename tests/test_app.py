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

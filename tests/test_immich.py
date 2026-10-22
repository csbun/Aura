import io
import json
from email.message import Message
from urllib.error import HTTPError

import pytest

from aura.immich import ImmichClient, ImmichSettings, ImmichError
from conftest import A,B,PERSON,TAG,REMOTE


def client_with_pages(pages):
    client=ImmichClient(ImmichSettings('http://immich','secret'))
    calls=[]
    def request(method,path,payload=None):
        calls.append((method,path,dict(payload) if payload else None))
        return pages.pop(0)
    client._request=request
    return client,calls


def page(items,cursor=None):
    return {'assets':{'items':[{'id':i} for i in items],'nextCursor':cursor,'nextPage':None}}


def test_cursor_pagination_and_filter_contract():
    client,calls=client_with_pages([page([A],'cursor-1'),page([B])])
    result=client.search({'person_ids':[PERSON],'tag_ids':[TAG],'start':'2025-01-01T00:00:00Z','end':'2026-01-01T00:00:00Z','city':'上海'})
    assert [r['id'] for r in result]==[A,B]
    first=calls[0][2]
    assert first['filter']['personIds']=={'all':[PERSON]}
    assert first['filter']['tagIds']=={'all':[TAG]}
    assert first['filter']['takenAt']=={'gte':'2025-01-01T00:00:00Z','lt':'2026-01-01T00:00:00Z'}
    assert first['filter']['city']=={'eq':'上海'}
    assert first['filter']['trashedAt']=={'eq':None}
    assert 'page' not in first and calls[1][2]['cursor']=='cursor-1'


@pytest.mark.parametrize('responses',[
    [{}], [{'assets':{'items':[]}}], [page([A],'same'),page([A],'same')],
    [page([],'nonempty')], [page([A],123)], [{'assets':{'items':[{}],'nextCursor':None}}],
])
def test_partial_or_malformed_search_never_returns_empty(responses):
    client,_=client_with_pages(responses)
    with pytest.raises(ImmichError):client.search({})


def test_semantic_is_explicitly_bounded_and_filtered():
    client,calls=client_with_pages([page([A],'more')])
    assert client.search({'q':'海边','person_ids':[PERSON]})==[{'id':A}]
    assert calls[0][1]=='/search/smart'
    assert calls[0][2]['size']==100 and calls[0][2]['query']=='海边'
    assert calls[0][2]['filter']['personIds']=={'all':[PERSON]}


def test_people_pagination_includes_hidden():
    client,calls=client_with_pages([{'people':[{'id':PERSON,'name':'person'}],'hasNextPage':True},
                                  {'people':[{'id':REMOTE,'name':''}],'hasNextPage':False}])
    assert len(client.people())==2
    assert 'page=2' in calls[1][1] and 'withHidden=true' in calls[0][1]


def test_album_members_are_paginated_not_album_detail():
    client,calls=client_with_pages([page([A],'next'),page([B])])
    assert client.album_assets(REMOTE)=={A,B}
    assert calls[0][2]['filter']=={'albumIds':{'all':[REMOTE]}}


def test_album_changes_check_version_and_bulk_outcomes():
    client,calls=client_with_pages([{'major':3,'minor':3,'patch':4},[{'id':A,'success':False,'error':'duplicate'}]])
    client.change_album_assets(REMOTE,{A})
    assert calls[-1][0]=='PUT'
    client,_=client_with_pages([{'major':3,'minor':3,'patch':4},[{'id':A,'success':False,'error':'not_found'}]])
    with pytest.raises(ImmichError):client.change_album_assets(REMOTE,{A},remove=True)
    client,calls=client_with_pages([{'major':4,'minor':0,'patch':0}])
    with pytest.raises(ImmichError):client.create_album('name','marker')
    assert len(calls)==1


def test_http_transport_keeps_key_out_of_errors(monkeypatch):
    client=ImmichClient(ImmichSettings('http://immich','private-key'))
    def deny(request,timeout):
        assert request.get_header('X-api-key')=='private-key'
        assert 'private-key' not in request.full_url
        raise HTTPError(request.full_url,403,'private-key',{},None)
    monkeypatch.setattr('aura.immich.urlopen',deny)
    with pytest.raises(ImmichError) as error:client.tags()
    assert '403' in str(error.value) and 'private-key' not in str(error.value)


def test_thumbnail_binary_type_validation(monkeypatch):
    client=ImmichClient(ImmichSettings('http://immich','key'))
    class Response(io.BytesIO):
        headers=Message()
    response=Response(b'<html>error</html>');response.headers['content-type']='text/html'
    monkeypatch.setattr('aura.immich.urlopen',lambda *a,**kw:response)
    with pytest.raises(ImmichError):client.thumbnail(A)


def test_compatibility_cleanup_cannot_delete_other_album():
    client,calls=client_with_pages([{'major':3,'minor':3,'patch':4},{'id':REMOTE,'description':'user album'}])
    with pytest.raises(ImmichError):client.delete_check_album(REMOTE,'Aura compatibility check 123')
    assert not any(call[0]=='DELETE' for call in calls)
    client,calls=client_with_pages([{'major':3,'minor':3,'patch':4}])
    with pytest.raises(ValueError):client.delete_check_album(REMOTE,'ordinary album')
    assert not any(call[0]=='DELETE' for call in calls)


def test_legacy_name_migration_never_overwrites_existing_name():
    client,calls=client_with_pages([{'major':3,'minor':3,'patch':4},{'id':PERSON,'name':'Immich name'}])
    assert client.migrate_person_name(PERSON,'old name')=='conflict'
    assert not any(c[0]=='PUT' for c in calls)
    client,calls=client_with_pages([{'major':3,'minor':3,'patch':4},{'id':PERSON,'name':''},{'id':PERSON,'name':'old name'}])
    assert client.migrate_person_name(PERSON,'old name')=='done'
    assert calls[-1][2]=={'name':'old name'}
    client,calls=client_with_pages([{'major':3,'minor':3,'patch':4},{'id':PERSON,'name':'old name'}])
    assert client.migrate_person_name(PERSON,'old name')=='done'
    assert not any(c[0]=='PUT' for c in calls)


def test_tag_membership_paginates_without_excluding_offline_assets():
    client, calls = client_with_pages([
        {'assets': {'items': [{'id': A}], 'nextCursor': 'next'}},
        {'assets': {'items': [{'id': B}], 'nextCursor': None}},
    ])
    assert [a['id'] for a in client.tag_assets(TAG)] == [A, B]
    assert calls[0][2]['filter'] == {'tagIds': {'all': [TAG]}}
    assert calls[1][2]['cursor'] == 'next'

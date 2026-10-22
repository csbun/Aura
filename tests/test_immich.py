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


def test_tag_hierarchy_and_rename_contract():
    version={'major':3,'minor':3,'patch':4}
    client,calls=client_with_pages([version,{'id':TAG,'name':'旅行','value':'事件/旅行','parentId':REMOTE}])
    assert client.create_tag('旅行',REMOTE)['id']==TAG
    assert calls[-1]==('POST','/tags',{'name':'旅行','parentId':REMOTE})
    client,calls=client_with_pages([version,{'id':TAG,'name':'新旅行','value':'事件/新旅行','parentId':REMOTE}])
    assert client.rename_tag(TAG,'新旅行')['name']=='新旅行'
    assert calls[-1]==('PUT','/tags/'+TAG,{'name':'新旅行'})


def test_tag_bulk_failure_never_becomes_success():
    version={'major':3,'minor':3,'patch':4}
    client,calls=client_with_pages([version,[{'id':A,'success':True},{'id':B,'success':False,'error':'not_found'}]])
    with pytest.raises(ImmichError,match='不能继续迁移'):
        client.change_tag_assets(TAG,{A,B})
    assert calls[-1][0:2]==('PUT','/tags/'+TAG+'/assets')
    client,calls=client_with_pages([version,[{'id':A,'success':False,'error':'duplicate'}]])
    client.change_tag_assets(TAG,{A})
    client,calls=client_with_pages([version,[{'id':A,'success':True}]])
    client.change_tag_assets(TAG,{A},remove=True)
    assert calls[-1][0]=='DELETE'


def test_tag_delete_refuses_cascading_into_user_children():
    client,calls=client_with_pages([{'major':3,'minor':3,'patch':4},[{'id':REMOTE,'parentId':TAG}]])
    with pytest.raises(ImmichError,match='子标签'):
        client.delete_tag(TAG)
    assert not any(c[0]=='DELETE' for c in calls)


def test_tag_writes_still_require_verified_upstream_version():
    client,calls=client_with_pages([{'major':4,'minor':0,'patch':0}])
    with pytest.raises(ImmichError):client.create_tag('旅行')
    assert len(calls)==1


def test_tag_job_check_refuses_missing_or_paused_queues():
    client,_=client_with_pages([{}])
    with pytest.raises(ImmichError):client.jobs_idle(('sidecar',))
    client,_=client_with_pages([{'sidecar':{'jobCounts':{'active':0,'waiting':1,'delayed':0,'failed':0},'queueStatus':{'isPaused':True}}}])
    with pytest.raises(ImmichError,match='已暂停'):client.jobs_idle(('sidecar',))


def test_tag_writes_wait_for_sidecar_and_metadata_to_stay_idle(monkeypatch):
    client, _ = client_with_pages([])
    observed = []
    states = iter([False, True, False, True, True])
    def idle(names):
        observed.append(names)
        return next(states)
    monkeypatch.setattr(client, 'jobs_idle', idle)
    client.wait_tag_writes(sleep=lambda _: None)
    assert len(observed) == 5
    assert set(observed) == {('sidecar', 'metadataExtraction')}


def test_tag_wait_timeout_and_failed_queue_never_report_success(monkeypatch):
    client, _ = client_with_pages([])
    monkeypatch.setattr(client, 'jobs_idle', lambda names: False)
    ticks = iter(range(20))
    with pytest.raises(ImmichError, match='仍忙碌'):
        client.wait_tag_writes(timeout=2, sleep=lambda _: None, clock=lambda: next(ticks))
    client, _ = client_with_pages([{'sidecar': {'jobCounts': {'active': 0, 'waiting': 0, 'delayed': 0, 'failed': 1}}}])
    with pytest.raises(ImmichError, match='失败任务'):
        client.jobs_idle(('sidecar',))


def test_job_key_is_only_used_for_queue_reads(monkeypatch):
    client = ImmichClient(ImmichSettings('http://immich', 'photo-key', 'queue-key'))
    calls = []
    def request(self, method, path, *args, **kwargs):
        calls.append((self.settings.api_key, method, path))
        if path == '/jobs':
            return {'sidecar': {'jobCounts': {'active': 0, 'waiting': 0, 'delayed': 0, 'failed': 0}}}
        return []
    monkeypatch.setattr(ImmichClient, '_request', request)
    assert client.jobs_idle(('sidecar',))
    client.tags()
    assert calls == [('queue-key', 'GET', '/jobs'), ('photo-key', 'GET', '/tags')]


def test_tag_membership_paginates_without_excluding_offline_assets():
    client, calls = client_with_pages([
        {'assets': {'items': [{'id': A}], 'nextCursor': 'next'}},
        {'assets': {'items': [{'id': B}], 'nextCursor': None}},
    ])
    assert [a['id'] for a in client.tag_assets(TAG)] == [A, B]
    assert calls[0][2]['filter'] == {'tagIds': {'all': [TAG]}}
    assert calls[1][2]['cursor'] == 'next'

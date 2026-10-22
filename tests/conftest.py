import copy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from aura.db import Database
from aura.immich import ImmichError
from aura.service import AuraService

A, B, C, PERSON, TAG, REMOTE = [str(UUID(int=i, version=4)) for i in range(1,7)]


class FakeImmich:
    def wait_tag_writes(self):
        pass

    settings = SimpleNamespace(base_url='http://immich:2283')

    def __init__(self):
        self.version = '3.3.4'
        self.user = 'owner'
        self.people_rows = [{'id':PERSON,'name':'小明'}]
        self.tag_rows = [{'id':TAG,'name':'海边','value':'海边'}]
        self.assets = [
            {'id':A,'fileCreatedAt':'2025-05-01T00:00:00Z','localDateTime':'2025-05-01T08:00:00','person_ids':[PERSON],'tag_ids':[TAG]},
            {'id':B,'fileCreatedAt':'2025-05-02T00:00:00Z','localDateTime':'2025-05-02T08:00:00','person_ids':[],'tag_ids':[]},
        ]
        self.remote = {}
        self.calls = []
        self.fail_search = False
        self.fail_add = False

    def server_version(self):
        return self.version

    def require_supported(self):
        if self.version != '3.3.4':
            raise ImmichError('unsupported version')
        return self.version

    def identity(self):
        return self.user

    def people(self):
        return copy.deepcopy(self.people_rows)

    def tags(self):
        return copy.deepcopy(self.tag_rows)

    def search(self, query):
        self.calls.append(('search',query))
        if self.fail_search:
            raise ImmichError('incomplete search')
        rows = copy.deepcopy(self.assets)
        for key in ('person_ids','tag_ids'):
            if query.get(key):
                rows = [r for r in rows if set(query[key]) <= set(r.get(key, []))]
        return rows

    def albums(self):
        return [{'id':key,'description':value['description']} for key,value in self.remote.items()]

    def create_album(self, name, marker):
        key = str(UUID(int=100+len(self.remote),version=4))
        self.remote[key] = {'description':marker,'assets':set()}
        self.calls.append(('create',key))
        return key

    def album_assets(self,key):
        if key not in self.remote:
            raise ImmichError('album missing')
        return set(self.remote[key]['assets'])

    def change_album_assets(self,key,ids,remove=False):
        self.calls.append(('remove' if remove else 'add',set(ids)))
        if not remove and self.fail_add:
            raise ImmichError('add failed')
        if remove:
            self.remote[key]['assets'] -= ids
        else:
            self.remote[key]['assets'] |= ids

    def tag(self, tag_id):
        for tag in self.tag_rows:
            if tag['id'] == tag_id: return copy.deepcopy(tag)
        raise ImmichError('tag missing')

    def create_tag(self, name, parent_id=None):
        if any(t['name'] == name and t.get('parentId') == parent_id for t in self.tag_rows):
            raise ImmichError('duplicate tag name')
        tag = {'id':str(uuid4()), 'name':name, 'value':name}
        if parent_id:
            parent = self.tag(parent_id); tag.update(parentId=parent_id,value=parent['value']+'/'+name)
        self.tag_rows.append(tag)
        return copy.deepcopy(tag)

    def rename_tag(self, tag_id, name):
        for tag in self.tag_rows:
            if tag['id'] == tag_id:
                tag['name'] = name
                tag['value'] = (self.tag(tag['parentId'])['value']+'/' if tag.get('parentId') else '')+name
                return copy.deepcopy(tag)
        raise ImmichError('tag missing')

    def tag_assets(self, tag_id):
        if self.fail_search: raise ImmichError('incomplete tag search')
        self.tag(tag_id)
        return [copy.deepcopy(a) for a in self.assets if tag_id in a.get('tag_ids', [])]

    def change_tag_assets(self, tag_id, ids, remove=False):
        self.tag(tag_id)
        for asset in self.assets:
            if asset['id'] in ids:
                current=set(asset.get('tag_ids',[]))
                current.discard(tag_id) if remove else current.add(tag_id)
                asset['tag_ids']=sorted(current)

    def delete_tag(self, tag_id):
        if any(t.get('parentId')==tag_id for t in self.tag_rows): raise ImmichError('child tags')
        self.tag_rows=[t for t in self.tag_rows if t['id']!=tag_id]
        for asset in self.assets:
            asset['tag_ids']=[i for i in asset.get('tag_ids',[]) if i!=tag_id]

    def thumbnail(self, asset_id):
        return b'fake-thumbnail','image/jpeg'


@pytest.fixture
def service(tmp_path):
    return AuraService(Database(tmp_path/'aura.db'),FakeImmich())

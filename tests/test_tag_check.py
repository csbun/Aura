from copy import deepcopy

import pytest

from aura.immich import ImmichError
from aura.tag_check import check_tag_persistence
from conftest import A, TAG


class TagProbe:
    def __init__(self, survives=True):
        self.survives = survives
        self.catalog = []
        self.asset = {'id': A, 'updatedAt': 'before', 'checksum': 'unchanged', 'tags': []}
        self.deleted = []
        self.calls = []

    def require_supported(self):
        return '3.3.4'

    def asset_detail(self, asset_id):
        return deepcopy(self.asset)

    def jobs_idle(self, names):
        return True

    def create_tag(self, name):
        self.catalog.append({'id': TAG, 'name': name, 'value': name})
        return self.catalog[0]

    def change_tag_assets(self, tag_id, ids):
        self.asset['tags'] = deepcopy(self.catalog)

    def refresh_asset_metadata(self, asset_id):
        self.calls.append('refresh')
        self.asset['updatedAt'] = 'after'
        if not self.survives:
            self.asset['tags'] = []

    def tags(self):
        return deepcopy(self.catalog)

    def delete_tag(self, tag_id):
        self.deleted.append(tag_id)
        self.catalog = [t for t in self.catalog if t['id'] != tag_id]
        self.asset['tags'] = [t for t in self.asset['tags'] if t['id'] != tag_id]


def test_readonly_tag_loss_fails_durability_gate_and_cleans_up():
    client = TagProbe(survives=False)
    result = check_tag_persistence(client, A)
    assert result['metadata_refreshed'] and not result['migration_ready']
    assert not result['tag_persisted'] and client.deleted == [TAG]
    assert client.catalog == []


def test_persistent_tag_passes_and_temporary_tag_is_removed():
    client = TagProbe()
    result = check_tag_persistence(client, A)
    assert result['migration_ready'] and client.deleted == [TAG]
    assert not client.asset['tags']


def test_probe_refuses_photo_with_existing_tags():
    client = TagProbe()
    client.asset['tags'] = [{'id': TAG}]
    with pytest.raises(ImmichError, match='没有标签'):
        check_tag_persistence(client, A)
    assert not client.catalog and not client.calls


def test_probe_cleans_up_after_metadata_request_failure():
    client = TagProbe()
    def fail(asset_id):
        raise ImmichError('permission denied')
    client.refresh_asset_metadata = fail
    with pytest.raises(ImmichError, match='permission denied'):
        check_tag_persistence(client, A)
    assert client.deleted == [TAG]


def test_probe_timeout_is_not_success():
    client = TagProbe()
    client.refresh_asset_metadata = lambda asset_id: None
    ticks = iter(range(100))
    with pytest.raises(ImmichError, match='超时'):
        check_tag_persistence(client, A, timeout=2, clock=lambda: next(ticks), sleep=lambda _: None)
    assert client.deleted == [TAG]


def test_probe_does_not_delete_a_tag_renamed_by_user():
    client = TagProbe()
    def refresh(asset_id):
        client.asset['updatedAt'] = 'after'
        client.catalog[0]['name'] = 'user rename'
    client.refresh_asset_metadata = refresh
    with pytest.raises(ImmichError, match='被改名'):
        check_tag_persistence(client, A)
    assert client.deleted == []

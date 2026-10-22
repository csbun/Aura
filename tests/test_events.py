import pytest

from conftest import A, B, C


def test_automatic_events_are_usable_and_grow_without_approval(service):
    service.build_events()
    events = service.list_events()
    assert all(e['review_state'] == 'confirmed' and e['source'] == 'automatic' for e in events)
    first = next(e for e in events if e['name'] == '2025-05-01')
    album = service.save_album('自动事件', {'event': first['id']}, 'mirror')['id']
    service.immich.assets.append({'id': C, 'localDateTime': '2025-05-01T09:00:00'})
    service.build_events()
    first_again = next(e for e in service.list_events() if e['id'] == first['id'])
    assert first_again['asset_count'] == 2
    assert service.materialize(album)['matched_assets'] == 2
    service.build_events()
    assert len(service.list_events()) == 2


def test_merge_unions_members_retargets_rules_and_survives_rebuild(service):
    service.build_events()
    ids = sorted(e['id'] for e in service.list_events())
    albums = [service.save_album(str(eid), {'event': eid}, 'mirror')['id'] for eid in ids]
    remotes = [service.materialize(a)['remote_id'] for a in albums]
    # Overlapping membership must not duplicate photos in the merged event.
    service.database.execute('INSERT INTO event_assets VALUES (?,?)', [ids[1], A])
    result = service.merge_events(ids, '一次旅行')
    assert result == {'id': ids[0], 'asset_count': 2, 'updated_albums': 2}
    assert len(service.list_events()) == 1
    merged = service.list_events()[0]
    assert merged['name'] == '一次旅行'
    assert merged['starts_at'].startswith('2025-05-01') and merged['ends_at'].startswith('2025-05-02')
    for album, remote in zip(albums, remotes):
        row = next(a for a in service.list_smart_albums() if a['id'] == album)
        assert row['query']['event'] == ids[0] and row['remote_id'] == remote
        service.materialize(album)
        assert service.immich.remote[remote]['assets'] == {A, B}
    service.build_events()
    assert service.list_events()[0]['id'] == ids[0]
    assert len(service.list_events()) == 1
    with pytest.raises(LookupError): service.search_assets({'event': ids[1]})


def test_delete_suppresses_regeneration_and_preserves_remote_album(service):
    service.build_events()
    event = service.list_events()[0]['id']
    album = service.save_album('保留照片', {'event': event}, 'mirror')['id']
    remote = service.materialize(album)['remote_id']
    original_members = set(service.immich.remote[remote]['assets'])
    service.delete_event(event)
    assert service.delete_event(event)['review_state'] == 'deleted'  # safe retry
    service.build_events()
    assert len(service.list_events()) == 1
    assert all(e['id'] != event for e in service.list_events())
    assert service.sync_albums() == {'albums': 0, 'paused': 1, 'errors': []}
    assert service.immich.remote[remote]['assets'] == original_members
    assert '已删除' in service.list_smart_albums()[0]['error']
    with pytest.raises(LookupError): service.search_assets({'event': event})
    with pytest.raises(LookupError): service.save_album('bad', {'event': event}, 'mirror')
    with pytest.raises(LookupError): service.review_event(event, 'confirmed')
    replacement = service.list_events()[0]['id']
    service.save_album('改选事件', {'event': replacement}, 'mirror', album)
    assert service.sync_albums()['albums'] == 1


def test_merge_validation_is_atomic(service):
    service.build_events()
    ids = [e['id'] for e in service.list_events()]
    before = service.list_events()
    with pytest.raises(LookupError): service.merge_events([ids[0], 9999], 'bad')
    with pytest.raises(ValueError): service.merge_events([ids[0], ids[0]])
    with pytest.raises(ValueError): service.merge_events(ids, '  ')
    assert service.list_events() == before
    service.delete_event(ids[0])
    with pytest.raises(LookupError): service.merge_events(ids)


def test_deleted_merged_event_never_returns(service):
    service.build_events()
    result = service.merge_events([e['id'] for e in service.list_events()])
    service.delete_event(result['id'])
    service.build_events()
    assert service.list_events() == []


def test_schema5_promotes_pending_but_keeps_human_decisions(service):
    service.build_events()
    ids = sorted(e['id'] for e in service.list_events())
    db = service.database
    db.execute('UPDATE schema_version SET version=5')
    db.execute("UPDATE events SET source='candidate',review_state='pending' WHERE id=?", [ids[0]])
    db.execute("UPDATE events SET source='candidate',review_state='confirmed' WHERE id=?", [ids[1]])
    db.initialize()
    rows = {e['id']: e for e in service.list_events()}
    assert rows[ids[0]]['review_state'] == 'confirmed' and rows[ids[0]]['source'] == 'automatic'
    assert rows[ids[1]]['source'] == 'manual'
    assert db.fetch_all('SELECT version FROM schema_version')[0]['version'] == 6
    db.initialize()
    assert len(service.list_events()) == 2


def test_automatic_join_retargets_old_album_rules(service):
    service.immich.assets[1]['localDateTime'] = '2025-05-01T18:00:00'
    service.build_events()
    ids = sorted(e['id'] for e in service.list_events())
    album = service.save_album('晚间', {'event': ids[1]}, 'mirror')['id']
    service.immich.assets.append({'id': C, 'localDateTime': '2025-05-01T13:00:00'})
    service.build_events()
    assert len(service.list_events()) == 1
    rule = next(a for a in service.list_smart_albums() if a['id'] == album)
    assert rule['query']['event'] == service.list_events()[0]['id']
    assert service.materialize(album)['matched_assets'] == 3


def test_missing_remote_album_reports_cause_without_recreating(service):
    service.build_events()
    event = service.list_events()[0]['id']
    album = service.save_album('事件相册', {'event': event}, 'mirror')['id']
    remote = service.materialize(album)['remote_id']
    del service.immich.remote[remote]
    from aura.immich import ImmichError
    with pytest.raises(ImmichError, match='已删除或无读取权限'):
        service.materialize(album)
    assert not service.immich.remote
    assert service.list_smart_albums()[0]['remote_id'] == remote

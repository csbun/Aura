"""One-time translation of Aura <=0.3 identities. Never used for live queries."""
import json


def read_legacy(c, archive):
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def rows(table):
        return [dict(r) for r in c.execute(f'SELECT * FROM {table}')] if table in tables else []

    assets = {r['asset_id']: r['external_id'] for r in rows('external_assets') if r['provider'] == 'immich'}
    people = {r['person_id']: r['external_id'] for r in rows('external_people')}
    tags = {r['tag_id']: r['external_id'] for r in rows('external_tags')}
    remote_albums = {r['smart_album_id']: r['external_id'] for r in rows('external_albums') if r['provider'] == 'immich'}
    names = [dict(r, immich_id=people.get(r['id'])) for r in rows('people') if r.get('name_locked', 1)]
    reviews = {}
    for table in ('asset_people', 'asset_tags'):
        reviews[table] = [dict(r, immich_asset_id=assets.get(r['asset_id'])) for r in rows(table)
                          if r.get('reviewed', r.get('source') == 'manual')]
    memberships, missing = [], set()
    for row in rows('event_assets'):
        if row['asset_id'] in assets:
            memberships.append((row['event_id'], assets[row['asset_id']]))
        else:
            missing.add(row['event_id'])
    events = [(r['id'], r['name'], r['starts_at'], r['ends_at'],
               r.get('review_state', 'confirmed' if r.get('confirmed') else 'pending'), r['source'],
               '旧事件有照片缺少 Immich ID，请重新生成事件' if r['id'] in missing else None, r['created_at'])
              for r in rows('events')]
    albums = []
    for row in rows('smart_albums'):
        old = json.loads(row['query_json'])
        query = {k: v for k, v in old.items() if k in ('start', 'end', 'event') and v is not None}
        issues = []
        for singular, plural, mapping, table in [('person', 'person_ids', people, 'people'), ('tag', 'tag_ids', tags, 'tags')]:
            local_id = old.get(singular + '_id')
            if local_id is None and old.get(singular):
                matches = [r['id'] for r in rows(table) if r['name'].lower() == old[singular].lower()]
                local_id = matches[0] if len(matches) == 1 else None
            if local_id is not None or old.get(singular):
                if local_id in mapping:
                    query[plural] = [mapping[local_id]]
                else:
                    issues.append(f'旧{singular}条件无法对应 Immich，请重新选择')
                if singular == 'person' and (any(r['id'] == local_id for r in names) or reviews['asset_people']):
                    issues.append('旧人物命名/审核已留档，请在 Immich 核对人物后保存规则')
                if singular == 'tag' and reviews['asset_tags']:
                    issues.append('旧标签审核已留档，请在 Immich 核对后保存规则')
        if old.get('q'):
            issues.append('旧路径关键词不等于语义搜索，请重新选择条件')
        if query.get('event') in missing:
            issues.append('事件照片 ID 不完整')
        unknown = set(old) - {'person', 'person_id', 'tag', 'tag_id', 'start', 'end', 'event', 'q'}
        if unknown:
            issues.append('旧规则含无法迁移的条件')
        albums.append((row['id'], row['name'], json.dumps(query, ensure_ascii=False), remote_albums.get(row['id']),
                       'append', '；'.join(issues) or None, row['created_at']))
    report = {'archive': archive, 'events': len(events), 'albums': len(albums),
              'paused_albums': sum(bool(r[5]) for r in albums), 'unmapped_events': sorted(missing),
              'local_names': names, 'local_reviews': reviews,
              'legacy_rules': [{'id': r['id'], 'name': r['name'], 'query': json.loads(r['query_json'])} for r in rows('smart_albums')],
              'note': '历史资料已归档，不再参与查询。人物命名和审核请在 Immich 核对；受影响规则保存后恢复。'}
    return events, memberships, albums, report


NAME_REVIEW_ISSUE = '旧人物命名/审核已留档，请在 Immich 核对人物后保存规则'


def finish_names(database, immich):
    """One-time, idempotent upgrade: fill only still-unnamed upstream people.

    Existing upstream names win. Local face corrections are never guessed from asset IDs.
    No ongoing name synchronization remains after this migration completes.
    """
    report = json.loads(database.get_setting('migration_report', 'null'))
    if not report or report.get('names_finished'):
        return {'pending': 0}
    failures = []
    for person in report['local_names']:
        if person.get('migration_state') in ('done', 'conflict', 'unmapped'):
            continue
        external_id = person.get('immich_id')
        if not external_id:
            person['migration_state'] = 'unmapped'
        else:
            try:
                # The adapter performs the read-before-write check against Immich.
                person['migration_state'] = immich.migrate_person_name(external_id, person['name'])
            except Exception as error:
                failures.append(str(error))
                person['migration_state'] = 'retry'
        database.set_setting('migration_report', json.dumps(report, ensure_ascii=False))
    done = {p['immich_id'] for p in report['local_names'] if p.get('migration_state') == 'done'}
    affected = {p.get('immich_id') for p in report['local_names']}
    if not report['local_reviews']['asset_people']:
        with database.lock('albums'):
            for album in database.fetch_all('SELECT id,query_json,migration_issue FROM smart_albums WHERE migration_issue IS NOT NULL'):
                ids = set(json.loads(album['query_json']).get('person_ids', [])) & affected
                if ids and ids <= done:
                    issues = [part for part in album['migration_issue'].split('；') if part != NAME_REVIEW_ISSUE]
                    database.execute('UPDATE smart_albums SET migration_issue=? WHERE id=?', ['；'.join(issues) or None, album['id']])
    report['paused_albums'] = database.fetch_all('SELECT COUNT(*) AS count FROM smart_albums WHERE migration_issue IS NOT NULL')[0]['count']
    report['names_finished'] = all(p.get('migration_state') in ('done', 'conflict', 'unmapped') for p in report['local_names'])
    report['note'] = '历史资料已归档。仍未命名的 Immich 人物已尝试补入旧名称；已有名称不覆盖。冲突、审核或缺失映射的规则请核对后保存。'
    database.set_setting('migration_report', json.dumps(report, ensure_ascii=False))
    return {'migrated_names': len(done), 'pending': sum(p.get('migration_state') == 'retry' for p in report['local_names']), 'errors': failures}

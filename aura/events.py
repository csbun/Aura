from datetime import datetime, timedelta, timezone


def build_candidates(database, assets):
    """Persist only Aura events and Immich IDs; photo metadata remains transient."""
    with database.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        protected = {r[0] for r in c.execute("SELECT ea.asset_id FROM event_assets ea JOIN events e ON e.id=ea.event_id WHERE e.review_state IN ('confirmed','rejected')")}
        dated = []
        for asset in assets:
            if asset['id'] in protected:
                continue
            raw = asset.get('localDateTime') or asset.get('fileCreatedAt')
            if not raw:
                continue
            try:
                date = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                # Immich localDateTime is a wall-clock representation; preserve its date for grouping.
                date = date.replace(tzinfo=None)
            except (ValueError, TypeError):
                continue
            dated.append((date, asset['id']))
        dated.sort()
        groups = []
        for date, asset_id in dated:
            if not groups or date.date() != groups[-1][-1][0].date() or date - groups[-1][-1][0] > timedelta(hours=6):
                groups.append([])
            groups[-1].append((date, asset_id))
        old = {}
        for r in c.execute("SELECT e.id, ea.asset_id FROM events e LEFT JOIN event_assets ea ON e.id=ea.event_id WHERE e.source='candidate' AND e.review_state='pending'"):
            old.setdefault(r['id'], set()).add(r['asset_id'])
        used, created = set(), 0
        for group in groups:
            ids = {r[1] for r in group}
            overlap, _, event_id = max(((len(ids & members), -eid, eid) for eid, members in old.items() if eid not in used), default=(0, 0, None))
            dates = [group[0][0].isoformat(), group[-1][0].isoformat()]
            if not overlap:
                event_id = c.execute('INSERT INTO events(name,starts_at,ends_at) VALUES (?,?,?)', [str(group[0][0].date()), *dates]).lastrowid
                created += 1
            else:
                c.execute('UPDATE events SET starts_at=?,ends_at=?,issue=NULL WHERE id=?', [*dates, event_id])
                c.execute('DELETE FROM event_assets WHERE event_id=?', [event_id])
            used.add(event_id)
            c.executemany('INSERT INTO event_assets VALUES (?,?)', [(event_id, aid) for aid in sorted(ids)])
        for event_id in old.keys() - used:
            c.execute("UPDATE events SET review_state='superseded' WHERE id=?", [event_id])
        c.execute("INSERT INTO settings VALUES ('events_built_at',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", [datetime.now(timezone.utc).isoformat()])
    return {'created': created, 'candidates': len(groups)}

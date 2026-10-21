from __future__ import annotations

from datetime import datetime, timedelta

from .db import Database


def build_day_candidates(database: Database, gap_hours: int = 6) -> int:
    """Reuse candidate identities and leave reviewed memberships untouched."""
    database.initialize()
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT id, captured_at FROM assets WHERE missing_at IS NULL AND captured_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM event_assets ea JOIN events e ON e.id = ea.event_id "
            "WHERE ea.asset_id = assets.id AND e.review_state IN ('confirmed', 'rejected')) "
            "ORDER BY captured_at, id"
        ).fetchall()
        groups = []
        for row in rows:
            captured = datetime.fromisoformat(row["captured_at"])
            previous = datetime.fromisoformat(groups[-1][-1]["captured_at"]) if groups else None
            if previous is None or captured.date() != previous.date() or captured - previous > timedelta(hours=gap_hours):
                groups.append([])
            groups[-1].append(row)
        old = {}
        for row in connection.execute(
            "SELECT e.id, ea.asset_id FROM events e LEFT JOIN event_assets ea ON ea.event_id = e.id "
            "WHERE e.source = 'candidate' AND e.review_state = 'pending' ORDER BY e.id"
        ):
            old.setdefault(row["id"], set()).add(row["asset_id"])
        used = set()
        created = 0
        for group in groups:
            ids = {row["id"] for row in group}
            available = [(len(ids & members), -event_id, event_id) for event_id, members in old.items() if event_id not in used]
            best = max(available, default=(0, 0, None))
            event_id = best[2] if best[0] else None
            if event_id is None:
                cursor = connection.execute(
                    "INSERT INTO events(name, starts_at, ends_at, source, created_at) VALUES (?, ?, ?, 'candidate', datetime('now'))",
                    [datetime.fromisoformat(group[0]["captured_at"]).date().isoformat(), group[0]["captured_at"], group[-1]["captured_at"]],
                )
                event_id = cursor.lastrowid
                created += 1
            else:
                connection.execute("UPDATE events SET starts_at = ?, ends_at = ? WHERE id = ?", [group[0]["captured_at"], group[-1]["captured_at"], event_id])
                connection.execute("DELETE FROM event_assets WHERE event_id = ?", [event_id])
            used.add(event_id)
            connection.executemany("INSERT INTO event_assets(event_id, asset_id, confidence) VALUES (?, ?, 1)", [(event_id, row["id"]) for row in group])
        for event_id in old.keys() - used:
            # Keep IDs reserved: saved album rules must never point to a different event.
            connection.execute("UPDATE events SET review_state = 'superseded' WHERE id = ?", [event_id])
        return created

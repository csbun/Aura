from __future__ import annotations

from datetime import datetime, timedelta

from .db import Database


def build_day_candidates(database: Database, gap_hours: int = 6) -> int:
    """Create replaceable day-level event candidates from capture times.

    This is intentionally deterministic and conservative. A later adapter can
    add GPS, people, and visual embeddings without changing the event tables.
    Only unconfirmed candidates are replaced, so user decisions are preserved.
    """
    rows = database.fetch_all(
        "SELECT id, captured_at FROM assets WHERE missing_at IS NULL AND captured_at IS NOT NULL ORDER BY captured_at, id"
    )
    groups: list[list[dict]] = []
    for row in rows:
        captured = datetime.fromisoformat(row["captured_at"])
        if not groups:
            groups.append([row])
            continue
        previous = datetime.fromisoformat(groups[-1][-1]["captured_at"])
        if captured.date() != previous.date() or captured - previous > timedelta(hours=gap_hours):
            groups.append([row])
        else:
            groups[-1].append(row)

    with database.connect() as connection:
        candidate_ids = [row["id"] for row in connection.execute("SELECT id FROM events WHERE confirmed = 0 AND source = 'candidate'").fetchall()]
        if candidate_ids:
            marks = ",".join("?" for _ in candidate_ids)
            connection.execute(f"DELETE FROM event_assets WHERE event_id IN ({marks})", candidate_ids)
            connection.execute(f"DELETE FROM events WHERE id IN ({marks})", candidate_ids)
        created = 0
        for group in groups:
            start = datetime.fromisoformat(group[0]["captured_at"])
            end = datetime.fromisoformat(group[-1]["captured_at"])
            name = start.date().isoformat() if start.date() == end.date() else f"{start.date()} – {end.date()}"
            cursor = connection.execute(
                "INSERT INTO events(name, starts_at, ends_at, level, confirmed, source, created_at) VALUES (?, ?, ?, 'day', 0, 'candidate', datetime('now'))",
                [name, group[0]["captured_at"], group[-1]["captured_at"]],
            )
            event_id = cursor.lastrowid
            connection.executemany("INSERT INTO event_assets(event_id, asset_id, confidence) VALUES (?, ?, ?)", [(event_id, row["id"], 1.0) for row in group])
            created += 1
        connection.commit()
    return created

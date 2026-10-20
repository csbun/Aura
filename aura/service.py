from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .db import Database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuraService:
    """Stable application interface for future Web and Immich adapters."""

    def __init__(self, database: Database):
        self.database = database
        self.database.initialize()

    def search_assets(self, query: str | None = None, start: str | None = None, end: str | None = None,
                      person: str | None = None, tag: str | None = None, event: int | None = None) -> list[dict[str, Any]]:
        clauses = ["missing_at IS NULL"]
        params: list[Any] = []
        if query:
            clauses.append("lower(path) LIKE ?")
            params.append(f"%{query.lower()}%")
        if start:
            clauses.append("captured_at >= ?")
            params.append(start)
        if end:
            clauses.append("captured_at < ?")
            params.append(end)
        joins = ""
        if person:
            joins += " JOIN asset_people ap ON ap.asset_id = assets.id JOIN people p ON p.id = ap.person_id"
            clauses.append("lower(p.name) = lower(?)")
            params.append(person)
        if tag:
            joins += " JOIN asset_tags at ON at.asset_id = assets.id JOIN tags t ON t.id = at.tag_id"
            clauses.append("lower(t.name) = lower(?)")
            params.append(tag)
        if event is not None:
            joins += " JOIN event_assets ea ON ea.asset_id = assets.id"
            clauses.append("ea.event_id = ?")
            params.append(event)
        return self.database.fetch_all(f"SELECT DISTINCT assets.* FROM assets{joins} WHERE {' AND '.join(clauses)} ORDER BY captured_at, path", params)

    def list_people(self) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM people ORDER BY name")

    def list_tags(self) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM tags ORDER BY name")

    def list_events(self) -> list[dict[str, Any]]:
        return self.database.fetch_all("SELECT * FROM events ORDER BY starts_at")

    def create_smart_album(self, name: str, query: dict[str, Any]) -> dict[str, Any]:
        cursor = self.database.execute(
            "INSERT INTO smart_albums(name, query_json, created_at) VALUES (?, ?, ?)",
            [name, json.dumps(query, ensure_ascii=False), _now()],
        )
        return self.database.fetch_all("SELECT * FROM smart_albums WHERE id = ?", [cursor.lastrowid])[0]

    def create_person(self, name: str, confirmed: bool = False) -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO people(name, confirmed, created_at) VALUES (?, ?, ?)", [name, int(confirmed), _now()])
        return self.database.fetch_all("SELECT * FROM people WHERE id = ?", [cursor.lastrowid])[0]

    def create_tag(self, name: str, kind: str = "user") -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO tags(name, kind, created_at) VALUES (?, ?, ?)", [name, kind, _now()])
        return self.database.fetch_all("SELECT * FROM tags WHERE id = ?", [cursor.lastrowid])[0]

    def create_event(self, name: str, level: str = "day", confirmed: bool = False) -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO events(name, level, confirmed, created_at) VALUES (?, ?, ?, ?)", [name, level, int(confirmed), _now()])
        return self.database.fetch_all("SELECT * FROM events WHERE id = ?", [cursor.lastrowid])[0]

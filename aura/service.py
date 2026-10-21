from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from .db import Database
from .immich import ImmichClient, ImmichSettings


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

    def materialize_smart_album(self, smart_album_id: int, immich: ImmichClient) -> dict[str, Any]:
        albums = self.database.fetch_all("SELECT * FROM smart_albums WHERE id = ?", [smart_album_id])
        if not albums:
            raise ValueError(f"smart album not found: {smart_album_id}")
        album = albums[0]
        query = json.loads(album["query_json"])
        assets = self.search_assets(
            query=query.get("q"), start=query.get("start"), end=query.get("end"),
            person=query.get("person"), tag=query.get("tag"), event=query.get("event"),
        )
        mappings = self.database.fetch_all(
            "SELECT external_id FROM external_assets WHERE provider = 'immich' AND asset_id IN ({})".format(
                ",".join("?" for _ in assets) or "''"
            ),
            [row["id"] for row in assets],
        )
        external_ids = [row["external_id"] for row in mappings]
        remote = immich.ensure_album(album["name"])
        immich.add_assets(remote["id"], external_ids)
        now = _now()
        self.database.execute(
            "INSERT INTO external_albums(smart_album_id, provider, external_id, synced_at) VALUES (?, 'immich', ?, ?) "
            "ON CONFLICT(smart_album_id, provider) DO UPDATE SET external_id=excluded.external_id, synced_at=excluded.synced_at",
            [smart_album_id, remote["id"], now],
        )
        self.database.execute("UPDATE smart_albums SET materialized_at = ? WHERE id = ?", [now, smart_album_id])
        return {"smart_album_id": smart_album_id, "remote_album_id": remote["id"], "matched_assets": len(assets), "synced_assets": len(external_ids), "missing_mappings": len(assets) - len(external_ids)}

    def map_external_asset(self, asset_id: int, provider: str, external_id: str) -> dict[str, Any]:
        if not self.database.fetch_all("SELECT id FROM assets WHERE id = ?", [asset_id]):
            raise ValueError(f"asset not found: {asset_id}")
        self.database.execute(
            "INSERT INTO external_assets(asset_id, provider, external_id, synced_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(asset_id, provider) DO UPDATE SET external_id=excluded.external_id, synced_at=excluded.synced_at",
            [asset_id, provider, external_id, _now()],
        )
        return self.database.fetch_all("SELECT * FROM external_assets WHERE asset_id = ? AND provider = ?", [asset_id, provider])[0]

    def sync_immich_asset_mappings(self, immich: ImmichClient, path_prefix: str = "/photos") -> dict[str, int]:
        """Match local read-only assets to Immich external-library paths."""
        local_assets = self.database.fetch_all("SELECT id, path FROM assets WHERE missing_at IS NULL")
        remote_assets = immich.all_assets()
        prefix = PurePosixPath(path_prefix)
        by_path: dict[str, dict] = {}
        by_name: dict[str, list[dict]] = {}
        for remote in remote_assets:
            remote_path = remote.get("originalPath") or remote.get("original_path")
            if remote_path:
                by_path[str(PurePosixPath(remote_path))] = remote
            name = remote.get("originalFileName") or remote.get("original_file_name")
            if name:
                by_name.setdefault(name, []).append(remote)
        matched = ambiguous = missing = 0
        for local in local_assets:
            local_path = PurePosixPath(local["path"])
            candidate = by_path.get(str(prefix / local_path.name))
            if candidate is None:
                candidate = by_path.get(str(local_path))
            if candidate is None:
                same_name = by_name.get(local_path.name, [])
                candidate = same_name[0] if len(same_name) == 1 else None
                ambiguous += int(len(same_name) > 1)
            if candidate is None or not candidate.get("id"):
                missing += 1
                continue
            self.map_external_asset(local["id"], "immich", str(candidate["id"]))
            matched += 1
        return {"local_assets": len(local_assets), "remote_assets": len(remote_assets), "matched": matched, "ambiguous": ambiguous, "missing": missing}

    def create_person(self, name: str, confirmed: bool = False) -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO people(name, confirmed, created_at) VALUES (?, ?, ?)", [name, int(confirmed), _now()])
        return self.database.fetch_all("SELECT * FROM people WHERE id = ?", [cursor.lastrowid])[0]

    def create_tag(self, name: str, kind: str = "user") -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO tags(name, kind, created_at) VALUES (?, ?, ?)", [name, kind, _now()])
        return self.database.fetch_all("SELECT * FROM tags WHERE id = ?", [cursor.lastrowid])[0]

    def create_event(self, name: str, level: str = "day", confirmed: bool = False) -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO events(name, level, confirmed, created_at) VALUES (?, ?, ?, ?)", [name, level, int(confirmed), _now()])
        return self.database.fetch_all("SELECT * FROM events WHERE id = ?", [cursor.lastrowid])[0]

from __future__ import annotations

import json
import fcntl
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from .db import Database
from .immich import ImmichClient, ImmichError


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
            clauses.append("lower(t.name) = lower(?) AND at.review_state = 'confirmed'")
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
        return self.database.fetch_all("SELECT e.*, (SELECT COUNT(*) FROM event_assets ea JOIN assets a ON a.id=ea.asset_id WHERE ea.event_id=e.id AND a.missing_at IS NULL) AS asset_count FROM events e ORDER BY starts_at")

    def create_smart_album(self, name: str, query: dict[str, Any]) -> dict[str, Any]:
        cursor = self.database.execute(
            "INSERT INTO smart_albums(name, query_json, created_at) VALUES (?, ?, ?)",
            [name, json.dumps(query, ensure_ascii=False), _now()],
        )
        return self.database.fetch_all("SELECT * FROM smart_albums WHERE id = ?", [cursor.lastrowid])[0]

    def materialize_smart_album(self, smart_album_id: int, immich: ImmichClient) -> dict[str, Any]:
        with self.database.path.with_suffix(".albums.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._materialize_smart_album(smart_album_id, immich)

    def _materialize_smart_album(self, smart_album_id: int, immich: ImmichClient) -> dict[str, Any]:
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
        existing = self.database.fetch_all("SELECT external_id FROM external_albums WHERE smart_album_id = ? AND provider = 'immich'", [smart_album_id])
        if existing:
            remote = {"id": existing[0]["external_id"]}
        else:
            remote = immich.create_album(album["name"])
            # Persist immediately so a failed membership request retries the same album.
            self.database.execute("INSERT INTO external_albums(smart_album_id, provider, external_id, synced_at) VALUES (?, 'immich', ?, ?)", [smart_album_id, remote["id"], _now()])
        immich.add_assets(remote["id"], external_ids)
        if len(external_ids) != len(assets):
            raise ImmichError(f"album {smart_album_id}: {len(assets) - len(external_ids)} assets await Immich mapping")
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

    def sync_immich_asset_mappings(self, immich: ImmichClient, path_prefix: str = "/photos", photo_root: str = "/photos") -> dict[str, int]:
        """Match complete relative paths; a basename alone is not an identity."""
        local_assets = self.database.fetch_all("SELECT id, path FROM assets WHERE missing_at IS NULL")
        remote_assets = immich.all_assets()
        by_path: dict[str, list[dict]] = {}
        for remote in remote_assets:
            path = remote.get("originalPath") or remote.get("original_path")
            if path and remote.get("id"):
                by_path.setdefault(str(PurePosixPath(path)), []).append(remote)
        matched = ambiguous = missing = 0
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM external_assets WHERE provider='immich'")
            for local in local_assets:
                try:
                    relative = PurePosixPath(local["path"]).relative_to(PurePosixPath(photo_root))
                except ValueError:
                    missing += 1
                    continue
                candidates = by_path.get(str(PurePosixPath(path_prefix) / relative), [])
                if len(candidates) != 1:
                    ambiguous += int(len(candidates) > 1)
                    missing += 1
                    continue
                connection.execute("INSERT INTO external_assets(asset_id, provider, external_id, synced_at) VALUES (?, 'immich', ?, ?)", [local["id"], str(candidates[0]["id"]), _now()])
                matched += 1
        return {"local_assets": len(local_assets), "remote_assets": len(remote_assets), "matched": matched, "ambiguous": ambiguous, "missing": missing}

    def create_person(self, name: str, confirmed: bool = False) -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO people(name, confirmed, created_at) VALUES (?, ?, ?)", [name, int(confirmed), _now()])
        return self.database.fetch_all("SELECT * FROM people WHERE id = ?", [cursor.lastrowid])[0]

    def create_tag(self, name: str, kind: str = "user") -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO tags(name, kind, created_at) VALUES (?, ?, ?)", [name, kind, _now()])
        return self.database.fetch_all("SELECT * FROM tags WHERE id = ?", [cursor.lastrowid])[0]

    def create_event(self, name: str, level: str = "day", confirmed: bool = False) -> dict[str, Any]:
        cursor = self.database.execute("INSERT INTO events(name, level, confirmed, review_state, source, created_at) VALUES (?, ?, ?, ?, 'manual', ?)", [name, level, int(confirmed), 'confirmed' if confirmed else 'pending', _now()])
        return self.database.fetch_all("SELECT * FROM events WHERE id = ?", [cursor.lastrowid])[0]

    def review_event(self, event_id: int, state: str, name: str | None = None) -> dict:
        if state not in {"confirmed", "rejected", "pending"}:
            raise ValueError("invalid review state")
        if not self.database.fetch_all("SELECT id FROM events WHERE id = ?", [event_id]):
            raise LookupError("event not found")
        self.database.execute("UPDATE events SET review_state=?, confirmed=?, name=COALESCE(?, name) WHERE id=?", [state, int(state == "confirmed"), name, event_id])
        return self.database.fetch_all("SELECT * FROM events WHERE id=?", [event_id])[0]

    def list_tag_assignments(self) -> list[dict]:
        return self.database.fetch_all("SELECT at.*, t.name, a.path FROM asset_tags at JOIN tags t ON t.id=at.tag_id JOIN assets a ON a.id=at.asset_id WHERE a.missing_at IS NULL ORDER BY at.review_state, a.id")

    def review_tag(self, asset_id: int, tag_id: int, state: str) -> dict:
        if state not in {"confirmed", "rejected", "pending"}:
            raise ValueError("invalid review state")
        if not self.database.fetch_all("SELECT 1 FROM asset_tags WHERE asset_id=? AND tag_id=?", [asset_id, tag_id]):
            raise LookupError("tag assignment not found")
        self.database.execute("UPDATE asset_tags SET review_state=? WHERE asset_id=? AND tag_id=?", [state, asset_id, tag_id])
        return {"asset_id": asset_id, "tag_id": tag_id, "review_state": state}

    def list_smart_albums(self) -> list[dict]:
        rows = self.database.fetch_all("SELECT * FROM smart_albums ORDER BY id DESC")
        for row in rows:
            row["query"] = json.loads(row.pop("query_json"))
        return rows

    def task_status(self) -> dict:
        tasks = self.database.fetch_all("SELECT * FROM task_status ORDER BY name")
        for task in tasks:
            task["result"] = json.loads(task.pop("result_json") or "null")
        count = self.database.fetch_all("SELECT COUNT(*) AS count FROM assets WHERE missing_at IS NULL")[0]["count"]
        return {"assets": count, "tasks": tasks}

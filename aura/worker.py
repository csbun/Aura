from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from .config import ImmichConfig, Settings
from .db import Database
from .events import build_day_candidates
from .enrichment import RecognitionSync, generate_metadata_tags, ensure_face_jobs
from .immich import ImmichClient, ImmichError, ImmichSettings
from .scanner import PhotoScanner
from .service import AuraService

LOGGER = logging.getLogger("aura.worker")
SYNC_INTERVAL = 60


def _run_task(database: Database, name: str, action) -> bool:
    started = datetime.now(timezone.utc).isoformat()
    database.execute(
        "INSERT INTO task_status(name, state, started_at) VALUES (?, 'running', ?) "
        "ON CONFLICT(name) DO UPDATE SET state='running', started_at=excluded.started_at, finished_at=NULL, error=NULL",
        [name, started],
    )
    try:
        result = action()
    except Exception as error:
        LOGGER.exception("automatic task failed: %s", name)
        database.execute("UPDATE task_status SET state='failed', finished_at=?, error=? WHERE name=?", [datetime.now(timezone.utc).isoformat(), str(error), name])
        return False
    errors = result.get("errors", [])
    database.execute("UPDATE task_status SET state=?, finished_at=?, result_json=?, error=? WHERE name=?", ["failed" if errors else "complete", datetime.now(timezone.utc).isoformat(), json.dumps(result), "; ".join(errors) if errors else None, name])
    LOGGER.info("%s=%s", name, result)
    return not errors


def scan_library(settings: Settings, database: Database) -> dict:
    result = PhotoScanner(settings.photo_root, database).scan()
    result["new_event_candidates"] = build_day_candidates(database)
    result["tag_assignments"] = generate_metadata_tags(database)
    return result


def sync_albums(settings: Settings, config: ImmichConfig, database: Database) -> dict:
    if not config.url or not config.api_key:
        return {"configured": False}
    client = ImmichClient(ImmichSettings(config.url, config.api_key))
    service = AuraService(database)
    mapping = service.sync_immich_asset_mappings(client, config.path_prefix, str(settings.photo_root.resolve()))
    _run_task(database, "face_jobs", lambda: ensure_face_jobs(database, client))
    _run_task(database, "recognition", lambda: RecognitionSync(database).run_batch(client))
    synced = 0
    failures = []
    for album in service.list_smart_albums():
        try:
            service.materialize_smart_album(album["id"], client)
            synced += 1
        except Exception as error:
            failures.append(f"album {album['id']}: {error}")
    if failures:
        raise ImmichError("; ".join(failures))
    return {"configured": True, "mapping": mapping, "albums": synced}


def run_once(settings: Settings, immich_config: ImmichConfig) -> None:
    database = Database(settings.db_path)
    database.initialize()
    _run_task(database, "scan", lambda: scan_library(settings, database))
    _run_task(database, "sync", lambda: sync_albums(settings, immich_config, database))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    immich_config = ImmichConfig.from_env()
    database = Database(settings.db_path)
    database.initialize()
    database.execute("UPDATE task_status SET state='interrupted', error='worker restarted before completion' WHERE state='running'")
    interval = max(60, int(os.getenv("AURA_SCAN_INTERVAL", "86400")))
    next_scan = 0.0
    while True:
        if time.monotonic() >= next_scan:
            success = _run_task(database, "scan", lambda: scan_library(settings, database))
            next_scan = time.monotonic() + (interval if success else SYNC_INTERVAL)
        _run_task(database, "sync", lambda: sync_albums(settings, immich_config, database))
        time.sleep(min(SYNC_INTERVAL, max(1, next_scan - time.monotonic())))

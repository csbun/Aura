from __future__ import annotations

import logging
import os
import time

from .config import ImmichConfig, Settings
from .db import Database
from .events import build_day_candidates
from .immich import ImmichClient, ImmichError, ImmichSettings
from .scanner import PhotoScanner
from .service import AuraService

LOGGER = logging.getLogger("aura.worker")


def run_once(settings: Settings, immich_config: ImmichConfig) -> None:
    database = Database(settings.db_path)
    result = PhotoScanner(settings.photo_root, database).scan()
    candidates = build_day_candidates(database)
    LOGGER.info("scan=%s event_candidates=%d", result, candidates)

    if not immich_config.url or not immich_config.api_key:
        LOGGER.info("Immich adapter is not configured; retrying on the next cycle")
        return

    client = ImmichClient(ImmichSettings(immich_config.url, immich_config.api_key))
    service = AuraService(database)
    try:
        mapping = service.sync_immich_asset_mappings(client, immich_config.path_prefix)
        LOGGER.info("Immich asset mapping=%s", mapping)
        for album in database.fetch_all("SELECT id FROM smart_albums"):
            try:
                LOGGER.info("materializing smart album id=%s: %s", album["id"], service.materialize_smart_album(album["id"], client))
            except Exception:
                LOGGER.exception("smart album materialization failed: id=%s", album["id"])
    except ImmichError:
        LOGGER.exception("Immich is unavailable; mapping will retry on the next cycle")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    immich_config = ImmichConfig.from_env()
    interval = max(60, int(os.getenv("AURA_SCAN_INTERVAL", "86400")))
    while True:
        try:
            run_once(settings, immich_config)
        except Exception:
            LOGGER.exception("automatic Aura task failed; retrying later")
        time.sleep(interval)

from __future__ import annotations

import argparse
import json
import logging

from .config import Settings
from .db import Database
from .scanner import PhotoScanner
from .events import build_day_candidates


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Scan a read-only photo library")
    parser.add_argument("command", nargs="?", choices=["scan", "events"], default="scan")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    database = Database(settings.db_path)
    if args.command == "events":
        result = {"created": build_day_candidates(database)}
    else:
        result = PhotoScanner(settings.photo_root, database).scan(args.dry_run)
    print(json.dumps(result, ensure_ascii=False))

from __future__ import annotations

import hashlib
import logging
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

try:
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - optional local decoder
    register_heif_opener = None

if register_heif_opener:
    register_heif_opener()

from .db import Database

LOGGER = logging.getLogger(__name__)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
if register_heif_opener:
    IMAGE_EXTENSIONS |= {".heic", ".heif"}
DATE_TAGS = {36867, 36868, 306}
GPS_TAG = 34853


def _sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _rational(value: Any) -> float | None:
    try:
        return float(value[0]) / float(value[1])
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _gps_coordinates(gps: Any) -> tuple[float | None, float | None]:
    if not isinstance(gps, dict):
        return None, None
    lat = gps.get(2)
    lon = gps.get(4)
    latitude = sum((_rational(part) or 0) / (60** index) for index, part in enumerate(lat or []))
    longitude = sum((_rational(part) or 0) / (60** index) for index, part in enumerate(lon or []))
    if latitude == 0 and longitude == 0:
        return None, None
    if gps.get(1) == "S":
        latitude *= -1
    if gps.get(3) == "W":
        longitude *= -1
    return latitude, longitude


def _capture_time(exif: dict[Any, Any]) -> str | None:
    for tag_id in DATE_TAGS:
        value = exif.get(tag_id)
        if not value:
            continue
        try:
            return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def read_metadata(path: Path) -> dict[str, Any]:
    metadata: dict[str, Any] = {"captured_at": None, "latitude": None, "longitude": None, "width": None, "height": None}
    try:
        # The library is a trusted, read-only NAS source. Pillow's warning is
        # aimed at untrusted uploads; metadata reads do not decode all pixels.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                metadata["width"], metadata["height"] = image.size
                exif = image.getexif()
                metadata["captured_at"] = _capture_time(exif)
                try:
                    gps = exif.get_ifd(GPS_TAG)
                except (AttributeError, KeyError, TypeError):
                    gps = exif.get(GPS_TAG)
                metadata["latitude"], metadata["longitude"] = _gps_coordinates(gps)
    except (OSError, ValueError):
        LOGGER.warning("Could not read image metadata: %s", path)
    if metadata["captured_at"] is None:
        metadata["captured_at"] = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    return metadata


class PhotoScanner:
    """Deep indexing module. It is idempotent and never mutates source files."""

    def __init__(self, root: Path, database: Database):
        self.root = root
        self.database = database

    def iter_images(self):
        if not self.root.exists():
            raise FileNotFoundError(f"PHOTO_ROOT does not exist: {self.root}")
        yield from (path for path in self.root.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)

    def scan(self, dry_run: bool = False) -> dict[str, int]:
        self.database.initialize()
        seen: set[str] = set()
        added = updated = unchanged = 0
        now = datetime.now(timezone.utc).isoformat()
        for index, path in enumerate(self.iter_images(), start=1):
            path = path.resolve()
            stat = path.stat()
            path_text = str(path)
            seen.add(path_text)
            existing = self.database.fetch_all("SELECT sha256, size_bytes, mtime_ns FROM assets WHERE path = ?", [path_text])
            if existing and existing[0]["size_bytes"] == stat.st_size and existing[0]["mtime_ns"] == stat.st_mtime_ns:
                unchanged += 1
                continue
            digest = _sha256(path)
            metadata = read_metadata(path)
            if not dry_run:
                self.database.execute(
                    """INSERT INTO assets(path, sha256, size_bytes, mtime_ns, captured_at, latitude, longitude, width, height, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256, size_bytes=excluded.size_bytes,
                       mtime_ns=excluded.mtime_ns, captured_at=excluded.captured_at, latitude=excluded.latitude,
                       longitude=excluded.longitude, width=excluded.width, height=excluded.height,
                       indexed_at=excluded.indexed_at, missing_at=NULL""",
                    [path_text, digest, stat.st_size, stat.st_mtime_ns, metadata["captured_at"], metadata["latitude"], metadata["longitude"], metadata["width"], metadata["height"], now],
                )
            if existing:
                updated += 1
            else:
                added += 1
            if index % 100 == 0:
                LOGGER.info("scan progress: files=%d added=%d updated=%d unchanged=%d", index, added, updated, unchanged)
        if not dry_run:
            self.database.execute(
                "UPDATE assets SET missing_at = ? WHERE missing_at IS NULL AND path NOT IN ({})".format(",".join("?" for _ in seen) or "''"),
                [now, *seen],
            )
        result = {"added": added, "updated": updated, "unchanged": unchanged, "seen": len(seen)}
        LOGGER.info("scan complete: %s", result)
        return result

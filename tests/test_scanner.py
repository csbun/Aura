from pathlib import Path

from PIL import Image

from aura.db import Database
from aura.scanner import PhotoScanner


def test_scan_is_idempotent_and_read_only(tmp_path: Path):
    photos = tmp_path / "photos"
    photos.mkdir()
    image_path = photos / "trip" / "beach.jpg"
    image_path.parent.mkdir()
    Image.new("RGB", (20, 10), "blue").save(image_path)
    original = image_path.read_bytes()

    database = Database(tmp_path / "data" / "aura.db")
    scanner = PhotoScanner(photos, database)
    assert scanner.scan() == {"added": 1, "updated": 0, "unchanged": 0, "seen": 1}
    assert scanner.scan() == {"added": 0, "updated": 0, "unchanged": 1, "seen": 1}
    assert image_path.read_bytes() == original
    rows = database.fetch_all("SELECT path, width, height FROM assets")
    assert rows[0]["path"] == str(image_path.resolve())
    assert rows[0]["width"] == 20
    assert rows[0]["height"] == 10


def test_dry_run_does_not_write_assets(tmp_path: Path):
    photos = tmp_path / "photos"
    photos.mkdir()
    Image.new("RGB", (2, 2), "red").save(photos / "a.png")
    database = Database(tmp_path / "data" / "aura.db")
    result = PhotoScanner(photos, database).scan(dry_run=True)
    assert result["added"] == 1
    assert database.fetch_all("SELECT * FROM assets") == []

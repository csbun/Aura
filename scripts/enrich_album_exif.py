#!/usr/bin/env python3
"""Create JPEG/PNG copies with source dates and optional city-level GPS estimates."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import subprocess
import time
import zlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from PIL import Image
from PIL.TiffImagePlugin import IFDRational

COUNTRIES = {"Amsterdam": "NL", "Istanbul": "TR", "New York": "US",
             "Paris": "FR", "Tokyo": "JP", "Venice": "IT"}
GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
    temp.replace(path)


def geocode(cities, cache_path):
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    for city in sorted(cities):
        if city in cache:
            continue
        url = GEOCODING_API + "?" + urlencode({"name": city, "countryCode": COUNTRIES[city],
                                               "count": 1, "language": "en", "format": "json"})
        response = subprocess.run(
            ["curl", "-sS", "-fL", "--connect-timeout", "10", "--max-time", "30",
             "--retry", "1", "--retry-delay", "1", url],
            capture_output=True, text=True, timeout=90)
        if response.returncode:
            raise RuntimeError(f"Geocoding {city} failed: {response.stderr.strip()}")
        results = json.loads(response.stdout).get("results", [])
        if not results or results[0].get("country_code") != COUNTRIES[city]:
            raise ValueError(f"No matching country for {city}")
        result = results[0]
        lat, lon = float(result["latitude"]), float(result["longitude"])
        if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"Invalid coordinates for {city}")
        cache[city] = {"latitude": lat, "longitude": lon, "precision": "city_approximation",
                       "source": "GeoNames via Open-Meteo", "source_url": url,
                       "attribution": "GeoNames / Open-Meteo, CC BY 4.0", "result": result}
        write_json(cache_path, cache)
        print(f"{city}: {lat}, {lon} (city approximation)", flush=True)
        time.sleep(1)  # Only six distinct cities, cached for all subsequent photos/runs.
    return cache


def jpeg_segments(data):
    """Return pre-scan segments and untouched compressed scan bytes."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("Only JPEG is supported")
    segments, pos = [], 2
    while pos < len(data):
        start = pos
        if data[pos] != 255:
            raise ValueError("Invalid JPEG marker")
        while pos < len(data) and data[pos] == 255:
            pos += 1
        if pos >= len(data):
            break
        marker = data[pos]
        pos += 1
        if marker in (0xDA, 0xD9):
            return segments, data[start:]
        if marker == 1 or 0xD0 <= marker <= 0xD7:
            segments.append((marker, b"", data[start:pos]))
            continue
        size = int.from_bytes(data[pos:pos+2], "big")
        if size < 2 or pos+size > len(data):
            raise ValueError("Invalid JPEG segment length")
        payload = data[pos+2:pos+size]
        segments.append((marker, payload, data[start:pos+size]))
        pos += size
    raise ValueError("JPEG scan missing")


def replace_exif(data, exif_bytes):
    """Replace only EXIF APP1; preserve XMP, ICC, JFIF and compressed image data."""
    if len(exif_bytes)+2 > 65535:
        raise ValueError("EXIF exceeds JPEG APP1 size limit")
    segments, scan = jpeg_segments(data)
    new = b"\xff\xe1" + (len(exif_bytes)+2).to_bytes(2, "big") + exif_bytes
    output, inserted = [b"\xff\xd8"], False
    for marker, payload, raw in segments:
        if marker == 0xE1 and payload.startswith(b"Exif\0\0"):
            if not inserted:
                output.append(new)
                inserted = True
        else:
            output.append(raw)
    if not inserted:
        output.append(new)
    output.append(scan)
    return b"".join(output)


def replace_png_exif(data, exif_bytes):
    """Insert PNG eXIf without re-encoding IDAT or dropping other chunks."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Unsupported image format; expected JPEG or PNG")
    payload = exif_bytes.removeprefix(b"Exif\0\0")
    chunk = (len(payload).to_bytes(4, "big") + b"eXIf" + payload +
             zlib.crc32(b"eXIf"+payload).to_bytes(4, "big"))
    output, pos, inserted = [data[:8]], 8, False
    while pos+12 <= len(data):
        size = int.from_bytes(data[pos:pos+4], "big")
        kind = data[pos+4:pos+8]
        end = pos+12+size
        if end > len(data):
            raise ValueError("Truncated PNG chunk")
        if kind == b"IDAT" and not inserted:
            output.append(chunk)
            inserted = True
        if kind != b"eXIf":
            output.append(data[pos:end])
        pos = end
        if kind == b"IEND":
            if not inserted:
                raise ValueError("PNG has no IDAT")
            output.append(data[pos:])
            return b"".join(output)
    raise ValueError("PNG has no IEND")


def dms(value):
    # Use integer micro-arcseconds so rounding can never yield 60 seconds.
    units = round(abs(value) * 3600 * 1_000_000)
    degrees, remaining = divmod(units, 3600 * 1_000_000)
    minutes, seconds = divmod(remaining, 60 * 1_000_000)
    return IFDRational(degrees), IFDRational(minutes), IFDRational(seconds, 1_000_000)


def enrich(data, record, location=None):
    with Image.open(io.BytesIO(data)) as image:
        exif = image.getexif()
        sub = dict(exif.get_ifd(34665)) if 34665 in exif else {}
        gps = dict(exif.get_ifd(34853)) if 34853 in exif else {}
        provenance = {}
        if record.get("source_taken_at") and not sub.get(36867):
            dt = datetime.fromisoformat(record["source_taken_at"].replace("Z", "+00:00"))
            sub[36867] = dt.strftime("%Y:%m:%d %H:%M:%S")
            provenance["DateTimeOriginal"] = {"source": "dataset_metadata",
                "original_value": record["source_taken_at"], "timezone": "unverified; offset not written"}
        if location and not (gps.get(2) and gps.get(4)):
            lat, lon = location["latitude"], location["longitude"]
            gps.update({0: b"\x02\x03\x00\x00", 1: "N" if lat >= 0 else "S", 2: dms(lat),
                        3: "E" if lon >= 0 else "W", 4: dms(lon),
                        27: b"ASCII\0\0\0CITY_APPROXIMATION"})
            exif[34853] = gps
            provenance["GPS"] = {"city": record["city"], **location}
        if not provenance:
            return data, provenance
        note = {"metadata_enrichment": "Aura album sample", "date": provenance.get("DateTimeOriginal"),
                "gps": {k: v for k, v in provenance.get("GPS", {}).items() if k != "result"},
                "photo_source": {k: record[k] for k in
                    ("dataset", "source_url", "author", "license", "source_original_photo_id")
                    if record.get(k)},
                "date_mapping": record.get("timestamp_mapping")}
        previous_comment = sub.get(37510)
        if previous_comment:
            note["original_user_comment"] = (previous_comment.hex() if isinstance(previous_comment, bytes)
                                              else str(previous_comment))
            note["original_user_comment_encoding"] = "hex" if isinstance(previous_comment, bytes) else "text"
        sub[37510] = b"ASCII\0\0\0" + json.dumps(note, ensure_ascii=True).encode("ascii")
        exif[34665] = sub
        replace = replace_exif if data.startswith(b"\xff\xd8") else replace_png_exif
        return replace(data, exif.tobytes()), provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("sample-photos/cities"))
    parser.add_argument("--output", type=Path, default=Path("sample-photos/cities-exif"))
    parser.add_argument("--geocode", action="store_true", help="Write approximate city coordinates")
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error("Use a separate output directory to preserve downloaded originals")
    records = [json.loads(line) for line in (args.source / "manifest.jsonl").read_text().splitlines()]
    locations = geocode({r["city"] for r in records if r.get("city")},
                        args.source.parent / ".cache" / "city-coordinates.json") if args.geocode else {}
    args.output.mkdir(parents=True, exist_ok=True)
    output_records = []
    summary = {"input": len(records), "written": 0, "date_added": 0, "approximate_gps_added": 0,
               "skipped_without_source_metadata": 0, "verified_lossless": 0}
    for record in records:
        rel = Path(record["file"])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("Unsafe manifest path")
        original = (args.source / rel).read_bytes()
        if hashlib.sha256(original).hexdigest() != record["sha256"]:
            raise ValueError(f"Source checksum changed: {rel}")
        data, provenance = enrich(original, record, locations.get(record.get("city")))
        if not provenance:
            summary["skipped_without_source_metadata"] += 1
            continue
        with Image.open(io.BytesIO(original)) as before, Image.open(io.BytesIO(data)) as after:
            if before.size != after.size or before.tobytes() != after.tobytes():
                raise ValueError(f"Pixel data changed: {rel}")
            exif = after.getexif()
            sub = exif.get_ifd(34665)
            if "DateTimeOriginal" in provenance and not sub.get(36867):
                raise ValueError(f"Capture date verification failed: {rel}")
            gps = exif.get_ifd(34853) if 34853 in exif else {}
            if "GPS" in provenance:
                for key, ref, field in [(2, 1, "latitude"), (4, 3, "longitude")]:
                    values = gps[key]
                    actual = sum(float(v)/scale for v, scale in zip(values, (1, 60, 3600)))
                    actual *= -1 if gps[ref] in ("S", "W") else 1
                    if abs(actual-provenance["GPS"][field]) > 1e-6:
                        raise ValueError(f"GPS verification failed: {rel}")
        destination = args.output / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != data:
            raise ValueError(f"Output differs; use a fresh directory: {destination}")
        destination.write_bytes(data)
        new = {**record, "sha256": hashlib.sha256(data).hexdigest(), "original_sha256": record["sha256"],
               "exif_modified": True, "exif_provenance": provenance, "has_exif": True,
               "exif_datetime_original": sub.get(36867, ""), "has_exif_gps": bool(gps.get(2) and gps.get(4)),
               "exif_gps": {str(k): str(v) for k, v in gps.items()}}
        write_json(destination.with_suffix(".json"), new)
        output_records.append(new)
        summary["written"] += 1
        summary["date_added"] += "DateTimeOriginal" in provenance
        summary["approximate_gps_added"] += "GPS" in provenance
        summary["verified_lossless"] += 1
    (args.output / "manifest.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in output_records))
    write_json(args.output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

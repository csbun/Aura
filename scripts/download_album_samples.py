#!/usr/bin/env python3
"""Download coherent CITIES trips and full-frame PIPA photos (not face crops).

Requires curl, Pillow, and PyArrow. See docs/album-samples.md.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageOps, ImageDraw

CITIES_URL = "https://hucvl.github.io/visual-storygraphs/yfcmmf00m-cities.zip"
PIPA_REPO = "HugC/MVEB-test"
PIPA_REVISION = "8126e36012415238dff1429fd20c39552ddb1661"


class NetworkUnavailable(Exception):
    """A DNS failure is not evidence that an individual photo is missing."""


def fetch(url, path, *, byte_range=None, timeout=90):
    """Bounded curl downloads, atomic publication; never retain error bodies."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    headers = path.with_suffix(path.suffix + ".headers")
    cmd = ["curl", "--silent", "--show-error", "--fail", "--location",
           "--connect-timeout", "10", "--max-time", str(timeout),
           "--retry", "1", "--retry-delay", "1", "--output", str(tmp)]
    if byte_range:
        start, end = byte_range
        cmd += ["--range", f"{start}-{end}", "--max-filesize", str(end-start+1),
                "--dump-header", str(headers)]
    cmd.append(url)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout*2+25)
        if result.returncode in (5, 6):
            raise NetworkUnavailable("DNS lookup failed; check network/proxy settings before retrying")
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[-400:])
        if byte_range:
            expected = f"bytes {start}-{end}/"
            if (tmp.stat().st_size != end-start+1 or
                    expected not in headers.read_text().lower()):
                raise RuntimeError("Server did not honor the requested byte range")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
        headers.unlink(missing_ok=True)


class RangeFile(io.RawIOBase):
    """Seekable HTTP file backed by verified, persistent 8 MiB range chunks."""
    chunk_size = 8 * 1024 * 1024

    def __init__(self, url, size, cache):
        super().__init__()
        self.url, self.size, self.cache = url, size, Path(cache)
        self.pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        pos = offset if whence == 0 else self.pos+offset if whence == 1 else self.size+offset
        if pos < 0 or whence not in (0, 1, 2):
            raise ValueError("Invalid seek")
        self.pos = pos
        return pos

    def read(self, size=-1):
        end = self.size if size < 0 else min(self.size, self.pos+size)
        parts = []
        while self.pos < end:
            start = self.pos // self.chunk_size * self.chunk_size
            stop = min(start+self.chunk_size, self.size)
            path = self.cache / f"{start}-{stop-1}.bin"
            if not path.exists() or path.stat().st_size != stop-start:
                fetch(self.url, path, byte_range=(start, stop-1))
            with path.open("rb") as stream:
                stream.seek(self.pos-start)
                data = stream.read(min(end, stop)-self.pos)
            parts.append(data)
            self.pos += len(data)
        return b"".join(parts)


def image_info(data, min_edge):
    with Image.open(io.BytesIO(data)) as im:
        im.verify()
    with Image.open(io.BytesIO(data)) as im:
        im.load()  # Detect truncation, not just header validity.
        if max(im.size) < min_edge or min(im.size) < 200:
            raise ValueError(f"Image too small: {im.size}")
        exif = im.getexif()
        sub = exif.get_ifd(34665) if 34665 in exif else {}
        gps = exif.get_ifd(34853) if 34853 in exif else {}
        return {"width": im.width, "height": im.height, "format": im.format,
                "has_exif": bool(exif),
                "exif_datetime_original": str(sub.get(36867) or exif.get(36867) or ""),
                "has_exif_gps": bool(gps.get(2) and gps.get(4)),
                "exif_gps": {str(k): str(v) for k, v in gps.items()},
                "sha256": hashlib.sha256(data).hexdigest()}


class Collection:
    def __init__(self, root, target, min_edge):
        self.root, self.target, self.min_edge = root, target, min_edge
        root.mkdir(parents=True, exist_ok=True)
        self.manifest = root / "manifest.jsonl"
        self.records = []
        if self.manifest.exists():
            for line in self.manifest.read_text().splitlines():
                r = json.loads(line)
                path = root / r["file"]
                if path.exists():
                    info = image_info(path.read_bytes(), min_edge)
                    if info["sha256"] != r["sha256"]:
                        raise ValueError(f"Modified sample: {path}")
                    self.records.append(r)
        if len(self.records) > target:
            raise ValueError("Output already exceeds target; use a new --output directory")
        self.hashes = {r["sha256"] for r in self.records}
        self.ids = {r["sample_id"] for r in self.records}
        self.save_manifest()

    def save_manifest(self):
        tmp = self.manifest.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in self.records))
        tmp.replace(self.manifest)

    @property
    def full(self):
        return len(self.records) >= self.target

    def add(self, data, metadata):
        if self.full or metadata["sample_id"] in self.ids:
            return False
        info = image_info(data, self.min_edge)
        if info["sha256"] in self.hashes:
            return False
        group = re.sub(r"[^A-Za-z0-9_.-]+", "_", metadata["group"])
        ext = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(info["format"])
        if not ext:
            raise ValueError(f"Unsupported image format: {info['format']}")
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", metadata["sample_id"])
        rel = Path("photos") / group / (name + ext)
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)  # Preserve bytes and existing EXIF, no recompression.
        record = {**metadata, **info, "file": str(rel)}
        path.with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n")
        self.records.append(record)
        self.ids.add(metadata["sample_id"])
        self.hashes.add(info["sha256"])
        self.save_manifest()
        if len(self.records) % 25 == 0 or self.full:
            print(f"{self.root.name}: {len(self.records)}/{self.target}", flush=True)
        return True

    def report(self):
        rs = self.records
        summary = {"target": self.target, "downloaded": len(rs), "complete": self.full,
                   "unique_sha256": len(self.hashes),
                   "with_exif": sum(r["has_exif"] for r in rs),
                   "with_exif_datetime_original": sum(bool(r["exif_datetime_original"]) for r in rs),
                   "with_exif_gps": sum(r["has_exif_gps"] for r in rs),
                   "with_source_date": sum(bool(r.get("source_taken_at")) for r in rs),
                   "groups": dict(Counter(r["group"] for r in rs)),
                   "cities": dict(Counter(r["city"] for r in rs if r.get("city")))}
        (self.root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n")
        # Contact sheet samples groups evenly, uses display-only EXIF orientation.
        groups = defaultdict(list)
        for r in rs:
            groups[r["group"]].append(r)
        selected = []
        all_groups = list(groups.values())
        for k in range(min(8, len(all_groups))):
            items = all_groups[k*len(all_groups)//min(8, len(all_groups))]
            for n in range(min(5, len(items))):
                selected.append(items[n*len(items)//min(5, len(items))])
        if selected:
            sheet = Image.new("RGB", (1000, ((len(selected)+4)//5)*160), "white")
            draw = ImageDraw.Draw(sheet)
            for i, r in enumerate(selected):
                with Image.open(self.root / r["file"]) as im:
                    thumb = ImageOps.exif_transpose(im).convert("RGB")
                    thumb.thumbnail((196, 132))
                    x, y = i % 5 * 200, i // 5 * 160
                    sheet.paste(thumb, (x, y))
                    draw.text((x, y+133), r["group"][:28], fill="black")
            sheet.save(self.root / "contact-sheet.jpg")
        return summary


def cities_groups(archive):
    grouped = defaultdict(list)
    with ZipFile(archive) as z:
        for name in sorted(z.namelist()):
            if not name.endswith(".csv"):
                continue
            for row in csv.DictReader(io.StringIO(z.read(name).decode("utf-8-sig")), delimiter="\t"):
                if row["video_b"].lower() == "true" or not row["taken_dt"]:
                    continue
                city, collection = name.split("/")
                key = (city, collection.removesuffix(".csv"), row["taken_dt"][:10])
                grouped[key].append(row)
    by_city = defaultdict(list)
    for key, rows in grouped.items():
        if len(rows) >= 20:
            by_city[key[0]].append((key, sorted(rows, key=lambda r: (r["taken_dt"], r["id"]))))
    for city in by_city:
        # Prefer day-sized collections; keep related photos together.
        by_city[city].sort(key=lambda item: (abs(len(item[1])-75), item[0]))
    return by_city


def download_cities(args, cache, output):
    archive = cache / "cities.zip"
    if not archive.exists():
        fetch(CITIES_URL, archive, timeout=180)
    grouped = cities_groups(archive)
    collection = Collection(output, args.count, args.min_edge)
    failures = output / "failures.jsonl"

    def download(row):
        url = row["url_s"].replace("http:", "https:")
        # Published derivatives use the same Flickr secret; originals do NOT.
        # Never invent an original URL by appending _o.
        large = re.sub(r"\.jpg$", "_b.jpg", url)
        dest = cache / "cities-images" / (row["id"] + ".jpg")
        url_path = dest.with_suffix(".url")
        error = ""
        for candidate in dict.fromkeys([large, url]):
            try:
                if not dest.exists() or not url_path.exists():
                    fetch(candidate, dest, timeout=25)
                    url_path.write_text(candidate)
                data = dest.read_bytes()
                image_info(data, args.min_edge)
                return data, url_path.read_text(), ""
            except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as e:
                error = str(e)
                dest.unlink(missing_ok=True)
        return None, None, error

    city_names = sorted(grouped)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # Equal city quotas; failed albums are replaced within that city.
        for city_index, city in enumerate(city_names):
            quota = args.count//len(city_names) + (city_index < args.count % len(city_names))
            city_count = sum(r.get("city") == city for r in collection.records)
            for (city, album, day), rows in grouped[city]:
                if city_count >= quota or collection.full:
                    break
                group = f"{city}_{album}_{day}"
                group_count = sum(r["group"] == group for r in collection.records)
                limit = min(args.per_group, quota-city_count+group_count)
                rows = [r for r in rows if r["id"] not in collection.ids]
                print(f"CITIES: {group}, candidates={len(rows)}", flush=True)
                consecutive_failures = 0
                for start in range(0, len(rows), args.workers):
                    if group_count >= limit or city_count >= quota:
                        break
                    batch = rows[start:start+min(args.workers, limit-group_count)]
                    for row, (data, url, error) in zip(batch, pool.map(download, batch)):
                        if data is None:
                            consecutive_failures += 1
                            with failures.open("a") as log:
                                log.write(json.dumps({"id": row["id"], "error": error})+"\n")
                            continue
                        consecutive_failures = 0
                        meta = {"sample_id": row["id"], "dataset": "CITIES", "group": group,
                                "group_kind": "source_collection_and_capture_day", "city": city,
                                "source_collection": album, "source_taken_at": row["taken_dt"],
                                "source_date_note": "Value from CITIES CSV; timezone not independently verified",
                                "source_url": row["urlpage_s"], "download_url": url,
                                "license": row["license_s"], "author": row["user_s"],
                                "source_metadata": row, "exif_modified": False}
                        if collection.add(data, meta):
                            city_count += 1
                            group_count += 1
                    if consecutive_failures >= args.workers*2:
                        break
    return collection.report()


def download_pipa(args, cache, output):
    import pyarrow.parquet as pq

    base = args.hf_endpoint.rstrip("/")
    index_path = cache / "pipa-tree.json"
    if not index_path.exists():
        fetch(f"{base}/api/datasets/{PIPA_REPO}/tree/{args.pipa_revision}/PIPA", index_path)
    index = json.loads(index_path.read_text())
    root = f"{base}/datasets/{PIPA_REPO}/resolve/{args.pipa_revision}"
    candidates_path = cache / "pipa-candidates.parquet"
    if not candidates_path.exists():
        fetch(root+"/PIPA/candidate.parquet", candidates_path)
    candidates = pq.read_table(candidates_path).to_pylist()
    by_media = defaultdict(list)
    for row in candidates:
        by_media[row["media_index"]].append(row)
    collection = Collection(output, args.count, args.min_edge)
    offset = 0
    for entry in sorted((e for e in index if re.search(r"/media-.*\.parquet$", e["path"])), key=lambda e: e["path"]):
        if collection.full:
            break
        remote = RangeFile(root+"/"+entry["path"], entry["size"], cache / Path(entry["path"]).name)
        parquet = pq.ParquetFile(remote)
        total = parquet.metadata.num_rows
        wanted = {i-offset for i in by_media if offset <= i < offset+total}
        if not wanted:
            offset += total
            parquet.close()
            remote.close()
            continue  # Query images are face crops: never include them.
        group_start = 0
        for rg in range(parquet.num_row_groups):
            size = parquet.metadata.row_group(rg).num_rows
            if not any(group_start <= i < group_start+size for i in wanted):
                group_start += size
                continue
            print(f"PIPA: reading full-photo row group {rg} of {entry['path']}", flush=True)
            table = parquet.read_row_group(rg, columns=["image"])
            for j, row in enumerate(table.to_pylist()):
                media_index = offset+group_start+j
                if media_index not in by_media:
                    continue
                annotations = by_media[media_index]
                identity = str(annotations[0]["instance_id"])
                group = f"person_{identity}"
                if sum(r["group"] == group for r in collection.records) >= args.per_group:
                    continue
                data = (row["image"] or {}).get("bytes")
                if not data:
                    continue
                meta = {"sample_id": f"media_{media_index}", "dataset": "PIPA",
                        "group": group, "group_kind": "MVEB_person_identity_not_original_album",
                        "source_url": f"https://huggingface.co/datasets/{PIPA_REPO}/tree/{args.pipa_revision}/PIPA",
                        "source_shard": entry["path"], "source_revision": args.pipa_revision,
                        "media_index": media_index, "source_annotations": annotations,
                        "source_taken_at": None, "source_original_photo_id": None,
                        "license": "MVEB dataset card: CC-BY-NC-SA-4.0; original per-photo license unavailable",
                        "exif_modified": False}
                try:
                    collection.add(data, meta)
                except (ValueError, OSError) as e:
                    with (output / "failures.jsonl").open("a") as log:
                        log.write(json.dumps({"media_index": media_index, "error": str(e)})+"\n")
                if collection.full:
                    break
            group_start += size
            if collection.full:
                break
        offset += total
        parquet.close()
        remote.close()
    return collection.report()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=["all", "cities", "pipa"], default="all")
    parser.add_argument("--count", type=int, default=500, help="Images per dataset")
    parser.add_argument("--output", type=Path, default=Path("sample-photos"))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--per-group", type=int, default=50)
    parser.add_argument("--min-edge", type=int, default=500, help="Minimum long edge in pixels")
    parser.add_argument("--hf-endpoint", default="https://huggingface.co")
    parser.set_defaults(pipa_revision=PIPA_REVISION)
    args = parser.parse_args()
    if min(args.count, args.workers, args.per_group, args.min_edge) < 1:
        parser.error("Counts, dimensions, and workers must be positive")
    cache = args.output / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    summary_path = args.output / "summary.json"
    summaries = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    for name, download in [("cities", download_cities), ("pipa", download_pipa)]:
        if args.dataset not in ("all", name):
            continue
        try:
            summaries[name] = download(args, cache, args.output / name)
        except Exception as e:
            print(f"{name}: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            summaries[name] = {"complete": False, "error": str(e)}
    summary_path.write_text(json.dumps(summaries, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(summaries, ensure_ascii=False, indent=2), flush=True)
    selected = summaries.values() if args.dataset == "all" else [summaries[args.dataset]]
    return 0 if all(r["complete"] for r in selected) else 1


if __name__ == "__main__":
    raise SystemExit(main())

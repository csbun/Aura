#!/usr/bin/env python3
"""Download, restore metadata, write EXIF, and retain only rewritten photos."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
MARKER = ".album-samples-work.json"


def no_symlinks(root):
    if root.is_symlink() or any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError(f"Symlinks are not supported: {root}")


def validate_photo(path, dataset):
    with Image.open(path) as image:
        image.load()
        exif = image.getexif()
        sub = exif.get_ifd(34665) if 34665 in exif else {}
        gps = exif.get_ifd(34853) if 34853 in exif else {}
        date = sub.get(36867)
        datetime.strptime(date or "", "%Y:%m:%d %H:%M:%S")
        # Older exports preserved an existing camera comment rather than adding
        # our JSON note. Their GPS method still explicitly identifies enrichment.
        if (b"Aura album sample" not in sub.get(37510, b"") and
                not (dataset == "cities" and b"CITY_APPROXIMATION" in gps.get(27, b""))):
            raise ValueError(f"Missing enrichment provenance: {path}")
        if dataset == "cities":
            if not gps.get(2) or not gps.get(4):
                raise ValueError(f"Missing city GPS: {path}")
            for key, ref, maximum in ((2, 1, 90), (4, 3, 180)):
                d, m, s = map(float, gps[key])
                directions = ("N", "S") if ref == 1 else ("E", "W")
                if not (0 <= d <= maximum and 0 <= m < 60 and 0 <= s < 60
                        and d+m/60+s/3600 <= maximum
                        and gps[ref] in directions):
                    raise ValueError(f"Invalid GPS: {path}")
        return date


def validate_final(output, count):
    """Recognize a completed photos-only export without needing deleted manifests."""
    no_symlinks(output)
    if {p.name for p in output.iterdir()} != {"cities", "pipa"}:
        raise ValueError(f"Output must contain only cities/ and pipa/: {output}")
    counts = {}
    for name in ("cities", "pipa"):
        folder = output / name
        if not folder.is_dir():
            raise ValueError(f"Not a directory: {folder}")
        files = [p for p in folder.rglob("*") if p.is_file()]
        if any(p.suffix.lower() not in IMAGE_SUFFIXES for p in files):
            raise ValueError(f"Unexpected non-photo files in {folder}")
        if (name == "cities" and len(files) != count) or not 0 < len(files) <= count:
            raise ValueError(f"Unexpected {name} photo count: {len(files)} (input target {count})")
        hashes = set()
        for path in files:
            validate_photo(path, name)
            hashes.add(hashlib.sha256(path.read_bytes()).hexdigest())
        if len(hashes) != len(files):
            raise ValueError(f"Duplicate photos in {folder}")
        counts[name] = len(files)
    return counts


def verified_exports(work, count):
    """Validate both complete exports before publishing or removing any inputs."""
    exports = {}
    for name in ("cities", "pipa"):
        source = work / name
        enriched = work / (name+"-exif")
        raw_records = [json.loads(s) for s in (source/"manifest.jsonl").read_text().splitlines()]
        if len(raw_records) != count:
            raise ValueError(f"Incomplete {name} download")
        raw = {r["sample_id"]: r for r in raw_records}
        expected = {key for key, r in raw.items() if r.get("source_taken_at")}
        records = [json.loads(s) for s in (enriched/"manifest.jsonl").read_text().splitlines()]
        if not expected or len(records) != len(expected) or {r["sample_id"] for r in records} != expected:
            raise ValueError(f"Incomplete {name} EXIF export")
        report = json.loads((enriched/"summary.json").read_text())
        if report["verified_lossless"] != len(records) or report["written"] != len(records):
            raise ValueError(f"Incomplete {name} lossless verification")
        files = {}
        for record in records:
            rel = Path(record["file"])
            if rel.is_absolute() or ".." in rel.parts or rel.parts[0] != "photos":
                raise ValueError("Unsafe export path")
            path = enriched / rel
            if path.suffix.lower() not in IMAGE_SUFFIXES or not record.get("exif_modified"):
                raise ValueError(f"Not an enriched photo: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            original = raw[record["sample_id"]]
            if digest != record["sha256"] or record["original_sha256"] != original["sha256"]:
                raise ValueError(f"Export checksum mismatch: {path}")
            date = validate_photo(path, name)
            expected_date = datetime.fromisoformat(original["source_taken_at"].replace("Z", "+00:00"))
            if date != expected_date.strftime("%Y:%m:%d %H:%M:%S"):
                raise ValueError(f"Export date mismatch: {path}")
            files[rel.relative_to("photos")] = (path, digest)
        if len(files) != len(records):
            raise ValueError("Duplicate export filenames")
        exports[name] = files
    return exports


def publish(work, output, count):
    exports = verified_exports(work, count)
    stage = work / "final"
    # This is a managed, reproducible staging directory, never a user output.
    if stage.exists():
        shutil.rmtree(stage)
    for name, files in exports.items():
        for rel, (source, digest) in files.items():
            target = stage / name / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError(f"Copy checksum mismatch: {target}")
    counts = validate_final(stage, count)
    if output.exists():
        raise ValueError(f"Output appeared during preparation; refusing to replace it: {output}")
    stage.rename(output)  # Same filesystem checked before downloading.
    return counts


def prepare(args, run=subprocess.run):
    output_arg = args.output.absolute()
    work_arg = (args.work_dir or output_arg.with_name("."+output_arg.name+"-work")).absolute()
    if output_arg.is_symlink() or work_arg.is_symlink():
        raise ValueError("Output/work directory cannot be a symlink")
    output, work = output_arg.resolve(), work_arg.resolve()
    if output == work or output in work.parents or work in output.parents:
        raise ValueError("Output and work directories must be separate, not nested")
    settings = {"version": 1, "output": str(output), "count": args.count,
                "per_group": args.per_group, "min_edge": args.min_edge}
    if output.exists():
        counts = validate_final(output, args.count)
        # A previous run might have been interrupted immediately after publication.
        if work.exists():
            no_symlinks(work)
            marker = work / MARKER
            if not marker.exists() or json.loads(marker.read_text()) != settings:
                raise ValueError(f"Unrecognized work directory; leaving it untouched: {work}")
            with (work/".lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                exports = verified_exports(work, args.count)
                for name, files in exports.items():
                    if len(files) != counts[name]:
                        raise ValueError("Published export does not match work directory")
                    for rel, (_, digest) in files.items():
                        if hashlib.sha256((output/name/rel).read_bytes()).hexdigest() != digest:
                            raise ValueError("Published export differs; keeping work directory")
                shutil.rmtree(work)
        print(f"Already prepared: {counts}. No downloads needed.")
        return counts
    work.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    no_symlinks(work)
    if work.stat().st_dev != output.parent.stat().st_dev:
        raise ValueError("Output and work directory must be on the same filesystem")
    marker = work / MARKER
    if not marker.exists():
        if any(work.iterdir()):
            raise ValueError(f"Refusing to use a nonempty unmanaged work directory: {work}")
        marker.write_text(json.dumps(settings, indent=2)+"\n")
    elif json.loads(marker.read_text()) != settings:
        raise ValueError("Work directory belongs to a different configuration")
    with (work/".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        commands = [
            ["download_album_samples.py", "--output", str(work), "--count", str(args.count),
             "--workers", str(args.workers), "--per-group", str(args.per_group),
             "--min-edge", str(args.min_edge), "--hf-endpoint", args.hf_endpoint],
            ["restore_pipa_dates.py", "--source", str(work/"pipa"), "--hf-endpoint", args.hf_endpoint],
            ["enrich_album_exif.py", "--source", str(work/"cities"),
             "--output", str(work/"cities-exif"), "--geocode"],
            ["enrich_album_exif.py", "--source", str(work/"pipa"), "--output", str(work/"pipa-exif")],
        ]
        for command in commands:
            print(f"Running {command[0]}", flush=True)
            run([sys.executable, str(SCRIPT_DIR/command[0]), *command[1:]], check=True)
        no_symlinks(work)
        counts = publish(work, output, args.count)
        shutil.rmtree(work)
    print(f"Prepared {counts}. Only rewritten photos remain in {output}.")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("sample-photos"))
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--count", type=int, default=500, help="Source photos per dataset; final PIPA count may be lower")
    parser.add_argument("--hf-endpoint", default="https://huggingface.co")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--per-group", type=int, default=50)
    parser.add_argument("--min-edge", type=int, default=500)
    args = parser.parse_args()
    if min(args.count, args.workers, args.per_group, args.min_edge) < 1:
        parser.error("Numeric options must be positive")
    try:
        prepare(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Preparation stopped: {error}. Any existing work files are retained for retry.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Recover PIPA photo IDs and official dates with conservative crop matching.

This matches existing dataset annotations, not names or real-world identities.
Only a unique original photo passing geometry AND pixel checks is accepted.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from PIL import Image
from scipy.io import loadmat

if __package__:
    from .download_album_samples import fetch, PIPA_REPO, PIPA_REVISION
else:
    from download_album_samples import fetch, PIPA_REPO, PIPA_REVISION

OFFICIAL_BASE = "https://datasets.d2.mpi-inf.mpg.de/oh15iccv/"


def load_official(index_path, timestamps_path):
    rows = [line.split() for line in index_path.read_text().splitlines() if line.strip()]
    dates = loadmat(timestamps_path)["date_vec"]
    if dates.shape != (len(rows), 6) or any(len(row) != 8 for row in rows):
        raise ValueError("Official instance index and timestamp rows do not align")
    by_geometry, photo_dates = defaultdict(list), defaultdict(set)
    for i, (row, date) in enumerate(zip(rows, dates)):
        value = datetime(*map(int, date)).isoformat() if np.any(date) else None
        if value:
            photo_dates[row[1]].add(value)
        by_geometry[(row[6], int(row[4]), int(row[5]))].append((i, row))
    return rows, dates, by_geometry, photo_dates


def accepted_photo(matches):
    """Reject ambiguous or weak correspondences instead of borrowing a date."""
    good = [m for m in matches if m["pixel_mae"] < 10 and m["pixel_correlation"] > .95]
    if len({m["photo_id"] for m in good}) != 1:
        return None
    return min(good, key=lambda m: m["pixel_mae"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("sample-photos/pipa"))
    parser.add_argument("--hf-endpoint", default="https://huggingface.co")
    args = parser.parse_args()
    cache = args.source.parent / ".cache" / "pipa-timestamps"
    cache.mkdir(parents=True, exist_ok=True)
    mirror = f"{args.hf_endpoint.rstrip('/')}/datasets/{PIPA_REPO}/resolve/{PIPA_REVISION}/PIPA/"
    for name, url in {
        "index.txt": OFFICIAL_BASE+"index.txt",
        "data_timestamp.mat": OFFICIAL_BASE+"data_timestamp.mat",
        "query.parquet": mirror+"query.parquet",
        "query-media-00000.parquet": mirror+"media-00000-of-00002.parquet",
    }.items():
        if not (cache / name).exists():
            fetch(url, cache / name, timeout=180)
    _, dates, by_geometry, photo_dates = load_official(cache/"index.txt", cache/"data_timestamp.mat")
    queries = pq.read_table(cache/"query.parquet").to_pylist()
    media = pq.read_table(cache/"query-media-00000.parquet").to_pylist()
    manifest = args.source / "manifest.jsonl"
    records = [json.loads(line) for line in manifest.read_text().splitlines()]
    candidate_ids = {a["id"] for r in records for a in r["source_annotations"]}
    by_candidate = defaultdict(list)
    for query in queries:
        ids = candidate_ids.intersection(query["pos_ids"])
        if not ids or query["media_index"] >= len(media):
            continue  # Missing query media is NOT a mapping; never infer from order.
        data = media[query["media_index"]]["image"]["bytes"]
        with Image.open(io.BytesIO(data)) as image:
            pixels = np.asarray(image.convert("RGB"), dtype=np.float32)
            size = image.size
        if pixels.std() < 10:
            continue  # Uniform patches are too ambiguous.
        for cid in ids:
            by_candidate[cid].append((query, size, pixels, hashlib.sha256(data).hexdigest()))
    evidence = []
    summary = {"input": len(records), "matched_photo_ids": 0, "dates_restored": 0,
               "matched_but_missing_or_conflicting_date": 0, "unmatched": 0,
               "official_file_sha256": {name: hashlib.sha256((cache/name).read_bytes()).hexdigest()
                                        for name in ("index.txt", "data_timestamp.mat")}}
    for record in records:
        if record.get("source_revision") != PIPA_REVISION:
            raise ValueError("PIPA revision mismatch")
        source = args.source / record["file"]
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError(f"Image changed: {source}")
        matches = []
        with Image.open(io.BytesIO(raw)) as image:
            full = image.convert("RGB")
            for annotation in record["source_annotations"]:
                for query, size, pixels, query_sha in by_candidate[annotation["id"]]:
                    for i, original in by_geometry[(query["instance_id"], *size)]:
                        x, y, w, h = map(int, original[2:6])
                        crop = np.asarray(full.crop((x, y, x+w, y+h)), dtype=np.float32)
                        correlation = float(np.corrcoef(crop.ravel(), pixels.ravel())[0, 1]) if crop.std() > 1 else 0
                        matches.append({"photo_id": original[1], "album_id": original[0],
                            "official_instance_row_zero_based": i, "bbox_xywh": [x, y, w, h],
                            "query_media_index": query["media_index"], "query_sha256": query_sha,
                            "pixel_mae": float(np.abs(crop-pixels).mean()), "pixel_correlation": correlation})
        match = accepted_photo(matches)
        audit = {"sample_id": record["sample_id"], "match": match}
        if match is None:
            summary["unmatched"] += 1
            audit["best_candidates"] = sorted(matches, key=lambda m: m["pixel_mae"])[:3]
            evidence.append(audit)
            continue
        summary["matched_photo_ids"] += 1
        row = match["official_instance_row_zero_based"]
        date = dates[row]
        values = photo_dates[match["photo_id"]]
        taken = datetime(*map(int, date)).isoformat() if np.any(date) and len(values) == 1 else None
        record.update({"source_original_photo_id": match["photo_id"],
                       "source_original_album_id": match["album_id"],
                       "source_taken_at": taken,
                       "source_date_note": "Official PIPA photo-taken-date; timezone unspecified",
                       "timestamp_mapping": {**match, "method": "unique_identity_bbox_geometry_and_crop_pixels",
                           "index_url": OFFICIAL_BASE+"index.txt", "timestamp_url": OFFICIAL_BASE+"data_timestamp.mat"}})
        if taken:
            summary["dates_restored"] += 1
        else:
            summary["matched_but_missing_or_conflicting_date"] += 1
        audit["source_taken_at"] = taken
        evidence.append(audit)
    backup = cache / "manifest-before-timestamps.jsonl"
    if not backup.exists():
        backup.write_bytes(manifest.read_bytes())
    for record in records:
        (args.source/record["file"]).with_suffix(".json").write_text(json.dumps(record, ensure_ascii=False, indent=2)+"\n")
    temp = manifest.with_suffix(".tmp")
    temp.write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in records))
    temp.replace(manifest)
    for name, value in [("timestamp-report.json", summary), ("timestamp-mapping.json", evidence)]:
        (args.source/name).write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n")
    summary_path = args.source/"summary.json"
    existing = json.loads(summary_path.read_text())
    existing["with_source_date"] = sum(bool(r.get("source_taken_at")) for r in records)
    existing["timestamp_restoration"] = summary
    summary_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2)+"\n")
    combined_path = args.source.parent/"summary.json"
    if combined_path.exists():
        combined = json.loads(combined_path.read_text())
        combined[args.source.name] = existing
        combined_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Exercise complete export/cleanup with real JPEG/PNG fixtures, offline."""
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from scripts.enrich_album_exif import enrich
from scripts.prepare_album_samples import MARKER, prepare


def settings(root):
    return SimpleNamespace(output=root/"photos", work_dir=root/"work", count=2,
                           per_group=50, min_edge=500, workers=2, hf_endpoint="https://example.test")


def fixtures(work):
    for name in ("cities", "pipa"):
        originals, exports = [], []
        folder = work/name
        rewritten = work/(name+"-exif")
        for i, color in enumerate(("red", "blue")):
            is_city = name == "cities"
            rel = Path("photos/group") / f"{i}.{'jpg' if is_city else 'png'}"
            stream = io.BytesIO()
            Image.new("RGB", (640, 480), color).save(stream, format="JPEG" if is_city else "PNG")
            raw = stream.getvalue()
            record = {"sample_id": str(i), "file": str(rel), "city": "Paris" if is_city else None,
                      "source_taken_at": "2010-06-21T09:54:25" if is_city or i == 0 else None,
                      "sha256": hashlib.sha256(raw).hexdigest()}
            (folder/rel).parent.mkdir(parents=True, exist_ok=True)
            (folder/rel).write_bytes(raw)
            originals.append(record)
            data, provenance = enrich(raw, record, {"latitude": 48.85, "longitude": 2.35} if is_city else None)
            if not provenance:
                continue
            (rewritten/rel).parent.mkdir(parents=True, exist_ok=True)
            (rewritten/rel).write_bytes(data)
            (rewritten/rel).with_suffix(".json").write_text("{}")
            exports.append({**record, "exif_modified": True,
                            "original_sha256": record["sha256"], "sha256": hashlib.sha256(data).hexdigest()})
        (folder/"manifest.jsonl").write_text("".join(json.dumps(r)+"\n" for r in originals))
        (rewritten/"manifest.jsonl").write_text("".join(json.dumps(r)+"\n" for r in exports))
        (rewritten/"summary.json").write_text(json.dumps({"written": len(exports), "verified_lossless": len(exports)}))
    (work/".cache").mkdir()
    (work/".cache"/"download.zip").write_bytes(b"cache")


class PrepareAlbumTests(unittest.TestCase):
    def test_pipeline_exports_photos_only_and_repeat_does_not_download(self):
        with tempfile.TemporaryDirectory() as temp:
            args = settings(Path(temp))
            calls = []

            def runner(command, check):
                self.assertTrue(check)
                calls.append(Path(command[1]).name)
                if len(calls) == 4:
                    fixtures(args.work_dir)

            self.assertEqual(prepare(args, runner), {"cities": 2, "pipa": 1})
            self.assertEqual(calls, ["download_album_samples.py", "restore_pipa_dates.py",
                                     "enrich_album_exif.py", "enrich_album_exif.py"])
            self.assertFalse(args.work_dir.exists())
            files = [p for p in args.output.rglob("*") if p.is_file()]
            self.assertEqual(len(files), 3)
            self.assertEqual({p.suffix for p in files}, {".jpg", ".png"})
            self.assertEqual(prepare(args, lambda *_a, **_k: self.fail("Should not download")),
                             {"cities": 2, "pipa": 1})

    def test_corrupt_export_keeps_work_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            args = settings(Path(temp))
            calls = []

            def runner(command, check):
                calls.append(command)
                if len(calls) == 4:
                    fixtures(args.work_dir)
                    (args.work_dir/"pipa-exif/photos/group/0.png").write_bytes(b"broken")

            with self.assertRaisesRegex(ValueError, "checksum"):
                prepare(args, runner)
            self.assertFalse(args.output.exists())
            self.assertTrue((args.work_dir/"cities/photos/group/0.jpg").exists())

    def test_failed_stage_keeps_owned_work_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            args = settings(Path(temp))

            def runner(command, check):
                raise subprocess.CalledProcessError(1, command)

            with self.assertRaises(subprocess.CalledProcessError):
                prepare(args, runner)
            self.assertTrue((args.work_dir/MARKER).exists())
            self.assertFalse(args.output.exists())

    def test_resume_cleanup_after_publication_interruption(self):
        with tempfile.TemporaryDirectory() as temp:
            args = settings(Path(temp))
            calls = []

            def runner(command, check):
                calls.append(command)
                if len(calls) == 4:
                    fixtures(args.work_dir)

            with patch("scripts.prepare_album_samples.shutil.rmtree", side_effect=OSError("interrupted cleanup")):
                with self.assertRaises(OSError):
                    prepare(args, runner)
            self.assertTrue(args.output.exists())
            self.assertTrue(args.work_dir.exists())
            prepare(args, lambda *_a, **_k: self.fail("Should only resume cleanup"))
            self.assertFalse(args.work_dir.exists())

    def test_unmanaged_work_and_nested_paths_are_not_deleted(self):
        with tempfile.TemporaryDirectory() as temp:
            args = settings(Path(temp))
            args.work_dir.mkdir()
            sentinel = args.work_dir/"personal-file.txt"
            sentinel.write_text("keep me")
            with self.assertRaisesRegex(ValueError, "unmanaged"):
                prepare(args)
            self.assertEqual(sentinel.read_text(), "keep me")
            args.output = args.work_dir/"nested"
            with self.assertRaisesRegex(ValueError, "nested"):
                prepare(args)


if __name__ == "__main__":
    unittest.main()

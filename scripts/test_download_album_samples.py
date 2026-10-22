"""Offline checks for the optional sample downloader (separate dependencies)."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq

from scripts import download_album_samples as samples


def jpeg(color="red", size=(640, 480)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="JPEG")
    return stream.getvalue()


class AlbumSampleTests(unittest.TestCase):
    def test_rejects_invalid_small_and_truncated_images(self):
        for data in (b"<html>not a photo</html>", jpeg(size=(100, 100)), jpeg()[:-100]):
            with self.subTest(size=len(data)), self.assertRaises((ValueError, OSError)):
                samples.image_info(data, 500)

    def test_preserves_bytes_deduplicates_and_resumes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            c = samples.Collection(root, 2, 500)
            data = jpeg()
            meta = {"sample_id": "1", "group": "same_album"}
            self.assertTrue(c.add(data, meta))
            self.assertFalse(c.add(data, {**meta, "sample_id": "2"}))
            self.assertEqual((root / c.records[0]["file"]).read_bytes(), data)
            resumed = samples.Collection(root, 2, 500)
            self.assertEqual(len(resumed.records), 1)
            self.assertTrue(resumed.add(jpeg("blue"), {**meta, "sample_id": "3"}))
            self.assertFalse(resumed.add(jpeg("green"), {**meta, "sample_id": "4"}))
            self.assertTrue(resumed.report()["complete"])

    def test_reads_original_capture_date_without_inventing_missing_values(self):
        exif = Image.Exif()
        exif[34665] = {36867: "2012:06:07 08:09:10"}
        stream = io.BytesIO()
        Image.new("RGB", (640, 480), "red").save(stream, format="JPEG", exif=exif)
        info = samples.image_info(stream.getvalue(), 500)
        self.assertEqual(info["exif_datetime_original"], "2012:06:07 08:09:10")
        self.assertFalse(info["has_exif_gps"])
        self.assertEqual(samples.image_info(jpeg(), 500)["exif_datetime_original"], "")

    def test_range_reader_crosses_chunks_and_reuses_cache(self):
        data = b"0123456789abcdef"
        calls = []

        def fake_fetch(url, path, *, byte_range):
            calls.append(byte_range)
            path.parent.mkdir(parents=True, exist_ok=True)
            a, b = byte_range
            path.write_bytes(data[a:b+1])

        with tempfile.TemporaryDirectory() as temp, patch.object(samples, "fetch", fake_fetch):
            remote = samples.RangeFile("https://example.test/file", len(data), temp)
            remote.chunk_size = 5
            remote.seek(3)
            self.assertEqual(remote.read(9), b"3456789ab")
            remote.seek(-4, 2)
            self.assertEqual(remote.read(), b"cdef")
            remote.seek(0)
            self.assertEqual(remote.read(), data)
            self.assertEqual(len(calls), 4)

    def test_pipa_skips_query_crops_and_counts_unique_full_photos(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache, output = root / "cache", root / "out"
            cache.mkdir()
            # A query crop and candidate can share one shard. Indexes are global.
            payloads = [[jpeg(size=(50, 50))],
                        [jpeg(size=(50, 50)), jpeg(), jpeg(), jpeg("blue")]]
            entries = []
            files = {}
            for i, payload in enumerate(payloads):
                name = f"PIPA/media-{i:05d}-of-00002.parquet"
                file = root / f"part{i}.parquet"
                table = pa.Table.from_pylist([{"image": {"bytes": p, "path": None}} for p in payload])
                pq.write_table(table, file, row_group_size=1)
                entries.append({"path": name, "size": file.stat().st_size})
                files[name] = file
            (cache / "pipa-tree.json").write_text(json.dumps(entries))
            pq.write_table(pa.Table.from_pylist([
                {"media_index": i, "instance_id": "person-A", "id": str(i)}
                for i in (2, 3, 4)
            ]), cache / "pipa-candidates.parquet")

            def local_remote(url, size, cache_dir):
                name = "PIPA/" + url.rsplit("/", 1)[1]
                return files[name].open("rb")

            args = SimpleNamespace(hf_endpoint="https://example.test", pipa_revision="test",
                                   count=2, min_edge=500, per_group=50)
            with patch.object(samples, "RangeFile", side_effect=local_remote):
                result = samples.download_pipa(args, cache, output)
            self.assertTrue(result["complete"])
            records = [json.loads(r) for r in (output / "manifest.jsonl").read_text().splitlines()]
            self.assertEqual([r["media_index"] for r in records], [2, 4])
            self.assertEqual(result["with_exif_datetime_original"], 0)
            self.assertTrue(all(r["source_original_photo_id"] is None for r in records))


if __name__ == "__main__":
    unittest.main()

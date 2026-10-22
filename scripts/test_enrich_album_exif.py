"""Offline checks for lossless date/GPS metadata restoration."""
import io
import unittest

from PIL import Image
from PIL.PngImagePlugin import PngInfo

from scripts.enrich_album_exif import dms, enrich, jpeg_segments


def photo(exif=None):
    stream = io.BytesIO()
    Image.new("RGB", (100, 80), "purple").save(stream, format="JPEG", exif=exif or b"")
    return stream.getvalue()


class ExifEnrichmentTests(unittest.TestCase):
    def test_date_and_approximate_gps_preserve_pixels_and_xmp(self):
        exif = Image.Exif()
        exif[274] = 6
        raw = photo(exif)
        xmp = b"http://ns.adobe.com/xap/1.0/\0<test>preserve me</test>"
        raw = raw[:2] + b"\xff\xe1" + (len(xmp)+2).to_bytes(2, "big") + xmp + raw[2:]
        updated, provenance = enrich(raw, {"city": "Test", "source_taken_at": "2013-04-30T12:33:26Z"},
                                     {"latitude": -33.8688, "longitude": -151.2093, "precision": "city_approximation"})
        before_segments, before_scan = jpeg_segments(raw)
        after_segments, after_scan = jpeg_segments(updated)
        self.assertEqual(before_scan, after_scan)
        non_exif = lambda ss: [r for _, p, r in ss if not p.startswith(b"Exif\0\0")]
        self.assertEqual(non_exif(before_segments), non_exif(after_segments))
        with Image.open(io.BytesIO(updated)) as im:
            exif = im.getexif()
            self.assertEqual(exif[274], 6)
            self.assertEqual(exif.get_ifd(34665)[36867], "2013:04:30 12:33:26")
            self.assertNotIn(36881, exif.get_ifd(34665))  # No invented UTC offset.
            gps = exif.get_ifd(34853)
            self.assertEqual((gps[1], gps[3]), ("S", "W"))
            lat = -sum(float(v)/scale for v, scale in zip(gps[2], (1, 60, 3600)))
            self.assertAlmostEqual(lat, -33.8688, places=6)
            self.assertIn(b"CITY_APPROXIMATION", gps[27])
        self.assertEqual(provenance["GPS"]["precision"], "city_approximation")

    def test_existing_native_date_and_gps_are_preserved(self):
        exif = Image.Exif()
        exif[34665] = {36867: "2001:02:03 04:05:06"}
        exif[34853] = {1: "N", 2: dms(10), 3: "E", 4: dms(20)}
        raw = photo(exif)
        updated, provenance = enrich(raw, {"city": "Test", "source_taken_at": "2013-01-01T00:00:00Z"},
                                     {"latitude": 50, "longitude": 60})
        self.assertEqual(updated, raw)
        self.assertEqual(provenance, {})

    def test_no_source_metadata_means_no_changes(self):
        raw = photo()
        self.assertEqual(enrich(raw, {"source_taken_at": None}), (raw, {}))

    def test_png_preserves_compressed_image_and_text_chunks(self):
        stream = io.BytesIO()
        info = PngInfo()
        info.add_text("Author", "Original author")
        Image.new("RGB", (100, 80), "green").save(stream, format="PNG", pnginfo=info)
        raw = stream.getvalue()
        updated, _ = enrich(raw, {"source_taken_at": "2010-06-21T09:54:25"})

        def non_exif_chunks(data):
            chunks, pos = [], 8
            while pos < len(data):
                end = pos+12+int.from_bytes(data[pos:pos+4], "big")
                if data[pos+4:pos+8] != b"eXIf":
                    chunks.append(data[pos:end])
                pos = end
            return chunks

        self.assertEqual(non_exif_chunks(raw), non_exif_chunks(updated))
        with Image.open(io.BytesIO(updated)) as im:
            self.assertEqual(im.info["Author"], "Original author")
            self.assertEqual(im.getexif().get_ifd(34665)[36867], "2010:06:21 09:54:25")


if __name__ == "__main__":
    unittest.main()

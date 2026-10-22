import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy.io import savemat

from scripts.restore_pipa_dates import accepted_photo, load_official


class PipaDateTests(unittest.TestCase):
    def test_rejects_ambiguous_and_weak_matches(self):
        good = {"photo_id": "1", "pixel_mae": 3, "pixel_correlation": .99}
        self.assertEqual(accepted_photo([good]), good)
        self.assertIsNone(accepted_photo([good, {**good, "photo_id": "2"}]))
        self.assertIsNone(accepted_photo([{**good, "pixel_correlation": .5}]))
        self.assertIsNone(accepted_photo([{**good, "pixel_mae": 20}]))
        # Multiple annotated people can still identify the same source photo.
        self.assertIsNotNone(accepted_photo([good, {**good, "pixel_mae": 4}]))

    def test_official_order_zeros_and_length_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            index, times = root/"index.txt", root/"dates.mat"
            index.write_text("10 100 1 2 30 40 7 3\n10 101 1 2 30 40 7 3\n")
            savemat(times, {"date_vec": np.array([[2010, 6, 21, 9, 54, 25], [0]*6], dtype=np.uint16)})
            _, _, geometry, dates = load_official(index, times)
            self.assertEqual(dates["100"], {"2010-06-21T09:54:25"})
            self.assertNotIn("101", dates)
            self.assertEqual([n for n, _ in geometry[("7", 30, 40)]], [0, 1])
            savemat(times, {"date_vec": np.zeros((1, 6), dtype=np.uint16)})
            with self.assertRaises(ValueError):
                load_official(index, times)


if __name__ == "__main__":
    unittest.main()

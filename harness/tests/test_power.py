#!/usr/bin/env python3
"""bench.py power: Fisher's exact test significance table.

Run with: python3 -m unittest discover harness/tests
"""
import os
import sys
import unittest

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
import bench  # noqa: E402


class TestPower(unittest.TestCase):
    def test_extreme_split_is_far_below_threshold(self):
        # A 10/10 vs 0/10 split: order of magnitude ~0.00001.
        self.assertLess(bench.fisher_p(10, 0, 10), 0.001)

    def test_equal_counts_are_not_significant(self):
        self.assertEqual(bench.fisher_p(5, 5, 10), 1.0)

    def test_table_has_one_row_per_baseline_count_and_no_none_at_n10(self):
        out = []
        self.assertEqual(bench.power(10, out=out.append), 0)
        rows = out[2:]
        self.assertEqual(len(rows), 11)
        self.assertTrue(all("none" not in r for r in rows))


if __name__ == "__main__":
    unittest.main()

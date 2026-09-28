#!/usr/bin/env python3
"""bench.py's per-trial fixture stability: fixture_diff (undeclared vs declared restarts),
vmstat_bi_mean (the host-thrash abort threshold parser), and wait_for_restart (the
signal_after_restart gate).

Run with: python3 -m unittest discover harness/tests
Python 3 stdlib only, no network, no LLM, no ssh, no docker.
"""
import os
import sys
import unittest
from unittest import mock

HARNESS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HARNESS_DIR)
import bench  # noqa: E402

BEFORE = {"recommendation": ("0", "false", "2026-09-28T10:00:00Z"), "frontend": ("0", "false", "2026-09-28T10:00:00Z")}


class TestFixtureDiff(unittest.TestCase):
    def test_undeclared_restart_is_not_ok(self):
        after = dict(BEFORE, recommendation=("1", "true", "2026-09-28T10:06:00Z"))
        changes, ok = bench.fixture_diff(BEFORE, after, [])
        self.assertEqual([c["container"] for c in changes], ["recommendation"])
        self.assertFalse(ok)

    def test_declared_restart_is_ok(self):
        after = dict(BEFORE, recommendation=("1", "true", "2026-09-28T10:06:00Z"))
        changes, ok = bench.fixture_diff(BEFORE, after, ["recommendation"])
        self.assertEqual([c["container"] for c in changes], ["recommendation"])
        self.assertTrue(ok)

    def test_no_changes_is_ok_with_nothing_declared(self):
        changes, ok = bench.fixture_diff(BEFORE, dict(BEFORE), [])
        self.assertEqual(changes, [])
        self.assertTrue(ok)

    def test_a_second_undeclared_container_still_fails_even_with_one_declared(self):
        after = dict(BEFORE, recommendation=("1", "true", "t"), frontend=("1", "false", "t2"))
        changes, ok = bench.fixture_diff(BEFORE, after, ["recommendation"])
        self.assertEqual(sorted(c["container"] for c in changes), ["frontend", "recommendation"])
        self.assertFalse(ok)

    def test_an_empty_snapshot_either_side_fails_closed(self):
        for before, after in ((BEFORE, {}), ({}, BEFORE), ({}, {})):
            changes, ok = bench.fixture_diff(before, after, ["recommendation", "frontend"])
            self.assertEqual(changes, ["snapshot_failed"])
            self.assertFalse(ok)  # never "no changes" just because nothing could be read


VMSTAT_CALM = """procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 0  0      0 4000000  30000 1000000    0    0 999999   100 1000 1000  1  1 98  0  0
 0  0      0 4000000  30000 1000000    0    0   150    80 1000 1000  0  0 100  0  0
 0  0      0 4000000  30000 1000000    0    0   180    90 1000 1000  0  0 100  0  0
"""
VMSTAT_THRASHING = """procs -----------memory---------- ---swap-- -----io---- -system-- ------cpu-----
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st
 0  0      0 4000000  30000 1000000    0    0    10   100 1000 1000  1  1 98  0  0
 3  2      0  400000  30000  100000    0    0 60000   900 1000 1000 10 40 10 40  0
 3  2      0  300000  30000   90000    0    0 70000  1000 1000 1000 10 40 10 40  0
"""


class TestVmstatBiMean(unittest.TestCase):
    def test_first_sample_is_excluded_so_a_huge_boot_average_does_not_abort(self):
        # First sample's bi is 999999 (since boot); the non-first samples (150, 180) mean 165.
        self.assertAlmostEqual(bench.vmstat_bi_mean(VMSTAT_CALM), 165.0)

    def test_thrashing_host_means_over_threshold(self):
        mean = bench.vmstat_bi_mean(VMSTAT_THRASHING)
        self.assertGreater(mean, 50000)


class TestWaitForRestart(unittest.TestCase):
    def test_stops_polling_the_call_after_restart_count_rises(self):
        snaps = [{"recommendation": ("0", "false", "t")}, {"recommendation": ("0", "false", "t")},
                 {"recommendation": ("1", "false", "t")}, {"recommendation": ("1", "false", "t")}]  # would keep going if polled again
        with mock.patch.object(bench, "container_snapshot", side_effect=lambda cfg: snaps.pop(0)), \
                mock.patch.object(bench, "sleep") as slept:
            polls = bench.wait_for_restart({}, "recommendation", 0, poll_max=10)
        self.assertEqual(polls, 3)  # rose on the 3rd poll
        self.assertEqual(slept.call_count, 2)  # slept after poll 1 and 2, never after the one that saw it rise
        self.assertEqual(snaps, [{"recommendation": ("1", "false", "t")}])  # the 4th canned reading was never consumed

    def test_never_rising_exhausts_poll_max_and_reports_none(self):
        with mock.patch.object(bench, "container_snapshot", return_value={"recommendation": ("0", "false", "t")}), \
                mock.patch.object(bench, "sleep"):
            polls = bench.wait_for_restart({}, "recommendation", 0, poll_max=3)
        self.assertIsNone(polls)


if __name__ == "__main__":
    unittest.main()

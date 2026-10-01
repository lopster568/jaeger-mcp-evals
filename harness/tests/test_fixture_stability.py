#!/usr/bin/env python3
"""bench.py's per-trial fixture stability: fixture_diff (undeclared vs declared restarts),
vmstat_bi_mean (the host-thrash abort threshold parser), and wait_for_restart (the
signal_after_restart gate).

Run with: python3 -m unittest discover harness/tests
Python 3 stdlib only, no network, no LLM, no ssh, no docker.
"""
import json
import os
import sys
import tempfile
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


class TestContainerSnapshotScope(unittest.TestCase):
    def test_lists_only_the_jaeger_containers_compose_project(self):
        # 2026-09-30: g-mamba-versionspec-algebra-solver-2 appeared and s-mamba-versionspec-algebra-2
        # vanished (another project on the shared host) and marked four trials INVALID.
        out = "/jaeger 0 false 2026-09-30T11:49:08Z\n"
        with mock.patch.object(bench, "fixture_sh", return_value=mock.Mock(returncode=0, stdout=out)) as sh:
            self.assertEqual(bench.container_snapshot({}), {"jaeger": ("0", "false", "2026-09-30T11:49:08Z")})
        cmd = sh.call_args[0][1]
        self.assertIn("docker ps -aq --filter label=com.docker.compose.project=$(docker inspect -f", cmd)
        self.assertTrue(cmd.rstrip().endswith("jaeger))"))


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


class TestRestartBeforeRun(unittest.TestCase):
    def test_recommendation_restarts_first_and_jaeger_only_once_it_is_running(self):
        log = []
        snaps = [{"recommendation": ("0", "false", "t1")}, {"recommendation": ("0", "false", "t1")},
                 {"recommendation": ("0", "false", "t2")}]  # 2nd poll still old, 3rd is running again

        def snap(cfg):
            log.append("snapshot")
            return snaps.pop(0)

        def sh(cfg, cmd, **kw):
            log.append(cmd)
            return mock.Mock(returncode=0)

        answers = [OSError("down"), []]  # jaeger refuses once, then answers

        def traces(*a):
            log.append("traces")
            r = answers.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(bench, "container_snapshot", side_effect=snap), mock.patch.object(bench, "fixture_sh", side_effect=sh), \
                mock.patch.object(bench, "traces", side_effect=traces), mock.patch.object(bench, "sleep"):
            rec = bench.restart_before_run({}, ["recommendation"], "recommendation", poll_max=5)
        self.assertEqual([c for c in log if c != "snapshot" and c != "traces"], ["docker restart recommendation", "docker restart jaeger"])
        self.assertEqual(log.index("docker restart jaeger"), 4)  # snapshot(before), restart, 2 polls (old, then new StartedAt), then jaeger
        self.assertEqual(log.count("traces"), 2)
        self.assertEqual(set(rec), {"containers", "jaeger"})

    def test_never_running_aborts_before_jaeger_is_touched(self):
        cmds = []
        with mock.patch.object(bench, "container_snapshot", return_value={"recommendation": ("0", "false", "t1")}), \
                mock.patch.object(bench, "fixture_sh", side_effect=lambda cfg, cmd, **kw: cmds.append(cmd) or mock.Mock(returncode=0)), \
                mock.patch.object(bench, "sleep"):
            with self.assertRaises(RuntimeError):
                bench.restart_before_run({}, ["recommendation"], "recommendation", poll_max=2)
        self.assertEqual(cmds, ["docker restart recommendation"])


class TestScenarioAtBatchSha(unittest.TestCase):
    def _batch(self, sha):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "meta.json"), "w") as f:
            json.dump({"scenario": "s", "harness_git_sha": sha}, f)
        return d

    def test_loads_the_scenario_at_the_recorded_sha_and_strips_dirty(self):
        with mock.patch.object(bench.subprocess, "run", return_value=mock.Mock(returncode=0, stdout='{"version": 2}')) as run, \
                mock.patch("builtins.print"):
            self.assertEqual(bench.scenario_at_batch_sha(self._batch("abc123-dirty")), {"version": 2})
        self.assertIn("abc123:harness/scenarios/s.json", run.call_args[0][0])

    def test_unresolvable_sha_falls_back_to_the_current_file(self):
        with mock.patch.object(bench.subprocess, "run", return_value=mock.Mock(returncode=128, stdout="")), \
                mock.patch("builtins.print") as pr:
            self.assertIsNone(bench.scenario_at_batch_sha(self._batch("abc123")))
        self.assertIn("using the current file", pr.call_args[0][0])


if __name__ == "__main__":
    unittest.main()

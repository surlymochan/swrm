"""M23 slice 1 (#193): `openswarm quota sync` + probe cache integration + stale guard.

Background (#192): M20/M21/M22 real-backend evidence was hand-copied from
gocc ~/.codex/sessions rate_limits lines to the VM; codex_limits() only
reads local {HOME}/.codex/sessions, so the VM never sees real codex quota.

This slice wires the copy into a command: `openswarm quota sync` pulls
the latest rate_limits lines from gocc over the existing SSH config
(read-only on the remote side — list, tail, grep; nothing written back,
no credentials touched) into a local cache ({DATA}/quota-codex-snapshot.json
with source host + sync timestamp). The probe reads the snapshot when the
host has no local sessions; local sessions still win when present. A
snapshot older than the configurable threshold (default 60 minutes) is
reported stale: the probe returns no codex limits, `openswarm quota` says
so explicitly, and routing treats codex quota as unknown (old behaviour)
with the snapshot provenance on the ledger record.

Run: python3 -m unittest tests.test_m23_slice1
     (or: python3 tests/test_m23_slice1.py)
"""
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _load  # noqa: E402

xs = _load.load()
xs.breakers = lambda: {}  # isolate from any breakers on disk
xs.load_policy = lambda: xs.jload(f"{xs.SKILL_DIR}/policy.json", {})  # ignore policy.local.json overrides

POLICY = xs.load_policy()
POLICY["codex"]["require_default_model"] = False
with open(_load.FIXTURE_STATE) as f:
    BASE = json.load(f)

PI_ISOLATED = os.path.join(_load.ROOT, "bin", "pi-isolated")

# Fixed "now" for deterministic ages: 2026-11-13T13:20:00Z.
NOW_FIXED = 1794576000


def rate_line(limit_id="codex", used=5.0, window=10080, ts="2026-11-13T13:07:18.935Z",
              resets=1795164141):
    return json.dumps({"timestamp": ts, "type": "event_msg",
                       "payload": {"type": "token_count",
                                   "rate_limits": {"limit_id": limit_id, "limit_name": None,
                                                   "primary": {"used_percent": used,
                                                               "window_minutes": window,
                                                               "resets_at": resets},
                                                   "secondary": None,
                                                   "credits": {"has_credits": True},
                                                   "rate_limit_reached_type": None}}})


def snapshot_dict(limits, synced_at, host="gocc"):
    return {"version": 1, "source_host": host, "source": f"{host}:~/.codex/sessions",
            "synced_at": synced_at, "synced_at_iso": xs.iso(synced_at),
            "raw_lines": [], "limits": limits}


def quota_policy(order, threshold=0, sources=None, enabled=True):
    policy = copy.deepcopy(POLICY)
    policy["routing"]["default_order"] = list(order)
    policy["routing"]["weighted"] = False
    policy["routing"]["quota"] = {"enabled": enabled, "threshold": threshold,
                                  "sources": sources or {}}
    return policy


class Slice1Base(unittest.TestCase):
    def setUp(self):
        self._now_patch = mock.patch.object(xs, "NOW", return_value=NOW_FIXED)
        self._now_patch.start()
        self.addCleanup(self._now_patch.stop)
        # Start each test with no snapshot cache on disk.
        try:
            os.unlink(xs.CODEX_SNAPSHOT)
        except OSError:
            pass

    def write_snapshot(self, limits, synced_at, host="gocc"):
        xs.jsave(xs.CODEX_SNAPSHOT, snapshot_dict(limits, synced_at, host))
        return xs.CODEX_SNAPSHOT


class ParseRateLimits(Slice1Base):
    def test_newest_event_per_limit_wins(self):
        lines = [rate_line(used=5.0, ts="2026-11-13T13:07:18.935Z"),
                 rate_line(used=42.0, ts="2026-11-13T12:00:00.000Z"),
                 rate_line(limit_id="other", used=80.0, ts="2026-11-13T13:00:00.000Z")]
        found = xs.parse_rate_limits_lines(lines)
        self.assertEqual(found["codex"]["windows"]["seven_day"]["used"], 0.05)
        self.assertEqual(found["other"]["windows"]["seven_day"]["used"], 0.8)
        # observed_at is local-time ISO; it must parse back to the event minute.
        observed = xs.to_epoch(found["codex"]["observed_at"])
        self.assertAlmostEqual(observed, xs.to_epoch("2026-11-13T13:07:18.935Z"), delta=60)

    def test_newest_wins_regardless_of_input_order(self):
        lines = [rate_line(used=42.0, ts="2026-11-13T12:00:00.000Z"),
                 rate_line(used=5.0, ts="2026-11-13T13:07:18.935Z")]
        found = xs.parse_rate_limits_lines(lines)
        self.assertEqual(found["codex"]["windows"]["seven_day"]["used"], 0.05)

    def test_non_rate_limits_and_malformed_lines_ignored(self):
        lines = ['{"timestamp":"2026-11-13T13:00:00.000Z","type":"event_msg","payload":'
                 '{"type":"text","text":"hello"}}',
                 '{"timestamp": broken json',
                 '{"timestamp":"2026-11-13T13:00:00.000Z","payload":{}}',
                 rate_line(used=5.0)]
        found = xs.parse_rate_limits_lines(lines)
        self.assertEqual(list(found), ["codex"])

    def test_window_names(self):
        lines = [rate_line(used=10.0, window=300, ts="2026-11-13T13:00:00.000Z")]
        found = xs.parse_rate_limits_lines(lines)
        self.assertIn("five_hour", found["codex"]["windows"])
        lines = [rate_line(used=10.0, window=60, ts="2026-11-13T13:00:00.000Z")]
        found = xs.parse_rate_limits_lines(lines)
        self.assertIn("w60", found["codex"]["windows"])

    def test_dateless_line_never_replaces_timestamped(self):
        lines = [rate_line(used=5.0, ts="2026-11-13T13:07:18.935Z"),
                 json.dumps({"payload": {"rate_limits": {"limit_id": "codex",
                                                         "primary": {"used_percent": 99.0,
                                                                     "window_minutes": 10080,
                                                                     "resets_at": None}}}})]
        found = xs.parse_rate_limits_lines(lines)
        self.assertEqual(found["codex"]["windows"]["seven_day"]["used"], 0.05)

    def test_merges_into_existing_found(self):
        found = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        xs.parse_rate_limits_lines([rate_line(used=9.0, ts="2026-11-13T13:08:00.000Z")], found)
        self.assertEqual(found["codex"]["windows"]["seven_day"]["used"], 0.09)


class SnapshotCache(Slice1Base):
    def test_save_load_roundtrip(self):
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        path = self.write_snapshot(limits, NOW_FIXED - 600)
        loaded = xs.load_codex_snapshot()
        self.assertEqual(loaded["source_host"], "gocc")
        self.assertEqual(loaded["synced_at"], NOW_FIXED - 600)
        self.assertEqual(loaded["limits"], limits)
        self.assertEqual(path, xs.CODEX_SNAPSHOT)

    def test_load_missing_and_malformed_returns_none(self):
        self.assertIsNone(xs.load_codex_snapshot())
        with open(xs.CODEX_SNAPSHOT, "w") as fh:
            fh.write("{not json")
        self.assertIsNone(xs.load_codex_snapshot())
        xs.jsave(xs.CODEX_SNAPSHOT, {"source_host": "gocc"})  # no limits
        self.assertIsNone(xs.load_codex_snapshot())
        xs.jsave(xs.CODEX_SNAPSHOT, {"limits": {}, "synced_at": "not-a-number"})
        self.assertIsNone(xs.load_codex_snapshot())

    def test_status_age_and_stale_boundary(self):
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 60 * 60)  # exactly 60 min old
        status = xs.codex_snapshot_status(POLICY)
        self.assertEqual(status["age_minutes"], 60.0)
        self.assertFalse(status["stale"])  # at the threshold still fresh
        self.assertEqual(status["stale_threshold_minutes"], 60)
        self.assertEqual(status["source_host"], "gocc")
        self.write_snapshot(limits, NOW_FIXED - 61 * 60)  # 61 min old
        status = xs.codex_snapshot_status(POLICY)
        self.assertEqual(status["age_minutes"], 61.0)
        self.assertTrue(status["stale"])

    def test_status_none_without_snapshot(self):
        self.assertIsNone(xs.codex_snapshot_status(POLICY))

    def test_env_threshold_override(self):
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 30 * 60)  # 30 min old
        with mock.patch.dict(os.environ, {"OPENSWARM_QUOTA_SNAPSHOT_MAX_AGE_MINUTES": "10"}):
            self.assertTrue(xs.codex_snapshot_status(POLICY)["stale"])
            self.assertEqual(xs.codex_snapshot_max_age_minutes(POLICY), 10)
        with mock.patch.dict(os.environ, {"OPENSWARM_QUOTA_SNAPSHOT_MAX_AGE_MINUTES": "0"}):
            with self.assertRaises(ValueError):
                xs.codex_snapshot_max_age_minutes(POLICY)
        with mock.patch.dict(os.environ, {"OPENSWARM_QUOTA_SNAPSHOT_MAX_AGE_MINUTES": "soon"}):
            with self.assertRaises(ValueError):
                xs.codex_snapshot_max_age_minutes(POLICY)

    def test_policy_threshold(self):
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 90 * 60)  # 90 min old
        policy = copy.deepcopy(POLICY)
        policy["routing"]["quota"]["snapshot_max_age_minutes"] = 120
        self.assertFalse(xs.codex_snapshot_status(policy)["stale"])
        policy["routing"]["quota"]["snapshot_max_age_minutes"] = 0
        with self.assertRaises(ValueError):
            xs.codex_snapshot_max_age_minutes(policy)

    def test_quota_config_accepts_and_returns_threshold(self):
        cfg = xs.quota_config(POLICY)
        self.assertEqual(cfg["snapshot_max_age_minutes"], 60)
        policy = copy.deepcopy(POLICY)
        policy["routing"]["quota"]["snapshot_max_age_minutes"] = 45
        self.assertEqual(xs.quota_config(policy)["snapshot_max_age_minutes"], 45)


class CodexLimitsOrigin(Slice1Base):
    def write_local_session(self, home, lines, name="rollout-2026-11-13T13-00-00-aaaa.jsonl"):
        session_dir = os.path.join(home, ".codex", "sessions", "2026", "11", "13")
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, name), "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_local_sessions_win_over_fresh_snapshot(self):
        home = tempfile.mkdtemp(prefix="m23-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.write_local_session(home, [rate_line(used=25.0)])
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 60)
        with mock.patch.object(xs, "HOME", home):
            found, origin = xs._codex_limits(POLICY)
        self.assertEqual(origin, "local")
        self.assertEqual(found["codex"]["windows"]["seven_day"]["used"], 0.25)

    def test_snapshot_fallback_when_no_local_sessions(self):
        home = tempfile.mkdtemp(prefix="m23-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 600)
        with mock.patch.object(xs, "HOME", home):
            found, origin = xs._codex_limits(POLICY)
        self.assertEqual(origin, "snapshot")
        self.assertEqual(found["codex"]["windows"]["seven_day"]["used"], 0.05)
        # Age is recomputed at read time from observed_at (13:07:00Z after
        # minute truncation) to NOW_FIXED (13:20:00Z) = 13.0 min, not frozen
        # at sync time.
        self.assertEqual(found["codex"]["age_minutes"], 13.0)

    def test_snapshot_fallback_when_local_sessions_carry_no_rate_limits(self):
        # The VM case: rollout files exist but no rate_limits payload yet.
        home = tempfile.mkdtemp(prefix="m23-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.write_local_session(home, ['{"type":"event_msg","payload":{"type":"text"}}'])
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 60)
        with mock.patch.object(xs, "HOME", home):
            found, origin = xs._codex_limits(POLICY)
        self.assertEqual(origin, "snapshot")
        self.assertEqual(found["codex"]["windows"]["seven_day"]["used"], 0.05)

    def test_stale_snapshot_supplies_nothing(self):
        home = tempfile.mkdtemp(prefix="m23-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 61 * 60)
        with mock.patch.object(xs, "HOME", home):
            found, origin = xs._codex_limits(POLICY)
        self.assertEqual(found, {})
        self.assertEqual(origin, "snapshot-stale")

    def test_no_local_no_snapshot_is_none(self):
        home = tempfile.mkdtemp(prefix="m23-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with mock.patch.object(xs, "HOME", home):
            self.assertEqual(xs._codex_limits(POLICY), ({}, "none"))
            self.assertEqual(xs.codex_limits(POLICY), {})

    def test_codex_sessions_disabled_ignores_snapshot(self):
        home = tempfile.mkdtemp(prefix="m23-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 60)
        policy = copy.deepcopy(POLICY)
        policy["local_sources"]["codex_sessions"] = False
        with mock.patch.object(xs, "HOME", home):
            self.assertEqual(xs._codex_limits(policy), ({}, "none"))

    def test_probe_codex_carries_source_and_snapshot(self):
        home = tempfile.mkdtemp(prefix="m23-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        self.write_snapshot(limits, NOW_FIXED - 600)
        with mock.patch.object(xs, "HOME", home), \
                mock.patch.object(xs, "find_codex", return_value=(None, None)), \
                mock.patch.object(xs, "codex_models", return_value=([], "none")), \
                mock.patch.object(xs, "codex_plan", return_value="plus"), \
                mock.patch.object(xs, "codex_config_model", return_value=None):
            probe = xs.probe_codex(POLICY)
        self.assertEqual(probe["limits_source"], "snapshot")
        self.assertEqual(probe["limits"]["codex"]["windows"]["seven_day"]["used"], 0.05)
        self.assertEqual(probe["limits_snapshot"]["source_host"], "gocc")
        self.assertEqual(probe["limits_snapshot"]["age_minutes"], 10.0)
        self.assertFalse(probe["limits_snapshot"]["stale"])


class QuotaProbeSnapshot(Slice1Base):
    def state(self, limits, source, snapshot):
        st = copy.deepcopy(BASE)
        st["codex"]["limits"] = limits
        st["codex"]["limits_source"] = source
        if snapshot is not None:
            st["codex"]["limits_snapshot"] = snapshot
        return st

    def test_builtin_remaining_from_fresh_snapshot(self):
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        snapshot = {"source_host": "gocc", "synced_at": NOW_FIXED - 600,
                    "synced_at_iso": "2026-11-13T13:10", "age_minutes": 10.0,
                    "stale": False, "stale_threshold_minutes": 60}
        st = self.state(limits, "snapshot", snapshot)
        remaining, error, text = xs.builtin_quota_remaining("codex", st)
        self.assertEqual(remaining, 0.95)
        self.assertIsNone(error)
        self.assertIn("synced snapshot from gocc", text)
        self.assertIn("age 10.0 min", text)

    def test_builtin_stale_snapshot_is_unknown_not_zero(self):
        snapshot = {"source_host": "gocc", "synced_at": NOW_FIXED - 61 * 60,
                    "synced_at_iso": "2026-11-13T12:19", "age_minutes": 61.0,
                    "stale": True, "stale_threshold_minutes": 60}
        st = self.state({}, "snapshot-stale", snapshot)
        remaining, error, text = xs.builtin_quota_remaining("codex", st)
        self.assertIsNone(remaining)
        self.assertIsNone(error)
        self.assertIn("stale", text)
        self.assertIn("treated as unknown", text)

    def test_probe_row_fresh_snapshot_is_ok(self):
        limits = xs.parse_rate_limits_lines([rate_line(used=5.0)])
        snapshot = {"source_host": "gocc", "synced_at": NOW_FIXED - 600,
                    "synced_at_iso": "2026-11-13T13:10", "age_minutes": 10.0,
                    "stale": False, "stale_threshold_minutes": 60}
        st = self.state(limits, "snapshot", snapshot)
        row = xs.quota_probe_row("codex", xs.quota_config(POLICY), (), st)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["remaining"], 0.95)
        self.assertIn("synced snapshot from gocc", row["source"])

    def test_probe_row_stale_snapshot_reports_stale(self):
        snapshot = {"source_host": "gocc", "synced_at": NOW_FIXED - 61 * 60,
                    "synced_at_iso": "2026-11-13T12:19", "age_minutes": 61.0,
                    "stale": True, "stale_threshold_minutes": 60}
        st = self.state({}, "snapshot-stale", snapshot)
        row = xs.quota_probe_row("codex", xs.quota_config(POLICY), (), st)
        self.assertEqual(row["status"], "unknown")
        self.assertIsNone(row["remaining"])
        self.assertIn("stale", row["detail"])
        self.assertIn("treated as unknown", row["detail"])
        self.assertIn("61.0 min > 60 min", row["detail"])

    def test_probe_row_no_snapshot_keeps_old_unknown_detail(self):
        st = self.state({}, "none", None)
        row = xs.quota_probe_row("codex", xs.quota_config(POLICY), (), st)
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["detail"], "probe state has no usable quota reading for this backend")


class QuotaSyncCommand(Slice1Base):
    def args(self, host="gocc", json_out=False):
        return types.SimpleNamespace(host=host, json=json_out)

    def fake_runner(self, stdout, rc=0, stderr=""):
        seen = []

        def run(cmd):
            seen.append(cmd)
            return rc, stdout, stderr
        run.seen = seen
        return run

    def test_sync_writes_cache_verbatim(self):
        line1 = rate_line(used=5.0, ts="2026-11-13T13:07:18.935Z")
        line2 = rate_line(used=4.0, ts="2026-11-13T13:04:58.861Z")
        runner = self.fake_runner(line1 + "\n" + line2 + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            xs.cmd_quota_sync(self.args(), POLICY, runner=runner)
        cmd = runner.seen[0]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("-F", cmd)
        self.assertIn("gocc", cmd)
        remote = cmd[-1]
        self.assertIn("grep", remote)
        self.assertIn("tail", remote)
        # The only ">" is stderr-to-null: no redirects that write a file.
        self.assertEqual(remote.count(">"), remote.count("2>/dev/null"))
        self.assertNotIn("rm ", remote)
        snapshot = xs.load_codex_snapshot()
        self.assertEqual(snapshot["source_host"], "gocc")
        self.assertEqual(snapshot["synced_at"], NOW_FIXED)
        self.assertEqual(snapshot["raw_lines"], [line1, line2])  # verbatim
        self.assertEqual(snapshot["limits"]["codex"]["windows"]["seven_day"]["used"], 0.05)
        self.assertIn("2 rate_limits line(s) from gocc", out.getvalue())
        self.assertIn("snapshot age: 0.0 min", out.getvalue())

    def test_sync_json_output(self):
        runner = self.fake_runner(rate_line(used=5.0) + "\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            xs.cmd_quota_sync(self.args(json_out=True), POLICY, runner=runner)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["synced_lines"], 1)
        self.assertEqual(payload["snapshot"]["source_host"], "gocc")

    def test_sync_host_env_override(self):
        runner = self.fake_runner(rate_line(used=5.0) + "\n")
        with mock.patch.dict(os.environ, {"OPENSWARM_QUOTA_SYNC_HOST": "gocc-alt"}):
            xs.cmd_quota_sync(types.SimpleNamespace(host=None, json=False), POLICY, runner=runner)
        self.assertIn("gocc-alt", runner.seen[0])
        self.assertEqual(xs.load_codex_snapshot()["source_host"], "gocc-alt")

    def test_sync_ssh_failure_leaves_cache_untouched(self):
        sentinel = snapshot_dict(xs.parse_rate_limits_lines([rate_line(used=5.0)]),
                                 NOW_FIXED - 600)
        xs.jsave(xs.CODEX_SNAPSHOT, sentinel)
        runner = self.fake_runner("", rc=255, stderr="ssh: connect to host gocc port 22: Connection refused")
        with self.assertRaises(SystemExit):
            xs.cmd_quota_sync(self.args(), POLICY, runner=runner)
        self.assertEqual(xs.load_codex_snapshot(), sentinel)

    def test_sync_no_lines_leaves_cache_untouched(self):
        sentinel = snapshot_dict(xs.parse_rate_limits_lines([rate_line(used=5.0)]),
                                 NOW_FIXED - 600)
        xs.jsave(xs.CODEX_SNAPSHOT, sentinel)
        runner = self.fake_runner("nothing matching here\n")
        with self.assertRaises(SystemExit):
            xs.cmd_quota_sync(self.args(), POLICY, runner=runner)
        self.assertEqual(xs.load_codex_snapshot(), sentinel)

    def test_sync_refused_when_codex_sessions_disabled(self):
        policy = copy.deepcopy(POLICY)
        policy["local_sources"]["codex_sessions"] = False
        runner = self.fake_runner(rate_line(used=5.0) + "\n")
        with self.assertRaises(SystemExit):
            xs.cmd_quota_sync(self.args(), policy, runner=runner)
        self.assertIsNone(xs.load_codex_snapshot())
        self.assertEqual(runner.seen, [])  # never even dialled out

    def test_fetch_splits_lines_and_reports_ssh_error(self):
        runner = self.fake_runner("ok\n", rc=1, stderr="boom")
        lines, error = xs.fetch_gocc_rate_limits("gocc", runner=runner)
        self.assertEqual(lines, [])
        self.assertIn("exit 1", error)
        self.assertIn("boom", error)


class RoutingSnapshotRecord(Slice1Base):
    def setUp(self):
        super().setUp()
        self._pi_bin_patch = mock.patch.object(xs, "pi_bin", return_value=PI_ISOLATED)
        self._pi_bin_patch.start()
        self.addCleanup(self._pi_bin_patch.stop)

    def state_with_snapshot(self, codex_used=0.05, stale=False, age=10.0):
        st = copy.deepcopy(BASE)
        st["pi"] = {"binary": PI_ISOLATED, "provider": "minimax-cn",
                    "default_model": "MiniMax-M3.1-Flash",
                    "quota": {"remaining": 0.86, "unit": "fraction"}}
        limits = xs.parse_rate_limits_lines(
            [rate_line(used=codex_used * 100, ts="2026-11-13T13:07:18.935Z")])
        st["codex"]["limits"] = {} if stale else limits
        st["codex"]["limits_source"] = "snapshot-stale" if stale else "snapshot"
        st["codex"]["limits_snapshot"] = {
            "source_host": "gocc", "synced_at": NOW_FIXED - int(age * 60),
            "synced_at_iso": "2026-11-13T13:10", "age_minutes": age,
            "stale": stale, "stale_threshold_minutes": 60}
        return st

    def test_fresh_snapshot_codex_remaining_on_record(self):
        st = self.state_with_snapshot(codex_used=0.05)
        decision = xs.route({"type": "impl", "difficulty": 3}, st,
                            quota_policy(["codex", "pi"], threshold=0.5))
        self.assertTrue(decision["ok"], decision)
        decision, info = xs.apply_quota_routing(
            {"type": "impl", "difficulty": 3}, st,
            quota_policy(["codex", "pi"], threshold=0.5), decision)
        self.assertEqual(decision["backend"], "codex")
        self.assertFalse(info["applied"])
        self.assertEqual(info["quota_before"], {"codex": 0.95})
        self.assertEqual(info["quota_snapshot"]["source_host"], "gocc")
        self.assertEqual(info["quota_snapshot"]["synced_at"], NOW_FIXED - 600)
        self.assertFalse(info["quota_snapshot"]["stale"])

    def test_stale_snapshot_codex_unknown_falls_back(self):
        st = self.state_with_snapshot(stale=True, age=61.0)
        decision = xs.route({"type": "impl", "difficulty": 3}, st,
                            quota_policy(["codex", "pi"], threshold=0.5))
        self.assertTrue(decision["ok"], decision)
        decision, info = xs.apply_quota_routing(
            {"type": "impl", "difficulty": 3}, st,
            quota_policy(["codex", "pi"], threshold=0.5), decision)
        # Old behaviour: kept the default pick, codex recorded as unknown.
        self.assertFalse(info["applied"])
        self.assertEqual(info["quota_before"], {"codex": None})
        self.assertIn("unknown", info["reason"])
        self.assertTrue(info["quota_snapshot"]["stale"])
        self.assertEqual(info["quota_snapshot"]["age_minutes"], 61.0)

    def test_explicit_backend_still_wins_with_snapshot_recorded(self):
        st = self.state_with_snapshot(codex_used=0.05)
        decision = xs.route({"type": "impl", "difficulty": 3}, st,
                            quota_policy(["codex", "pi"], threshold=0.5))
        decision, info = xs.apply_quota_routing(
            {"type": "impl", "difficulty": 3, "backend": "pi"}, st,
            quota_policy(["codex", "pi"], threshold=0.5), decision)
        self.assertFalse(info["applied"])
        self.assertIn("explicit --backend wins", info["reason"])
        self.assertEqual(info["quota_snapshot"]["source_host"], "gocc")

    def test_no_snapshot_leaves_record_shape_unchanged(self):
        st = copy.deepcopy(BASE)
        st["pi"] = {"binary": PI_ISOLATED, "provider": "minimax-cn",
                    "default_model": "MiniMax-M3.1-Flash",
                    "quota": {"remaining": 0.86, "unit": "fraction"}}
        decision = xs.route({"type": "impl", "difficulty": 3}, st,
                            quota_policy(["codex", "pi"], threshold=0.5))
        decision, info = xs.apply_quota_routing(
            {"type": "impl", "difficulty": 3}, st,
            quota_policy(["codex", "pi"], threshold=0.5), decision)
        self.assertNotIn("quota_snapshot", info)


if __name__ == "__main__":
    unittest.main()

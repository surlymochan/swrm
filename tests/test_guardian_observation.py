"""Guardian observation (public issue #216): observe-only fleet health.

The guardian normalises *existing* machine-readable openswarm outputs into
one versioned health snapshot and persists it atomically. These tests drive
the real module and the real CLI: deterioration, unreachable hosts, crashed
lanes, 429/quota exhaustion, stale and unsupported inputs, the durable
manual drain, and a controller restart that reads the state back.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

HERE = pathlib.Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
sys.path.insert(0, str(BIN))

import guardian  # noqa: E402
from guardian import observation  # noqa: E402

CLI = BIN / "swrm-guardian"


def snapshot_payload(lanes=None, providers=None, config_valid=True):
    return {"lanes": lanes if lanes is not None else [], "active_runs": [],
            "providers": providers if providers is not None else {},
            "backends": {}, "lane_config_valid": config_valid, "lane_config_error": None}


def lane(name="C1", state="idle", backend="codex", provider="minimax-cn", error=None):
    return {"name": name, "state": state, "backend": backend, "provider": provider,
            "model": "model-x", "pid": 4242, "supervisor_pid": 4241, "quota_reset_at": None,
            "current_run": None, "run_state": None, "provider_note": None,
            "uptime_seconds": 12.0, "last_error": error}


def quota_payload(rows, threshold=5):
    return {"quota_routing_enabled": True, "threshold": threshold, "backends": rows}


def quota_row(backend="codex", remaining=42, status="ok", detail="remaining 42 is above threshold 5",
              source="local"):
    return {"backend": backend, "source": source, "remaining": remaining,
            "status": status, "detail": detail}


def health_payload(hosts=None, runs=None, lanes=None):
    payload = {"summary": {"alive": len(runs or []), "dead": 0, "zombie": 0, "unknown": 0},
               "finished_runs": 0, "stall_minutes": 10, "lane_config_valid": True,
               "lane_config_error": None}
    if hosts is not None:
        payload["hosts"] = hosts
    if runs is not None:
        payload["runs"] = runs
    if lanes is not None:
        payload["lanes"] = lanes
    return payload


def resource_payload(hosts):
    return {"hosts": hosts}


class GuardianObservationTests(unittest.TestCase):
    """The guardian turns existing outputs into one honest verdict."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.state = self.root / "state"

    # helpers ------------------------------------------------------------
    def write(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def observe(self, **inputs):
        return guardian.observe(inputs, state_dir=str(self.state), persist=False)

    def run_cli(self, *args, **kwargs):
        argv = [sys.executable, str(CLI), *args]
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=30, **kwargs)

    # healthy baseline ----------------------------------------------------
    def test_healthy_snapshot_and_quota_read_healthy(self):
        snapshot = self.write("snapshot.json", snapshot_payload(
            lanes=[lane()], providers={"codex": "minimax-cn"}))
        quota = self.write("quota.json", quota_payload([quota_row()]))
        result = self.observe(snapshot=snapshot, quota=quota)
        self.assertEqual(result["overall"]["status"], "healthy")
        self.assertEqual(result["dimensions"]["snapshot"]["lanes"]["status"], "healthy")
        self.assertEqual(result["dimensions"]["quota"]["status"], "healthy")
        self.assertEqual(result["manual_drain"]["active"], False)
        self.assertNotEqual(result["overall"]["confidence"], "none")

    def test_confidence_and_freshness_are_reported_per_observation(self):
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane()]))
        result = self.observe(snapshot=snapshot)
        records = {record["kind"]: record for record in result["coverage"]["inputs"]}
        self.assertEqual(records["snapshot"]["freshness"], "fresh")
        self.assertEqual(records["snapshot"]["timestamp_source"], "file_mtime")
        self.assertIn(records["snapshot"]["confidence"], ("medium", "high"))
        self.assertEqual(records["quota"]["status"], "not_provided")
        self.assertEqual(result["coverage"]["not_provided"], ["health", "probe", "quota", "resources"])

    # deterioration -------------------------------------------------------
    def test_crashed_lane_is_unhealthy(self):
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane(state="crashed")]))
        result = self.observe(snapshot=snapshot)
        lanes = result["dimensions"]["snapshot"]["lanes"]
        self.assertEqual(lanes["status"], "unhealthy")
        self.assertEqual(lanes["items"][0]["name"], "C1")
        self.assertEqual(lanes["items"][0]["status"], "unhealthy")
        self.assertEqual(result["overall"]["status"], "unhealthy")
        self.assertIn("lane_crashed", [signal["kind"] for signal in result["signals"]])

    def test_unreachable_host_is_unhealthy(self):
        health = self.write("health.json", health_payload(hosts=[
            {"name": "alpha", "reachable": False, "paused": False, "status": "unreachable",
             "health": None, "error": "ssh: connect timed out"},
            {"name": "beta", "reachable": True, "paused": False, "status": "reachable", "health": {}}]))
        result = self.observe(health=health)
        hosts = result["dimensions"]["health"]["hosts"]
        self.assertEqual(hosts["status"], "unhealthy")
        by_name = {item["name"]: item for item in hosts["items"]}
        self.assertEqual(by_name["alpha"]["status"], "unhealthy")
        self.assertEqual(by_name["beta"]["status"], "healthy")
        self.assertEqual(result["overall"]["status"], "unhealthy")

    def test_memory_disk_and_network_deterioration_is_unhealthy(self):
        resources = self.write("resources.json", resource_payload([
            {"name": "worker-1", "memory": {"used_pct": 96.0}, "disk": {"free_pct": 3.0},
             "network": {"ok": False, "error": "connection refused"}},
            {"name": "worker-2", "memory": {"used_pct": 42.0}, "disk": {"free_pct": 61.0},
             "network": {"ok": True, "latency_ms": 30}}]))
        result = self.observe(resources=resources, require_fleet=False)
        block = result["dimensions"]["resources"]
        self.assertEqual(block["status"], "unhealthy")
        by_name = {item["name"]: item for item in block["items"]}
        self.assertEqual(by_name["worker-1"]["status"], "unhealthy")
        self.assertEqual(by_name["worker-2"]["status"], "healthy")
        self.assertEqual(by_name["worker-1"]["metrics"]["memory"]["status"], "unhealthy")
        self.assertEqual(by_name["worker-1"]["metrics"]["disk"]["status"], "unhealthy")
        self.assertEqual(by_name["worker-1"]["metrics"]["network"]["status"], "unhealthy")

    def test_healthy_resource_metrics_stay_healthy(self):
        resources = self.write("resources.json", resource_payload([
            {"name": "worker-1", "memory": {"used_pct": 30.0}, "disk": {"free_pct": 55.0},
             "network": {"ok": True}}]))
        result = self.observe(resources=resources, require_fleet=False)
        self.assertEqual(result["dimensions"]["resources"]["status"], "healthy")

    def test_resource_thresholds_are_configurable(self):
        resources = self.write("resources.json", resource_payload([
            {"name": "worker-1", "memory": {"used_pct": 85.0}, "disk": {"free_pct": 55.0},
             "network": {"ok": True}}]))
        default = self.observe(resources=resources, require_fleet=False)
        self.assertEqual(default["dimensions"]["resources"]["status"], "degraded")
        strict = observation.load_thresholds(overrides={"memory_warn_pct": 60.0,
                                                        "memory_critical_pct": 70.0})
        tuned = guardian.observe({"resources": resources}, thresholds=strict,
                                 state_dir=str(self.state), persist=False, require_fleet=False)
        self.assertEqual(tuned["dimensions"]["resources"]["status"], "unhealthy")
        self.assertEqual(tuned["thresholds"]["memory_warn_pct"], 60.0)

    # provider failure ----------------------------------------------------
    def test_provider_429_is_reported_as_a_provider_failure(self):
        snapshot = self.write("snapshot.json", snapshot_payload(
            lanes=[lane(name="A1", state="busy", backend="agy", provider="official",
                        error="HTTP 429 Too Many Requests")],
            providers={"agy": "official"}))
        result = self.observe(snapshot=snapshot)
        providers = result["dimensions"]["snapshot"]["providers"]
        item = providers["items"][0]
        self.assertEqual(item["name"], "official")
        self.assertEqual(item["status"], "unhealthy")
        self.assertIn("rate_limited", item["signals"])
        self.assertIn("429", item["detail"])
        self.assertEqual(result["overall"]["status"], "unhealthy")

    def test_quota_exhaustion_is_unhealthy_and_marks_the_provider(self):
        snapshot = self.write("snapshot.json", snapshot_payload(
            lanes=[lane()], providers={"codex": "minimax-cn"}))
        quota = self.write("quota.json", quota_payload([
            quota_row(backend="codex", remaining=0, status="low",
                      detail="remaining 0 is at or below threshold 5")]))
        result = self.observe(snapshot=snapshot, quota=quota)
        self.assertEqual(result["dimensions"]["quota"]["items"][0]["status"], "unhealthy")
        self.assertIn("quota_exhausted", result["dimensions"]["quota"]["items"][0]["signals"])
        provider = result["dimensions"]["snapshot"]["providers"]["items"][0]
        self.assertEqual(provider["status"], "unhealthy")
        self.assertEqual(result["overall"]["status"], "unhealthy")

    def test_quota_read_error_is_unhealthy_and_unknown_is_not_healthy(self):
        quota = self.write("quota.json", quota_payload([
            quota_row(backend="codex", remaining=None, status="error", detail="read failed")]))
        result = self.observe(quota=quota, require_fleet=False)
        self.assertEqual(result["dimensions"]["quota"]["status"], "unhealthy")

        quota = self.write("quota2.json", quota_payload([
            quota_row(backend="codex", remaining=None, status="unknown",
                      detail="source reports no usable quota")]))
        result = self.observe(quota=quota, require_fleet=False)
        self.assertEqual(result["dimensions"]["quota"]["status"], "unknown")
        self.assertNotEqual(result["overall"]["status"], "healthy")

    def test_unmetered_backends_do_not_hide_the_metered_ones(self):
        quota = self.write("quota.json", quota_payload([
            quota_row(backend="codex", remaining=42, status="ok"),
            quota_row(backend="cursor", remaining=None, status="unknown",
                      detail="no quota source configured; routing cannot judge this backend",
                      source="none (unknown)")]))
        result = self.observe(quota=quota, require_fleet=False)
        block = result["dimensions"]["quota"]
        self.assertEqual(block["status"], "healthy")
        self.assertEqual([item["backend"] for item in block["unmetered"]], ["cursor"])

    # unknown inputs ------------------------------------------------------
    def test_stale_quota_is_unknown_and_never_healthy(self):
        quota = self.write("quota.json", quota_payload([quota_row()]))
        stale = time.time() - 7200
        os.utime(quota, (stale, stale))
        result = self.observe(quota=quota, require_fleet=False)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["quota"]
        self.assertEqual(record["freshness"], "stale")
        self.assertEqual(record["confidence"], "low")
        self.assertEqual(result["dimensions"]["quota"]["status"], "unknown")
        self.assertEqual(result["overall"]["status"], "unknown")
        self.assertNotEqual(result["overall"]["status"], "healthy")

    def test_unsupported_quota_input_is_unknown(self):
        quota = self.write("quota.json", {"something_else": {"rows": []}})
        result = self.observe(quota=quota, require_fleet=False)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["quota"]
        self.assertEqual(record["status"], "unsupported")
        self.assertEqual(result["dimensions"]["quota"]["status"], "unknown")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_missing_input_is_unknown(self):
        result = self.observe(quota=str(self.root / "absent.json"), require_fleet=False)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["quota"]
        self.assertEqual(record["status"], "missing")
        self.assertEqual(result["dimensions"]["quota"]["status"], "unknown")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_unreadable_json_is_unknown(self):
        broken = self.root / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        result = self.observe(snapshot=str(broken), require_fleet=False)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["snapshot"]
        self.assertEqual(record["status"], "invalid")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_unknown_lane_state_is_not_healthy(self):
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane(state="wobbling")]))
        result = self.observe(snapshot=snapshot)
        self.assertEqual(result["dimensions"]["snapshot"]["lanes"]["status"], "unknown")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_an_undated_input_is_unknown_because_its_age_cannot_be_bounded(self):
        payload = snapshot_payload(lanes=[lane()])
        result = guardian.observe({"snapshot": payload}, state_dir=str(self.state), persist=False)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["snapshot"]
        self.assertEqual(record["timestamp_source"], "ingest")
        self.assertEqual(record["freshness"], "unknown")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_a_payload_timestamp_gives_high_confidence(self):
        snapshot = self.write("snapshot.json", dict(snapshot_payload(lanes=[lane()]),
                                                    observed_at=time.time()))
        result = self.observe(snapshot=snapshot)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["snapshot"]
        self.assertEqual(record["timestamp_source"], "payload")
        self.assertEqual(record["confidence"], "high")

    def test_a_required_input_that_is_missing_makes_the_verdict_unknown(self):
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane()]))
        result = guardian.observe({"snapshot": snapshot}, state_dir=str(self.state), persist=False,
                                  require=["quota"])
        self.assertEqual(result["coverage"]["missing_required"], ["quota"])
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_a_stale_drain_scope_does_not_hide_an_unknown(self):
        health = self.write("health.json", health_payload(hosts=[
            {"name": "alpha", "reachable": False, "status": "unreachable", "error": "boom"}]))
        stale = time.time() - 7200
        os.utime(health, (stale, stale))
        guardian.drain(str(self.state), "alpha is being rebuilt", host="alpha")
        result = self.observe(health=health)
        self.assertEqual(result["dimensions"]["health"]["hosts"]["status"], "unknown")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_probe_with_a_stale_reading_is_unknown(self):
        probe = self.write("probe.json", {"generated_at": time.time(), "claude": {
            "plan": "max", "provider": "official", "five_hour": {"used": 0.1},
            "seven_day": {"used": 0.2}, "source": "cache", "age_minutes": 900.0, "stale": True}})
        result = self.observe(probe=probe, require_fleet=False)
        self.assertEqual(result["dimensions"]["probe"]["items"][0]["status"], "unknown")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_probe_without_quota_evidence_does_not_claim_health(self):
        probe = self.write("probe.json", {"generated_at": time.time(),
                                          "cursor": {"binary": "cursor", "default_model": "m"}})
        result = self.observe(probe=probe, require_fleet=False)
        block = result["dimensions"]["probe"]
        self.assertEqual(block["status"], "unknown")
        self.assertIn("cursor", block["unobserved"])

    # manual drain --------------------------------------------------------
    def test_manual_drain_is_durable_and_survives_a_restart(self):
        record = guardian.drain(str(self.state), "host under maintenance", by="operator")
        self.assertTrue(record["active"])
        self.assertEqual(record["scope"], "fleet")
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane()]))
        result = self.observe(snapshot=snapshot)
        self.assertEqual(result["overall"]["status"], "drained")
        self.assertEqual(result["overall"]["transient_status"], "healthy")
        self.assertTrue(result["manual_drain"]["active"])
        # a restart is a fresh process reading the same state directory
        restarted = guardian.observe({"snapshot": snapshot}, state_dir=str(self.state),
                                     persist=False)
        self.assertTrue(restarted["manual_drain"]["active"])
        self.assertEqual(restarted["overall"]["status"], "drained")

    def test_observing_a_healthy_fleet_never_clears_the_manual_drain(self):
        guardian.drain(str(self.state), "waiting for a window")
        drain_path = self.state / "drain.json"
        before = drain_path.read_text(encoding="utf-8")
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane()]))
        result = self.observe(snapshot=snapshot)
        self.assertEqual(result["overall"]["status"], "drained")
        self.assertTrue(result["manual_drain"]["active"])
        self.assertEqual(drain_path.read_text(encoding="utf-8"), before)

    def test_undrain_clears_the_drain_and_the_next_observation_is_healthy(self):
        guardian.drain(str(self.state), "waiting for a window")
        cleared = guardian.undrain(str(self.state))
        self.assertFalse(cleared["active"])
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane()]))
        result = self.observe(snapshot=snapshot)
        self.assertEqual(result["overall"]["status"], "healthy")
        self.assertFalse(result["manual_drain"]["active"])

    def test_host_scoped_drain_only_drains_that_host(self):
        health = self.write("health.json", health_payload(hosts=[
            {"name": "alpha", "reachable": False, "status": "unreachable", "error": "boom"},
            {"name": "beta", "reachable": True, "status": "reachable"}]))
        guardian.drain(str(self.state), "alpha is being rebuilt", host="alpha")
        result = self.observe(health=health)
        by_name = {item["name"]: item for item in result["dimensions"]["health"]["hosts"]["items"]}
        self.assertEqual(by_name["alpha"]["status"], "drained")
        self.assertEqual(by_name["beta"]["status"], "healthy")
        self.assertEqual(result["overall"]["status"], "healthy")

    # persistence ---------------------------------------------------------
    def test_snapshot_is_persisted_atomically_and_readable_after_restart(self):
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane(state="crashed")]))
        result = guardian.observe({"snapshot": snapshot}, state_dir=str(self.state))
        path = self.state / "health.json"
        self.assertTrue(path.exists())
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema"], guardian.SCHEMA)
        self.assertEqual(stored["schema_version"], guardian.SCHEMA_VERSION)
        self.assertEqual(stored["overall"]["status"], result["overall"]["status"])
        # no half-written temporary file is left behind
        self.assertEqual([p.name for p in self.state.iterdir() if p.name.startswith(".")], [])

        # a restarted controller reads the state back
        reloaded = guardian.load_snapshot(str(self.state))
        self.assertEqual(reloaded["overall"]["status"], "unhealthy")
        self.assertEqual(reloaded["dimensions"]["snapshot"]["lanes"]["items"][0]["status"], "unhealthy")

    def test_corrupt_snapshot_reports_a_clear_error_instead_of_crashing(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "health.json").write_text("{ half written", encoding="utf-8")
        with self.assertRaises(guardian.GuardianError) as caught:
            guardian.load_snapshot(str(self.state))
        self.assertIn("corrupt", str(caught.exception))

    def test_snapshot_with_an_unsupported_version_is_refused(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "health.json").write_text(json.dumps(
            {"schema": guardian.SCHEMA, "schema_version": 99}), encoding="utf-8")
        with self.assertRaises(guardian.GuardianError) as caught:
            guardian.load_snapshot(str(self.state))
        self.assertIn("not supported", str(caught.exception))

    def test_missing_snapshot_state_is_a_clear_error(self):
        with self.assertRaises(guardian.GuardianError):
            guardian.load_snapshot(str(self.root / "nowhere"))

    # bounded inputs ------------------------------------------------------
    def test_oversized_input_is_rejected_as_unusable(self):
        big = self.root / "big.json"
        big.write_text(json.dumps({"lanes": [], "pad": "x" * 5000}), encoding="utf-8")
        thresholds = observation.load_thresholds(overrides={"max_input_bytes": 1024})
        result = guardian.observe({"snapshot": str(big)}, thresholds=thresholds,
                                  state_dir=str(self.state), persist=False)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["snapshot"]
        self.assertEqual(record["status"], "too_large")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_deeply_nested_input_is_rejected_as_unusable(self):
        deep = self.root / "deep.json"
        payload = {"lanes": []}
        node = payload
        for _ in range(40):
            node["nested"] = {}
            node = node["nested"]
        deep.write_text(json.dumps(payload), encoding="utf-8")
        thresholds = observation.load_thresholds(overrides={"max_depth": 8})
        result = guardian.observe({"snapshot": str(deep)}, thresholds=thresholds,
                                  state_dir=str(self.state), persist=False)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["snapshot"]
        self.assertEqual(record["status"], "invalid")
        self.assertEqual(result["overall"]["status"], "unknown")

    def test_non_finite_numbers_are_rejected(self):
        broken = self.root / "nan.json"
        broken.write_text('{"lanes": [], "value": NaN}', encoding="utf-8")
        result = self.observe(snapshot=str(broken), require_fleet=False)
        record = {item["kind"]: item for item in result["coverage"]["inputs"]}["snapshot"]
        self.assertEqual(record["status"], "invalid")

    # what the guardian must never do -------------------------------------
    def test_guardian_never_shells_out_and_never_reports_capacity(self):
        source = (BIN / "guardian" / "observation.py").read_text(encoding="utf-8")
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)
        snapshot = self.write("snapshot.json", snapshot_payload(
            lanes=[lane(state="idle")], providers={"codex": "minimax-cn"}))
        result = self.observe(snapshot=snapshot)
        self.assertNotIn("free_slots", json.dumps(result))
        self.assertNotIn("capacity", json.dumps(result))
        self.assertNotIn("dispatch", json.dumps(result))

    # the command line ----------------------------------------------------
    def test_cli_observe_persists_and_status_reads_it_back(self):
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane(state="crashed")]))
        observed = self.run_cli("observe", "--snapshot", snapshot, "--state-dir", str(self.state))
        self.assertEqual(observed.returncode, 0, observed.stderr)
        payload = json.loads(observed.stdout)
        self.assertEqual(payload["overall"]["status"], "unhealthy")
        self.assertTrue((self.state / "health.json").exists())

        # a fresh process (a restarted controller) reads the persisted snapshot
        status = self.run_cli("status", "--state-dir", str(self.state))
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["overall"]["status"], "unhealthy")

    def test_cli_drain_and_undrain_round_trip(self):
        drained = self.run_cli("drain", "--reason", "maintenance window",
                               "--state-dir", str(self.state))
        self.assertEqual(drained.returncode, 0, drained.stderr)
        self.assertTrue(json.loads(drained.stdout)["active"])
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane()]))
        observed = self.run_cli("observe", "--snapshot", snapshot, "--state-dir", str(self.state))
        self.assertEqual(json.loads(observed.stdout)["overall"]["status"], "drained")
        cleared = self.run_cli("undrain", "--state-dir", str(self.state))
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertFalse(json.loads(cleared.stdout)["active"])

    def test_cli_reports_a_missing_snapshot_state(self):
        result = self.run_cli("status", "--state-dir", str(self.root / "nowhere"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("no health snapshot", result.stderr)

    def test_cli_fail_on_flag_reflects_the_verdict(self):
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane(state="crashed")]))
        result = self.run_cli("observe", "--snapshot", snapshot, "--state-dir", str(self.state),
                              "--fail-on", "unhealthy")
        self.assertEqual(result.returncode, 2)

    def test_cli_no_persist_writes_nothing(self):
        snapshot = self.write("snapshot.json", snapshot_payload(lanes=[lane()]))
        result = self.run_cli("observe", "--snapshot", snapshot, "--state-dir", str(self.state),
                              "--no-persist")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.state / "health.json").exists())
        self.assertEqual(json.loads(result.stdout)["overall"]["status"], "healthy")

    def test_cli_reads_one_input_from_stdin(self):
        payload = json.dumps(snapshot_payload(lanes=[lane(state="crashed")]))
        result = subprocess.run([sys.executable, str(CLI), "observe", "--snapshot", "-",
                                 "--state-dir", str(self.state), "--observed-at", str(time.time())],
                                input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["overall"]["status"], "unhealthy")


if __name__ == "__main__":
    unittest.main()

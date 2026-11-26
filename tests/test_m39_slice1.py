"""M39 slice 1 (#300): declarative task-graph file format + validator.

The validator runs before any dispatch: every illegal graph class below
must fail with an actionable error and a non-zero exit, a legal graph must
pass, and `graph validate` must be read-only (no ledger, batch, or run
state is written). No backend, no network.
"""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

import _load

xs = _load.load()
ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin" / "openswarm"

VALID_YAML = """\
# comment line
version: 1
defaults:
  backend: codex
  verify: pytest -q
cards:
  - id: setup
    task: Set up the fixture repo
  - id: impl
    backend: ocgo
    depends_on: [setup]
    task: |
      Implement the feature.
      Keep it small.
  - id: check
    depends_on:
      - impl
    verify: pytest tests/test_x.py -q
    task: Verify the feature
"""

VALID_JSON = {
    "version": 1,
    "defaults": {"backend": "codex", "verify": "pytest -q"},
    "cards": [
        {"id": "setup", "task": "Set up the fixture repo"},
        {"id": "impl", "backend": "ocgo", "depends_on": ["setup"],
         "task": "Implement the feature."},
        {"id": "check", "depends_on": ["impl"],
         "verify": "pytest tests/test_x.py -q", "task": "Verify the feature"},
    ],
}


def graph_with(cards, defaults=None, version=1):
    data = {"version": version, "cards": cards}
    if defaults is not None:
        data["defaults"] = defaults
    return data


def card(card_id, **fields):
    entry = {"id": card_id}
    entry.update(fields)
    return entry


class ParseTests(unittest.TestCase):
    def test_yaml_valid_graph_parses(self):
        data = xs.parse_graph_yaml(VALID_YAML)
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["defaults"], {"backend": "codex", "verify": "pytest -q"})
        self.assertEqual([entry["id"] for entry in data["cards"]],
                         ["setup", "impl", "check"])
        self.assertEqual(data["cards"][1]["depends_on"], ["setup"])
        self.assertEqual(data["cards"][2]["depends_on"], ["impl"])
        self.assertEqual(data["cards"][1]["task"],
                         "Implement the feature.\nKeep it small.\n")

    def test_yaml_plain_scalar_may_contain_colon(self):
        data = xs.parse_graph_yaml(
            "version: 1\ncards:\n  - id: a\n    verify: pytest -q\n"
            "    task: Fix bug: handle None\n")
        self.assertEqual(data["cards"][0]["task"], "Fix bug: handle None")

    def test_yaml_quoted_scalar_and_inline_list(self):
        data = xs.parse_graph_yaml(
            'version: 1\ncards:\n  - id: a\n    verify: "pytest -q -k \'fast\'"\n'
            "    depends_on: [b, c]\n")
        self.assertEqual(data["cards"][0]["verify"], "pytest -q -k 'fast'")
        self.assertEqual(data["cards"][0]["depends_on"], ["b", "c"])

    def test_yaml_tabs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "tabs are not supported"):
            xs.parse_graph_yaml("version: 1\ncards:\n\t- id: a\n")

    def test_yaml_duplicate_key_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate key 'version'"):
            xs.parse_graph_yaml("version: 1\nversion: 1\ncards: []\n")

    def test_yaml_empty_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "empty graph file"):
            xs.parse_graph_yaml("# only a comment\n")

    def test_json_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json"
            path.write_text(json.dumps(VALID_JSON), encoding="utf-8")
            self.assertEqual(xs.load_task_graph(str(path)), VALID_JSON)

    def test_yaml_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.yaml"
            path.write_text(VALID_YAML, encoding="utf-8")
            data = xs.load_task_graph(str(path))
            self.assertEqual(data["version"], 1)
            self.assertEqual(len(data["cards"]), 3)

    def test_unsupported_suffix_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.txt"
            path.write_text(VALID_YAML, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unsupported graph file suffix '.txt'"):
                xs.load_task_graph(str(path))

    def test_missing_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot read graph file"):
            xs.load_task_graph("/nonexistent/graph.yaml")

    def test_malformed_json_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "graph.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                xs.load_task_graph(str(path))


class ValidateTests(unittest.TestCase):
    def test_valid_graph_normalizes_defaults_and_order(self):
        graph = xs.validate_task_graph(VALID_JSON)
        self.assertEqual(graph["order"], ["setup", "impl", "check"])
        by_id = {entry["id"]: entry for entry in graph["cards"]}
        self.assertEqual(by_id["setup"]["backend"], "codex")  # from defaults
        self.assertEqual(by_id["setup"]["verify"], "pytest -q")  # from defaults
        self.assertEqual(by_id["impl"]["backend"], "ocgo")  # card wins over defaults
        self.assertEqual(by_id["check"]["verify"], "pytest tests/test_x.py -q")
        self.assertEqual([entry["order"] for entry in graph["cards"]], [0, 1, 2])

    def test_diamond_order_keeps_declaration_order_among_ready_cards(self):
        graph = xs.validate_task_graph(graph_with([
            card("d", verify="pytest -q", depends_on=["b", "c"]),
            card("c", verify="pytest -q", depends_on=["a"]),
            card("b", verify="pytest -q", depends_on=["a"]),
            card("a", verify="pytest -q"),
        ]))
        self.assertEqual(graph["order"], ["a", "c", "b", "d"])

    def test_backend_may_be_omitted_entirely(self):
        graph = xs.validate_task_graph(graph_with(
            [card("a", verify="pytest -q")]))
        self.assertIsNone(graph["cards"][0]["backend"])
        self.assertIsNone(graph["defaults"]["backend"])

    def test_gemini_alias_canonicalizes_to_agy(self):
        graph = xs.validate_task_graph(graph_with(
            [card("a", backend="gemini", verify="pytest -q")]))
        self.assertEqual(graph["cards"][0]["backend"], "agy")

    def test_custom_backend_counts_as_known_when_listed(self):
        graph = xs.validate_task_graph(
            graph_with([card("a", backend="echo", verify="pytest -q")]),
            known_backends=["codex", "echo"])
        self.assertEqual(graph["cards"][0]["backend"], "echo")

    def test_cycle_is_named(self):
        with self.assertRaisesRegex(
                ValueError, r"dependency cycle detected: a -> b -> a"):
            xs.validate_task_graph(graph_with([
                card("a", verify="pytest -q", depends_on=["b"]),
                card("b", verify="pytest -q", depends_on=["a"]),
            ]))

    def test_three_card_cycle_is_named(self):
        with self.assertRaisesRegex(
                ValueError, r"dependency cycle detected: a -> c -> b -> a"):
            xs.validate_task_graph(graph_with([
                card("a", verify="pytest -q", depends_on=["c"]),
                card("b", verify="pytest -q", depends_on=["a"]),
                card("c", verify="pytest -q", depends_on=["b"]),
            ]))

    def test_unknown_dependency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "depends on unknown card 'ghost'"):
            xs.validate_task_graph(graph_with([
                card("a", verify="pytest -q", depends_on=["ghost"]),
            ]))

    def test_missing_acceptance_command_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "has no acceptance command"):
            xs.validate_task_graph(graph_with([card("a", backend="codex")]))

    def test_missing_acceptance_command_can_come_from_defaults(self):
        graph = xs.validate_task_graph(graph_with(
            [card("a", backend="codex")], defaults={"verify": "pytest -q"}))
        self.assertEqual(graph["cards"][0]["verify"], "pytest -q")

    def test_duplicate_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate card id 'a'"):
            xs.validate_task_graph(graph_with([
                card("a", verify="pytest -q"), card("a", verify="pytest -q"),
            ]))

    def test_unknown_backend_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown backend 'nosuchbackend'"):
            xs.validate_task_graph(graph_with([
                card("a", backend="nosuchbackend", verify="pytest -q"),
            ]))

    def test_unknown_defaults_backend_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown backend 'nosuchbackend'"):
            xs.validate_task_graph(graph_with(
                [card("a", verify="pytest -q")],
                defaults={"backend": "nosuchbackend"}))

    def test_self_dependency_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "depends on itself"):
            xs.validate_task_graph(graph_with([
                card("a", verify="pytest -q", depends_on=["a"]),
            ]))

    def test_missing_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing 'id'"):
            xs.validate_task_graph(graph_with([{"verify": "pytest -q"}]))

    def test_bad_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "'id' 'bad id!'"):
            xs.validate_task_graph(graph_with([
                card("bad id!", verify="pytest -q"),
            ]))

    def test_missing_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing 'version'"):
            xs.validate_task_graph({"cards": [card("a", verify="pytest -q")]})

    def test_unsupported_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported graph version 2"):
            xs.validate_task_graph(graph_with(
                [card("a", verify="pytest -q")], version=2))

    def test_unknown_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown top-level field 'bogus'"):
            data = graph_with([card("a", verify="pytest -q")])
            data["bogus"] = 1
            xs.validate_task_graph(data)
        with self.assertRaisesRegex(ValueError, "unknown card field 'bogus'"):
            xs.validate_task_graph(graph_with([
                card("a", verify="pytest -q", bogus=1),
            ]))
        with self.assertRaisesRegex(ValueError, "unknown defaults field 'bogus'"):
            xs.validate_task_graph(graph_with(
                [card("a", verify="pytest -q")], defaults={"bogus": 1}))

    def test_empty_cards_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "'cards' must be a non-empty list"):
            xs.validate_task_graph(graph_with([]))

    def test_depends_on_must_be_a_list(self):
        with self.assertRaisesRegex(ValueError, "'depends_on' must be a list"):
            xs.validate_task_graph(graph_with([
                card("a", verify="pytest -q", depends_on="b"),
            ]))

    def test_empty_verify_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "'verify' must be a non-empty"):
            xs.validate_task_graph(graph_with([card("a", verify="  ")]))

    def test_all_problems_are_reported_at_once(self):
        with self.assertRaisesRegex(ValueError, "duplicate card id 'a'"):
            try:
                xs.validate_task_graph(graph_with([
                    card("a", backend="nosuchbackend", verify="pytest -q"),
                    card("a", verify="pytest -q"),
                ]))
            except ValueError as error:
                self.assertIn("unknown backend 'nosuchbackend'", str(error))
                raise

    def test_topo_sort_matches_batch_semantics(self):
        # Same dependency shape as test_batch.py's M4 case: topo order puts
        # every dependency before its dependents.
        order = xs.topo_sort_graph_cards(
            {"a": [], "b": ["a"], "c": ["b"]}, ["c", "b", "a"])
        self.assertEqual(order, ["a", "b", "c"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="openswarm-graph-")
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.home = self.root / "home"
        self.home.mkdir()

    def write_graph(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *argv):
        env = dict(os.environ)
        env["OPENSWARM_HOME"] = str(self.home)
        return subprocess.run([sys.executable, str(CLI), *argv],
                              capture_output=True, env=env, text=True)

    def test_valid_graph_exits_zero(self):
        path = self.write_graph("good.yaml", VALID_YAML)
        result = self.run_cli("graph", "validate", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("valid:", result.stdout)
        self.assertIn("setup -> impl -> check", result.stdout)

    def test_valid_graph_json_output_parses(self):
        path = self.write_graph("good.json", json.dumps(VALID_JSON))
        result = self.run_cli("graph", "validate", "--json", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        graph = json.loads(result.stdout)
        self.assertEqual(graph["order"], ["setup", "impl", "check"])

    def test_each_illegal_graph_class_exits_nonzero(self):
        cases = {
            "cycle.yaml": "version: 1\ncards:\n"
                          "  - id: a\n    verify: pytest -q\n    depends_on: [b]\n"
                          "  - id: b\n    verify: pytest -q\n    depends_on: [a]\n",
            "unknowndep.yaml": "version: 1\ncards:\n"
                               "  - id: a\n    verify: pytest -q\n    depends_on: [ghost]\n",
            "noverify.yaml": "version: 1\ncards:\n  - id: a\n    backend: codex\n",
            "dupid.yaml": "version: 1\ncards:\n"
                          "  - id: a\n    verify: pytest -q\n"
                          "  - id: a\n    verify: pytest -q\n",
            "badbackend.yaml": "version: 1\ncards:\n"
                               "  - id: a\n    backend: nosuchbackend\n    verify: pytest -q\n",
            "selfdep.yaml": "version: 1\ncards:\n"
                            "  - id: a\n    verify: pytest -q\n    depends_on: [a]\n",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                path = self.write_graph(name, text)
                result = self.run_cli("graph", "validate", str(path))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('"ok": false', result.stderr)

    def test_validate_is_read_only(self):
        path = self.write_graph("good.yaml", VALID_YAML)
        result = self.run_cli("graph", "validate", str(path))
        self.assertEqual(result.returncode, 0, result.stderr)
        # Nothing is created under the isolated home: no ledger, no batch,
        # no run state — validation must not produce a half-executed state.
        self.assertEqual(list(self.home.iterdir()), [])

    def test_custom_backend_via_backends_file(self):
        backends = self.root / "backends.json"
        backends.write_text(json.dumps({"backends": [{
            "id": "echo", "name": "echo", "command": ["echo-cli.py", "{prompt}"],
            "quota_source": "none"}]}), encoding="utf-8")
        path = self.write_graph("custom.yaml",
                                "version: 1\ncards:\n"
                                "  - id: a\n    backend: echo\n    verify: pytest -q\n")
        denied = self.run_cli("graph", "validate", str(path))
        self.assertNotEqual(denied.returncode, 0)
        allowed = self.run_cli("graph", "validate", "--backends-file",
                               str(backends), str(path))
        self.assertEqual(allowed.returncode, 0, allowed.stderr)

    def test_cmd_graph_validate_returns_graph_in_process(self):
        path = self.write_graph("good.yaml", VALID_YAML)
        args = SimpleNamespace(file=str(path), json=False, backends_file=None)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            graph = xs.cmd_graph_validate(args)
        self.assertEqual(graph["order"], ["setup", "impl", "check"])
        self.assertIn("valid:", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()

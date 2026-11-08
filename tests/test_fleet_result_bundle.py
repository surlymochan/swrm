"""Direct tests for bin/fleet-result-bundle.

Until now the script's only coverage was indirect: one happy-path call
from tests/test_hub_bridge.py (via the hub gateway) and one from
tests/test_fleet_capacity.py. Every error branch — bad run id, branch
mismatch, bad commit/base hex, missing repo, rev-parse mismatch,
malformed input — was untested. These tests drive the script directly
as a subprocess with a throwaway git repo, no backends needed.
"""
import base64
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'fleet-result-bundle'


def _git(repo, *args, check=True):
    return subprocess.run(['git', '-C', str(repo), *args],
                          check=check, capture_output=True, text=True)


def _make_repo(root):
    """A repo with a base commit and one result commit on openswarm/run-1."""
    repo = pathlib.Path(root) / 'repo'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'main')
    (repo / 'a').write_text('base\n')
    _git(repo, 'add', 'a')
    _git(repo, '-c', 'user.name=test', '-c', 'user.email=test@test',
         'commit', '-qam', 'base')
    base = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(repo, 'checkout', '-qb', 'openswarm/run-1')
    (repo / 'a').write_text('result\n')
    _git(repo, '-c', 'user.name=test', '-c', 'user.email=test@test',
         'commit', '-qam', 'result')
    commit = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    return repo, base, commit


def _info(run_id='run-1', branch='openswarm/run-1', commit=None, repo=None, base=None):
    return {'id': run_id,
            'result': {'branch': branch, 'commit': commit or 'f' * 40,
                       'workspace': {'repo': str(repo), 'base': base or 'e' * 40}}}


class FleetResultBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='os-bundle-')
        self.addCleanup(self.tmp.cleanup)
        self.repo, self.base, self.commit = _make_repo(self.tmp.name)

    def run_script(self, payload):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run([sys.executable, str(SCRIPT)], input=stdin,
                              text=True, capture_output=True, timeout=120)

    def test_happy_path_returns_decodable_bundle(self):
        proc = self.run_script(_info(repo=self.repo, base=self.base, commit=self.commit))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual((out['branch'], out['commit']),
                         ('openswarm/run-1', self.commit))
        bundle_path = pathlib.Path(self.tmp.name) / 'result.bundle'
        bundle_path.write_bytes(base64.b64decode(out['bundle']))
        heads = _git(self.tmp.name, 'bundle', 'list-heads', str(bundle_path)).stdout
        self.assertIn('openswarm/run-1', heads)
        verify = subprocess.run(['git', '-C', str(self.repo), 'bundle', 'verify',
                                 str(bundle_path)],
                                capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stderr)

    def test_branch_mismatch_rejected(self):
        proc = self.run_script(_info(branch='openswarm/other',
                                     repo=self.repo, base=self.base, commit=self.commit))
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no matching branch', json.loads(proc.stdout)['error'])

    def test_bad_run_id_rejected(self):
        proc = self.run_script(_info(run_id='../escape',
                                     repo=self.repo, base=self.base, commit=self.commit))
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no matching branch', json.loads(proc.stdout)['error'])

    def test_bad_commit_hex_rejected(self):
        proc = self.run_script(_info(commit='not-a-hex-commit',
                                     repo=self.repo, base=self.base))
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no completed commit', json.loads(proc.stdout)['error'])

    def test_recorded_commit_must_match_branch_tip(self):
        proc = self.run_script(_info(commit=self.base,
                                     repo=self.repo, base=self.base))
        self.assertEqual(proc.returncode, 1)
        self.assertIn('does not match', json.loads(proc.stdout)['error'])

    def test_missing_repo_rejected(self):
        missing = pathlib.Path(self.tmp.name) / 'no-such-repo'
        proc = self.run_script(_info(repo=missing, base=self.base, commit=self.commit))
        self.assertEqual(proc.returncode, 1)
        self.assertIn('no completed commit', json.loads(proc.stdout)['error'])

    def test_malformed_input_rejected(self):
        proc = self.run_script('this is not json')
        self.assertEqual(proc.returncode, 1)
        self.assertIn('error', json.loads(proc.stdout))

    def test_missing_result_keys_rejected(self):
        proc = self.run_script({'id': 'run-1'})
        self.assertEqual(proc.returncode, 1)
        self.assertIn('error', json.loads(proc.stdout))


if __name__ == '__main__':
    unittest.main()

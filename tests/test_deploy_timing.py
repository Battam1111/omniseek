"""scripts/deploy_timing.sh is the one source of OmniSeek's deploy health wait (added 2026-10-11).

Both deploy paths (deploy.sh, scripts/deploy_via_git.sh) wait by seconds read from it, and the
sentinel's deploy grace is read from it too, so these tests pin that the three cannot drift apart,
that the wait covers the worst restart (launchd ExitTimeOut), and that neither script still counts
probes or carries its own default.
"""
from __future__ import annotations

import importlib.util
import os
import plistlib
import re
import shutil
import subprocess
import unittest
from pathlib import Path

try:  # `discover -s tests` puts tests/ on sys.path; `-m unittest tests.x` puts the repo root there
    from _repo_only import requires_source_repo
except ImportError:
    from tests._repo_only import requires_source_repo

ROOT = Path(__file__).resolve().parents[1]
TIMING = ROOT / "scripts" / "deploy_timing.sh"
PLIST = ROOT / "scripts" / "com.omniseek.organ.eye-http.plist"
LAUNCHD_DEFAULT_EXIT_TIMEOUT_S = 20


def _sentinel():
    spec = importlib.util.spec_from_file_location("_sentinel_for_timing", ROOT / "scripts" / "sentinel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@requires_source_repo
@unittest.skipIf(os.name == "nt", "POSIX file modes and shell dispatch only")
class DeployTimingTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("bash"):
            self.fail("bash is required to verify the deploy scripts")

    def _source(self, *names: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k != "OMNISEEK_HEALTH_WAIT_S"}
        env.update(env_extra or {})
        echo = " ".join(f"${{{n}}}" for n in names)
        return subprocess.run(
            ["bash", "-c", f'set -euo pipefail; source scripts/deploy_timing.sh; echo "{echo}"'],
            cwd=ROOT, capture_output=True, text=True, env=env, check=False,
        )

    def test_scripts_parse(self):
        for script in ("scripts/deploy_timing.sh", "scripts/deploy_via_git.sh", "deploy.sh"):
            r = subprocess.run(["bash", "-n", script], cwd=ROOT, capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 0, f"{script}: {r.stderr}")

    def test_default_wait_is_the_worst_restart_path(self):
        r = self._source("EYE_EXIT_TIMEOUT_S", "EYE_READY_S", "EYE_PROBE_PERIOD_S", "HEALTH_WAIT_S")
        self.assertEqual(r.returncode, 0, r.stderr)
        exit_timeout, ready, period, wait = map(int, r.stdout.split())
        self.assertEqual(wait, exit_timeout + ready + period)
        self.assertEqual(wait, 95)
        # the drain-cap in-process shutdown ceiling (65 s) fits inside ExitTimeOut
        self.assertGreaterEqual(exit_timeout, 65)
        # and the worst path still keeps the old normal-path rule (2 x 45.7 s)
        self.assertGreaterEqual(wait, 2 * 45.7)

    def test_env_override_and_its_validation(self):
        r = self._source("HEALTH_WAIT_S", env_extra={"OMNISEEK_HEALTH_WAIT_S": "150"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "150"))
        r = self._source("HEALTH_WAIT_S", env_extra={"OMNISEEK_HEALTH_WAIT_S": "90s"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("whole number of seconds", r.stderr)

    def test_python_reader_matches_bash(self):
        names = ["EYE_EXIT_TIMEOUT_S", "EYE_READY_S", "EYE_PROBE_PERIOD_S",
                 "EYE_HEALTH_WAIT_DEFAULT_S", "EYE_ROLLBACK_SLACK_S", "EYE_DEPLOY_DARK_MAX_S"]
        r = self._source(*names)
        self.assertEqual(r.returncode, 0, r.stderr)
        from_bash = dict(zip(names, map(int, r.stdout.split())))
        self.assertEqual(_sentinel().load_deploy_timing(), from_bash)

    def test_sentinel_grace_is_read_from_the_file_and_its_fallback_agrees(self):
        s = _sentinel()
        t = s.load_deploy_timing()
        self.assertEqual(s.EYE_DEPLOY_GRACE_FROM, str(TIMING))
        self.assertEqual(s.EYE_DEPLOY_GRACE_S, t["EYE_DEPLOY_DARK_MAX_S"])
        self.assertEqual(s._OMNISEEK_DEPLOY_GRACE_FALLBACK_S, t["EYE_DEPLOY_DARK_MAX_S"])
        self.assertGreaterEqual(s.EYE_DEPLOY_GRACE_S, 2 * t["EYE_HEALTH_WAIT_DEFAULT_S"])

    def test_sentinel_reader_refuses_anything_but_sums_and_products(self):
        s = _sentinel()
        for expr in ("A-1", "A/2", "__import__('os')", "B+1"):
            with self.assertRaises((ValueError, SyntaxError)):
                s._eval_timing_expr(expr, {"A": 3})
        self.assertEqual(s._eval_timing_expr(" 2*A + 4 ", {"A": 3}), 10)

    def test_plist_exit_timeout_never_exceeds_the_waited_bound(self):
        r = self._source("EYE_EXIT_TIMEOUT_S")
        bound = int(r.stdout)
        plist = plistlib.loads(PLIST.read_bytes())
        self.assertLessEqual(plist.get("ExitTimeOut", LAUNCHD_DEFAULT_EXIT_TIMEOUT_S), bound)

    def test_both_deploy_paths_wait_by_seconds_from_the_shared_file(self):
        for script in ("deploy.sh", "scripts/deploy_via_git.sh"):
            text = (ROOT / script).read_text(encoding="utf-8")
            code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
            self.assertRegex(code, r"(?m)^\s*source scripts/deploy_timing\.sh", script)
            self.assertNotRegex(code, r"OMNISEEK_HEALTH_WAIT_S:-", f"{script} keeps its own default")
            wait = code[code.index("wait_for_eye_build() {"):]
            wait = wait[:wait.index("\n}\n")]
            self.assertNotRegex(wait, r"\bseq\b|for _ in", f"{script} still counts probes")
            self.assertIn("SECONDS", wait, script)
            self.assertRegex(wait, r"elapsed >= HEALTH_WAIT_S", script)

    def test_git_deploy_probe_fits_the_probe_period(self):
        # one probe = ssh ConnectTimeout + curl -m + the sleep after it must stay within EYE_PROBE_PERIOD_S
        text = (ROOT / "scripts" / "deploy_via_git.sh").read_text(encoding="utf-8")
        body = text[text.index("wait_for_eye_build() {"):]
        body = body[:body.index("\n}\n")]
        self.assertNotRegex(body, r"\brs\b", "the probe must not go through rs (6 retries)")
        connect = int(re.search(r"ssh -o ConnectTimeout=(\d+) \"\$\{SSH_OPTS", body).group(1))
        curl = int(re.search(r"curl -fsS -m (\d+)", body).group(1))
        sleep = int(re.search(r"sleep (\d+)", body).group(1))
        period = int(self._source("EYE_PROBE_PERIOD_S").stdout)
        self.assertLessEqual(connect + curl + sleep, period)

    def _run_git_wait(self, ssh_body: str, wait_s: int) -> tuple[subprocess.CompletedProcess, float]:
        import tempfile, time
        text = (ROOT / "scripts" / "deploy_via_git.sh").read_text(encoding="utf-8")
        func = text[text.index("wait_for_eye_build() {"):]
        func = func[:func.index("\n}\n") + 3]
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "ssh"
            fake.write_text("#!/bin/bash\n" + ssh_body + "\n", encoding="utf-8")
            fake.chmod(0o755)
            script = (f"set -euo pipefail\nSSH_OPTS=(-o BatchMode=yes)\nMINI=fake\nHEALTH_WAIT_S={wait_s}\n"
                      "json_field() { printf '%s' \"$1\"; }\n" + func + "wait_for_eye_build abc123\n")
            env = dict(os.environ, PATH=f"{d}:{os.environ.get('PATH', '')}")
            t0 = time.monotonic()
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)
            return r, time.monotonic() - t0

    def test_git_deploy_wait_gives_up_by_the_clock_not_a_count(self):
        r, took = self._run_git_wait("exit 255", wait_s=3)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertGreaterEqual(took, 3 - 1)  # bash SECONDS ticks in whole seconds from any phase
        self.assertIn("waiting up to 3s", r.stdout)
        self.assertIn("RUNTIME BUILD ID MISMATCH: expected abc123 within 3s", r.stderr)
        self.assertIn("healthz down or ssh probe failed", r.stderr)

    def test_git_deploy_wait_returns_when_the_build_answers(self):
        r, _ = self._run_git_wait("printf abc123", wait_s=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"eye up after \d+s")

    def test_timing_file_ships_in_the_deploy_payload(self):
        payload = (ROOT / "scripts" / "deploy_payload.sh").read_text(encoding="utf-8")
        paths = re.search(r"DEPLOY_PATHS=\(([^)]*)\)", payload).group(1).split()
        self.assertIn("scripts", paths)


if __name__ == "__main__":
    unittest.main()

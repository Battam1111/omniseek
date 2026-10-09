"""Cleanup of the app-bundle clones Google Chrome leaves on macOS (2026-10-05).

Fake clone roots in a temp directory, fake lsof / ps data: nothing here runs lsof, ps or getconf,
and nothing touches the machine's real clone directory. The real one was checked by hand on the live host
(dry run, and a run with lsof's names blinded so only the file-identity test could keep a clone).
"""
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from omniseek.core import chrome_clones as cc
from omniseek.core import infra_jobs

NOW = 1_800_000_000.0
HOUR = 3600
INSTALLED = (1, 500)          # (dev, inode) of the installed Chrome executable
OLD_BINARY = (1, 400)         # an executable no longer installed (an old version still running)

LSOF_SAMPLE = "\n".join([
    "p884", "cGoogle Chrome", "fcwd", "D0x1000010", "i2", "n/",
    "ftxt", "D0x1000010", "i11066151",
    "n/private/var/folders/nr/x/X/com.google.Chrome.code_sign_clone/code_sign_clone.RhNiir/"
    "Google Chrome.app.bundle/Contents/MacOS/Google Chrome",
    "p20067", "cGoogle Chrome", "ftxt", "D0x1000010", "i23125435",
    "n/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "f12", "n/dev/null",
    "",
])

PS_SAMPLE = ("  884 Mon Aug  3 22:40:21 2026\n"
             "20067 Mon Oct  5 08:49:07 2026\n"
             "garbage line\n")


class ParseTests(unittest.TestCase):
    def test_lsof_records_carry_pid_command_and_file_identity(self):
        files = cc.parse_lsof(LSOF_SAMPLE)
        self.assertEqual(len(files), 4)
        f = files[1]
        self.assertEqual((f.pid, f.cmd, f.fd), (884, "Google Chrome", "txt"))
        self.assertEqual((f.dev, f.ino), (0x1000010, 11066151))
        self.assertTrue(f.name.endswith("/MacOS/Google Chrome"))
        last = files[3]
        self.assertEqual((last.pid, last.fd, last.dev, last.ino), (20067, "12", None, None))

    def test_ps_start_times(self):
        starts = cc.parse_ps_starts(PS_SAMPLE)
        self.assertEqual(sorted(starts), [884, 20067])
        self.assertEqual(starts[20067] - starts[884],
                         time.mktime((2026, 10, 5, 8, 49, 7, 0, 0, -1))
                         - time.mktime((2026, 8, 3, 22, 40, 21, 0, 0, -1)))

    def test_an_lsof_that_does_not_list_this_process_is_not_trusted(self):
        done = mock.Mock(stdout="p1\ncfoo\nftxt\nn/bin/foo\n")
        with mock.patch.object(cc.subprocess, "run", return_value=done):
            self.assertIsNone(cc.read_lsof())
        done = mock.Mock(stdout="p%d\ncpython\nftxt\nn/bin/python\n" % os.getpid())
        with mock.patch.object(cc.subprocess, "run", return_value=done):
            self.assertEqual(len(cc.read_lsof()), 1)


class FakeRoot:
    """A clone root in a temp dir. ``births`` / ``exes`` stand in for the APFS birth time and the
    identity of each clone's main executable (neither can be set on a plain test filesystem)."""

    def __init__(self):
        # Resolved, like the real root (clone_root() resolves it, and lsof prints resolved names): on
        # macOS mkdtemp() answers /var/folders/..., a symlink to /private/var/folders/..., and the
        # fake lsof names below must match what plan() compares them with.
        self.base = os.path.realpath(tempfile.mkdtemp())
        self.root = os.path.join(self.base, "com.google.Chrome.code_sign_clone")
        os.makedirs(self.root)
        self.births = {}
        self.exes = {}

    def clone(self, name, *, age, exe=INSTALLED):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.join(path, "Google Chrome.app.bundle", "Contents", "MacOS"))
        with open(os.path.join(path, "Google Chrome.app.bundle", "Contents", "Info.plist"), "w"):
            pass
        self.births[name] = NOW - age
        self.exes[name] = {exe}
        return path

    def birth_of(self, path):
        return self.births.get(os.path.basename(path))

    def exe_ids_of(self, path):
        return self.exes.get(os.path.basename(path), set())

    def names(self):
        return sorted(os.listdir(self.root))

    def cleanup(self):
        shutil.rmtree(self.base, ignore_errors=True)


def mapped(pid, path_or_none, fid):
    return cc.OpenFile(pid, "Google Chrome", "txt", fid[0], fid[1], path_or_none or "/elsewhere")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.fr = FakeRoot()
        self.addCleanup(self.fr.cleanup)

    def _plan(self, open_files=(), starts=None):
        p = cc.plan(self.fr.root, list(open_files), now=NOW, starts=starts or {},
                    installed={INSTALLED}, birth_of=self.fr.birth_of, exe_ids_of=self.fr.exe_ids_of)
        return {k: sorted(x["name"] for x in v) for k, v in p.items()}, p

    def test_an_old_unused_clone_is_deleted(self):
        self.fr.clone("code_sign_clone.AAAAAA", age=2 * HOUR)
        got, _ = self._plan()
        self.assertEqual(got["delete"], ["code_sign_clone.AAAAAA"])

    def test_a_clone_with_an_open_file_under_it_is_in_use(self):
        path = self.fr.clone("code_sign_clone.BBBBBB", age=2 * HOUR)
        f = cc.OpenFile(77, "Google Chrome", "txt", 9, 9, os.path.join(path, "Google Chrome.app.bundle"))
        got, p = self._plan([f])
        self.assertEqual(got["in_use"], ["code_sign_clone.BBBBBB"])
        self.assertEqual(p["in_use"][0]["pids"], [77])

    def test_a_clone_born_within_the_hour_is_kept(self):
        self.fr.clone("code_sign_clone.CCCCCC", age=HOUR - 60)
        got, _ = self._plan()
        self.assertEqual(got["too_new"], ["code_sign_clone.CCCCCC"])
        self.assertEqual(got["delete"], [])

    def test_its_maker_holds_a_clone_even_when_lsof_prints_another_name(self):
        """The main executable is a hard link of the running binary: lsof may print it under
        /Applications. The browser that started just before the clone was born still holds it."""
        self.fr.clone("code_sign_clone.DDDDDD", age=2 * HOUR)
        maker_start = NOW - 2 * HOUR - 1
        got, p = self._plan([mapped(10, None, INSTALLED)], starts={10: maker_start})
        self.assertEqual(got["in_use"], ["code_sign_clone.DDDDDD"])
        self.assertEqual(p["in_use"][0]["why"], ["maker"])

    def test_a_browser_that_did_not_make_the_clone_does_not_hold_it(self):
        """Every clone of the installed version shares that executable with every running browser
        of the version; a long-running browser must not keep all leaked clones alive."""
        self.fr.clone("code_sign_clone.EEEEEE", age=2 * HOUR)
        long_running = NOW - 30 * HOUR
        started_after = NOW - HOUR
        got, _ = self._plan([mapped(20, None, INSTALLED), mapped(21, None, INSTALLED)],
                            starts={20: long_running, 21: started_after})
        self.assertEqual(got["delete"], ["code_sign_clone.EEEEEE"])

    def test_an_old_binary_still_running_holds_every_clone_of_it(self):
        """A desktop Chrome left open across an update: its executable survives only in clones."""
        self.fr.clone("code_sign_clone.FFFFFF", age=60 * 24 * HOUR, exe=OLD_BINARY)
        got, p = self._plan([mapped(884, None, OLD_BINARY)], starts={884: NOW - 70 * 24 * HOUR})
        self.assertEqual(got["in_use"], ["code_sign_clone.FFFFFF"])
        self.assertEqual(p["in_use"][0]["why"], ["old binary"])

    def test_names_that_are_not_clones_are_left_alone(self):
        for name in ("code_sign_clone.abc", "code_sign_clone.ABCDEFG", "other",
                     "code_sign_clone.AB-DEF", "xcode_sign_clone.ABCDEF"):
            os.makedirs(os.path.join(self.fr.root, name))
        with open(os.path.join(self.fr.root, "code_sign_clone.GGGGGG"), "w"):
            pass
        self.fr.births["code_sign_clone.GGGGGG"] = NOW - 9 * HOUR
        got, p = self._plan()
        self.assertEqual(got["delete"], [])
        self.assertEqual(len(got["skipped"]), 6)
        whys = {x["name"]: x["why"] for x in p["skipped"]}
        self.assertEqual(whys["code_sign_clone.GGGGGG"], "not a directory")
        self.assertEqual(whys["other"], "name")

    def test_a_symlink_out_of_the_root_is_skipped_and_its_target_survives(self):
        outside = os.path.join(self.fr.base, "precious")
        os.makedirs(outside)
        link = os.path.join(self.fr.root, "code_sign_clone.HHHHHH")
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("cannot create a symlink here: %s" % exc)
        self.fr.births["code_sign_clone.HHHHHH"] = NOW - 9 * HOUR
        res = cc.sweep(root=self.fr.root, open_files=[], starts={}, installed={INSTALLED},
                       now=NOW, birth_of=self.fr.birth_of, exe_ids_of=self.fr.exe_ids_of)
        self.assertEqual(res["deleted"], [])
        self.assertEqual(res["skipped"], ["code_sign_clone.HHHHHH (symlink)"])
        self.assertTrue(os.path.isdir(outside))


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.fr = FakeRoot()
        self.addCleanup(self.fr.cleanup)
        self.fr.clone("code_sign_clone.Old001", age=5 * HOUR)
        self.fr.clone("code_sign_clone.Old002", age=6 * HOUR)
        self.fr.clone("code_sign_clone.New001", age=60)

    def _sweep(self, **kw):
        base = dict(root=self.fr.root, open_files=[], starts={}, installed={INSTALLED}, now=NOW,
                    birth_of=self.fr.birth_of, exe_ids_of=self.fr.exe_ids_of)
        base.update(kw)
        return cc.sweep(**base)

    def test_deletes_the_old_and_keeps_the_new(self):
        with self.assertLogs(cc.log, "INFO") as logs:
            res = self._sweep()
        self.assertEqual(sorted(res["deleted"]), ["code_sign_clone.Old001", "code_sign_clone.Old002"])
        self.assertEqual(self.fr.names(), ["code_sign_clone.New001"])
        lines = [m for m in logs.output if "deleted code_sign_clone.Old00" in m]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("born " in m and "s" in m.rsplit(" in ", 1)[1] for m in lines))

    def test_dry_run_lists_and_deletes_nothing(self):
        res = self._sweep(dry_run=True)
        self.assertEqual(sorted(res["would_delete"]),
                         ["code_sign_clone.Old001", "code_sign_clone.Old002"])
        self.assertEqual(res["too_new"], ["code_sign_clone.New001"])
        self.assertEqual(len(self.fr.names()), 3)

    def test_a_failed_delete_is_logged_and_the_rest_go_on(self):
        calls = []

        def flaky(path):
            calls.append(os.path.basename(path))
            if path.endswith("Old001"):
                raise PermissionError("operation not permitted")
            shutil.rmtree(path)

        with self.assertLogs(cc.log, "WARNING"):
            res = self._sweep(delete=flaky)
        self.assertEqual(res["deleted"], ["code_sign_clone.Old002"])
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("Old001", res["errors"][0])
        self.assertEqual(self.fr.names(), ["code_sign_clone.New001", "code_sign_clone.Old001"])

    def test_the_time_budget_leaves_the_rest_for_the_next_pass(self):
        res = self._sweep(time_budget_s=-1)
        self.assertEqual(res["deleted"], [])
        self.assertEqual(len(res["left_for_next_pass"]), 2)
        self.assertEqual(len(self.fr.names()), 3)

    def test_with_too_little_of_the_job_left_the_pass_does_not_start(self):
        with mock.patch.object(cc, "read_lsof") as lsof:
            res = self._sweep(open_files=None, time_left_s=cc.MIN_PASS_S - 1)
        self.assertTrue(res["skipped"].startswith("no time left"))
        lsof.assert_not_called()
        self.assertEqual(len(self.fr.names()), 3)

    def test_the_tools_get_no_more_time_than_the_job_has_left(self):
        with mock.patch.object(cc, "read_lsof", return_value=[]) as lsof, \
             mock.patch.object(cc, "read_ps_starts", return_value={}) as ps:
            self._sweep(open_files=None, starts=None, time_left_s=20)
        self.assertLessEqual(lsof.call_args.kwargs["timeout"], 20)
        self.assertLessEqual(ps.call_args.kwargs["timeout"], cc.PS_TIMEOUT_S)

    def test_an_unreadable_lsof_deletes_nothing(self):
        with mock.patch.object(cc, "read_lsof", return_value=None):
            res = self._sweep(open_files=None)
        self.assertEqual(res["skipped"], "lsof unreadable")
        self.assertEqual(len(self.fr.names()), 3)

    def test_an_unreadable_ps_deletes_nothing(self):
        with mock.patch.object(cc, "read_ps_starts", return_value=None):
            res = self._sweep(starts=None)
        self.assertEqual(res["skipped"], "ps unreadable")
        self.assertEqual(len(self.fr.names()), 3)

    def test_a_crash_inside_the_pass_is_returned_not_raised(self):
        with mock.patch.object(cc, "plan", side_effect=RuntimeError("boom")), \
             self.assertLogs(cc.log, "WARNING"):
            res = self._sweep()
        self.assertIn("boom", res["error"])
        self.assertEqual(len(self.fr.names()), 3)

    def test_off_macos_it_does_nothing(self):
        with mock.patch.object(cc.sys, "platform", "linux"), \
             mock.patch.object(cc, "clone_root") as root, \
             mock.patch.object(cc, "read_lsof") as lsof:
            res = cc.sweep()
        self.assertEqual(res, {"skipped": "not-darwin"})
        root.assert_not_called()
        lsof.assert_not_called()


class ReaperRunsTheSweepTests(unittest.TestCase):
    def _reap(self, sweep):
        flag = mock.Mock()
        flag.exists.return_value = False
        from omniseek.core.sources.walled import _cdp
        with mock.patch.object(infra_jobs.sys, "platform", "darwin"), \
             mock.patch.object(infra_jobs, "_MAINT_FLAG", flag), \
             mock.patch.object(_cdp, "cdp_health", return_value=(False, "")), \
             mock.patch.object(infra_jobs.subprocess, "run"), \
             mock.patch.object(cc, "sweep", sweep):
            return infra_jobs.run_cdp_reaper()

    def test_the_reaper_reports_what_the_sweep_deleted(self):
        sweep = mock.Mock(return_value={"deleted": ["code_sign_clone.Old001"], "in_use": ["x"],
                                        "too_new": [], "errors": [], "left_for_next_pass": []})
        res = self._reap(sweep)
        sweep.assert_called_once()
        left = sweep.call_args.kwargs["time_left_s"]
        self.assertTrue(0 < left <= infra_jobs.REAPER_DEADLINE_S, left)
        self.assertEqual(res["clones"], {"deleted": ["code_sign_clone.Old001"], "in_use": 1,
                                         "too_new": 0, "errors": [], "left_for_next_pass": 0})
        self.assertEqual(len(res["already_down"]), 4)

    def test_a_sweep_that_raises_does_not_fail_the_reaper(self):
        with self.assertLogs(infra_jobs.log, "WARNING"):
            res = self._reap(mock.Mock(side_effect=RuntimeError("boom")))
        self.assertIn("boom", res["clones"]["error"])
        self.assertEqual(len(res["already_down"]), 4)


if __name__ == "__main__":
    unittest.main()

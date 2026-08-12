"""Contract for the off-machine backup audit (2026-08-11).

The failure this exists to catch: a mirror task that ran on time, failed on time, and told nobody
for fifteen days. So every assertion here is about MEASURING THE DESTINATION, and the cases that
matter most are the silent ones (no heartbeat, stale heartbeat, a heartbeat that says success while
the content is behind).
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from penumbra.core import infra_jobs as J


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _repo(path: Path, commits: int = 1) -> list[str]:
    """A throwaway git repo; returns the commit shas oldest-first."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    shas = []
    for i in range(commits):
        (path / f"f{i}.txt").write_text(str(i), encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", f"c{i}")
        shas.append(_git(path, "rev-parse", "HEAD").stdout.strip())
    return shas


class OffMachineAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hb = self.root / "heartbeat.json"
        self.backups = self.root / "backups"
        self.volumes = self.root / "Volumes"
        self.backups.mkdir()
        self.brain = self.root / "brain"
        self.shas = _repo(self.brain, commits=3)
        # _BACKUP_LOG is isolated too: without it these read the REAL backup log, so the machine's
        # actual lane verdict leaks into a unit test and makes it pass or fail for the wrong reason.
        self.backup_log = self.root / "state-backup.log"
        self.backup_log.write_text("backup ok: wall=w.db.gz off-machine=ext:Drive")
        self.patches = [
            patch.object(J, "_OFFMACHINE_HEARTBEAT", self.hb),
            patch.object(J, "_BACKUPS", self.backups),
            patch.object(J, "_VOLUMES", self.volumes),
            patch.object(J, "_BACKUP_LOG", self.backup_log),
            patch.object(J, "_MIRROR_TARGETS", {"brain": self.brain}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _write_hb(self, *, age_s=0.0, ok=True, head=None, error=""):
        stamp = datetime.fromtimestamp(time.time() - age_s).isoformat(timespec="seconds")
        self.hb.write_text(json.dumps({
            "at": stamp,
            "mirrors": {"brain": {"ok": ok, "head": head if head is not None else self.shas[-1],
                                  "count": 3, "error": error}},
        }), encoding="utf-8")

    # ── mirrors ────────────────────────────────────────────────────────────────
    def test_a_missing_heartbeat_is_itself_the_alarm(self):
        faults = J._audit_mirrors(time.time())
        self.assertTrue(any("心跳缺失" in f for f in faults), faults)

    def test_a_stale_heartbeat_is_the_alarm_even_if_it_reported_success(self):
        self._write_hb(age_s=5 * 24 * 3600, ok=True)
        faults = J._audit_mirrors(time.time())
        self.assertTrue(any("心跳已" in f for f in faults), faults)

    def test_a_reported_failure_surfaces_with_its_reason(self):
        self._write_hb(ok=False, error="ssh: connect to host 192.168.1.5 port 22: timed out")
        faults = J._audit_mirrors(time.time())
        self.assertTrue(any("上次镜像更新失败" in f and "192.168.1.5" in f for f in faults), faults)

    def test_a_mirror_head_the_live_repo_never_had_reads_as_divergence(self):
        self._write_hb(head="0" * 40)
        faults = J._audit_mirrors(time.time())
        self.assertTrue(any("分叉或损坏" in f for f in faults), faults)

    def test_a_mirror_far_behind_the_live_repo_is_caught_though_it_claims_success(self):
        # THE 15-DAY FAILURE, in miniature: the heartbeat says ok, the content is old.
        self._write_hb(head=self.shas[0])
        with patch.object(J, "_MIRROR_LAG_MAX_COMMITS", 1):
            faults = J._audit_mirrors(time.time())
        self.assertTrue(any("落后活仓 2 个 commit" in f for f in faults), faults)

    def test_a_current_mirror_with_a_fresh_heartbeat_is_clean(self):
        self._write_hb()
        self.assertEqual(J._audit_mirrors(time.time()), [])

    # ── external drive ─────────────────────────────────────────────────────────
    def _wall(self, base: Path, name: str, age_s: float):
        base.mkdir(parents=True, exist_ok=True)
        p = base / name
        p.write_text("x", encoding="utf-8")
        t = time.time() - age_s
        os.utime(p, (t, t))
        return p

    def test_no_local_wall_backup_at_all_is_reported(self):
        faults = J._audit_external_drive(time.time())
        self.assertTrue(any("本地一个 wall 备份都没有" in f for f in faults), faults)

    def test_a_mounted_drive_with_no_wall_copy_is_reported(self):
        # The drive must actually be VISIBLE for this to mean "the copy is missing"; with no volume
        # in sight the honest verdict is "I cannot look", which the visibility tests below cover.
        (self.volumes / "Drive").mkdir(parents=True)
        self._wall(self.backups, "wall-20260811.db.gz", 0)
        faults = J._audit_external_drive(time.time())
        self.assertTrue(any("外置盘上没有任何 wall 备份" in f for f in faults), faults)

    def test_an_offsite_copy_lagging_the_local_one_is_reported(self):
        self._wall(self.backups, "wall-20260811.db.gz", 0)
        self._wall(self.volumes / "Drive" / "penumbra-backups", "wall-20260801.db.gz", 10 * 86400)
        faults = J._audit_external_drive(time.time())
        self.assertTrue(any("比本地旧" in f for f in faults), faults)

    def test_a_current_offsite_copy_is_clean(self):
        self._wall(self.backups, "wall-20260811.db.gz", 0)
        self._wall(self.volumes / "Drive" / "penumbra-backups", "wall-20260811.db.gz", 60)
        self.assertEqual(J._audit_external_drive(time.time()), [])


class DriveVisibilityTests(unittest.TestCase):
    """'I cannot see the drive' and 'there is no backup on the drive' need OPPOSITE responses.

    macOS gates /Volumes/* behind a privacy grant a launchd process does not inherit, and the
    directory then reads EMPTY rather than raising. Measured 2026-08-11 with the same script a
    minute apart: under launchd off-machine=NONE, over ssh off-machine=ext:PenumbraRecovery. An
    audit that cannot tell those apart tells you to plug in a drive that is already plugged in.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.backups = root / "backups"
        self.volumes = root / "Volumes"
        self.log = root / "state-backup.log"
        self.backups.mkdir()
        self.volumes.mkdir()
        self.log.write_text("backup ok: wall=wall-20260812.db.gz off-machine=ext:Drive\n",
                            encoding="utf-8")
        (self.backups / "wall-20260812.db.gz").write_text("x", encoding="utf-8")
        self.patches = [patch.object(J, "_BACKUPS", self.backups),
                        patch.object(J, "_VOLUMES", self.volumes),
                        patch.object(J, "_BACKUP_LOG", self.log)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_a_blinded_process_says_it_cannot_look_not_that_nothing_is_there(self):
        # /Volumes empty AND no removable volume visible == the launchd blindness.
        faults = J._audit_external_drive(time.time())
        self.assertTrue(any("看不到可移动卷" in f for f in faults), faults)
        self.assertFalse(any("外置盘上没有任何 wall 备份" in f for f in faults), faults)

    def test_a_sighted_process_with_a_bare_drive_says_the_backup_is_missing(self):
        (self.volumes / "Drive").mkdir()          # a removable volume IS visible...
        faults = J._audit_external_drive(time.time())   # ... it just holds no backup
        self.assertTrue(any("外置盘上没有任何 wall 备份" in f for f in faults), faults)
        self.assertFalse(any("看不到可移动卷" in f for f in faults), faults)

    def test_the_lanes_own_NONE_verdict_is_reported_even_when_files_are_visible(self):
        # THE REAL 2026-08-11 STATE: files sit on the drive from manual runs, while every SCHEDULED
        # run records off-machine=NONE. The files must not paper over the lane's own verdict.
        self.log.write_text("backup ok: wall=wall-20260812.db.gz off-machine=NONE\n",
                            encoding="utf-8")
        d = self.volumes / "Drive" / "penumbra-backups"
        d.mkdir(parents=True)
        (d / "wall-20260812.db.gz").write_text("x", encoding="utf-8")
        faults = J._audit_external_drive(time.time())
        self.assertTrue(any("off-machine=NONE" in f for f in faults), faults)

    def test_a_healthy_lane_with_a_current_copy_is_clean(self):
        d = self.volumes / "Drive" / "penumbra-backups"
        d.mkdir(parents=True)
        (d / "wall-20260812.db.gz").write_text("x", encoding="utf-8")
        self.assertEqual(J._audit_external_drive(time.time()), [])


class LaunchdFleetTests(unittest.TestCase):
    """A guard can cease to EXIST, and then its silence reads as calm.

    2026-08-12: com.penumbra.infra.sentinel, the one external watchdog, was absent from launchd for
    four days. Its plist sat on disk, the registry declared it resident, its log simply stopped, and
    its alarm state files still carried week-old timestamps that looked like "no incidents" rather
    than "no observer". Nothing noticed, because the thing that would notice IS the watchdog.
    """

    def test_a_declared_resident_service_that_is_not_loaded_is_reported(self):
        with patch.object(J, "_declared_resident_labels",
                          lambda: ["com.penumbra.organ.eye-http", "com.penumbra.infra.sentinel"]):
            with patch.object(J, "_loaded_labels", lambda: {"com.penumbra.organ.eye-http"}):
                faults = J._audit_launchd_fleet()
        self.assertTrue(any("com.penumbra.infra.sentinel" in f for f in faults), faults)
        self.assertTrue(any("舰队缺员" in f for f in faults), faults)

    def test_a_complete_fleet_is_clean(self):
        with patch.object(J, "_declared_resident_labels", lambda: ["a", "b"]):
            with patch.object(J, "_loaded_labels", lambda: {"a", "b", "unrelated"}):
                self.assertEqual(J._audit_launchd_fleet(), [])

    def test_an_unreadable_registry_says_nothing_rather_than_crying_wolf(self):
        with patch.object(J, "_declared_resident_labels", lambda: []):
            with patch.object(J, "_loaded_labels", lambda: set()):
                self.assertEqual(J._audit_launchd_fleet(), [])

    def test_an_unavailable_launchctl_is_unauditable_not_a_failure(self):
        with patch.object(J, "_declared_resident_labels", lambda: ["a"]):
            with patch.object(J, "_loaded_labels", lambda: set()):
                self.assertEqual(J._audit_launchd_fleet(), [])


if __name__ == "__main__":
    unittest.main()

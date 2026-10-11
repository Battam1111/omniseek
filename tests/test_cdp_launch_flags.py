"""Every logged-in CDP browser launcher caps its disk cache with the same value (2026-10-10).

Reads scripts/launch_cdp_*.sh as text and parses the `exec "$CHROME_APP" ...` command line; nothing
is run. A launcher added later without the flag, or with a different value, fails here.
Source-repo only: public_prepare.sh strips the launchers from the public mirror.
"""
import pathlib
import shlex
import unittest

try:
    from _repo_only import requires_source_repo
except ImportError:
    from tests._repo_only import requires_source_repo

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
KNOWN = {"launch_cdp_cn_forums.sh", "launch_cdp_douyin.sh", "launch_cdp_xhs.sh", "launch_cdp_xhs_cn.sh"}
# PROVISIONAL 200 MiB per profile (see the DISK 2026-10-10 comment in the launchers); change it here
# and in all four launchers together.
DISK_CACHE_BYTES = 209715200


def chrome_args(text):
    joined = text.replace("\\\n", " ")
    lines = [ln.strip() for ln in joined.splitlines() if ln.strip().startswith('exec "$CHROME_APP"')]
    if len(lines) != 1:
        raise AssertionError(f"expected one exec \"$CHROME_APP\" line, found {len(lines)}")
    return shlex.split(lines[0])[2:]


@requires_source_repo
class CdpLaunchFlagTests(unittest.TestCase):
    def setUp(self):
        self.launchers = sorted(SCRIPTS.glob("launch_cdp_*.sh"))

    def test_known_launchers_are_found(self):
        self.assertTrue(KNOWN <= {p.name for p in self.launchers}, [p.name for p in self.launchers])

    def test_every_launcher_caps_the_disk_cache_with_the_same_value(self):
        for path in self.launchers:
            with self.subTest(script=path.name):
                args = chrome_args(path.read_text(encoding="utf-8"))
                sizes = [a.split("=", 1)[1] for a in args if a.startswith("--disk-cache-size=")]
                self.assertEqual(sizes, [str(DISK_CACHE_BYTES)])

    def test_profile_dir_is_still_passed(self):
        for path in self.launchers:
            with self.subTest(script=path.name):
                args = chrome_args(path.read_text(encoding="utf-8"))
                self.assertIn('--user-data-dir=$USER_DATA_DIR', args)

    def test_parser_rejects_a_flag_that_only_sits_in_a_comment(self):
        text = ('# --disk-cache-size=209715200\n'
                'exec "$CHROME_APP" \\\n  --user-data-dir="$USER_DATA_DIR" \\\n  about:blank\n')
        self.assertNotIn("--disk-cache-size=209715200", chrome_args(text))


if __name__ == "__main__":
    unittest.main()

"""Where OmniSeek keeps its data: the ONE module that names those locations.

Every other module and script asks this one; none of them builds a path under the home directory on
its own.

Where the data lives is not written here. A deployment that has a parts table (a release tree carries
``.omo_places.json`` at its root, generated from the table; a source checkout has
``10_deploy/parts.json``) names every data location there, and each function below takes its location
from that table BY NAME: a name the table lacks raises NotRegistered, which says where to register it.
Without a table (the public build), each function falls back to the default written next to it.

Each location is computed from the HOME of the calling process at CALL time (``Path.home()``), never
frozen at import: a process started with another HOME (a rehearsal home, a test's temporary home)
gets locations under that home. No environment switch moves a location and there is no fallback to an
earlier layout: a location is where this module says, or nowhere. (The one switch here, REHEARSAL_ENV,
moves nothing: it only marks a process as not the live eye.)

Comments and messages across the package name these locations by the function, in angle brackets:
``<root>``, ``<state>``, ``<logs>``, ``<debug>``, ``<models>``, ``<credentials>``, ``<doc-inbox>``.
``show(path)`` renders a path for a human message (``~/...`` when under the home directory).

This file imports only the standard library and nothing of the package, so a script that must keep
working when the rest of OmniSeek is broken (scripts/services.py, scripts/omniseek_doctor.py) can load it by file path, and a shell script
can ask it on the command line: ``python3 <this file> state`` prints that one location.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

# The repository's structure check reads this line: only a part's location module may build a path
# from the home directory, and each part has exactly one.
OMO_LOCATION_MODULE = "omniseek"


# ── the parts table: data locations by name ───────────────────────────────────────────────────────

PLACES_FILE = ".omo_places.json"
_TABLE: list = []


class NotRegistered(LookupError):
    """A data location the parts table does not name; the message says where to register it."""


def _read_table(start: str):
    """Look upward from start for the table: the release tree's PLACES_FILE, or a checkout's
    10_deploy/parts.json. Returns (source, {part: (home, {name: path})}) or (None, None); paths are
    as the table writes them (relative to the home directory unless absolute)."""
    import json
    d = start
    while True:
        f = os.path.join(d, PLACES_FILE)
        if os.path.isfile(f):
            with open(f, encoding="utf-8") as h:
                doc = json.load(h)
            return f, {p: (v["home"]["rel"], {n: e["rel"] for n, e in v["names"].items()})
                       for p, v in doc["parts"].items()}
        f = os.path.join(d, "10_deploy", "parts.json")
        if os.path.isfile(f):
            with open(f, encoding="utf-8") as h:
                doc = json.load(h)
            return f, {p: (v["home"].rstrip("/"), {n: e["path"].rstrip("/") for n, e in (v.get("data") or {}).items()
                                                   if isinstance(e, dict)})
                       for p, v in doc["parts"].items() if isinstance(v, dict) and v.get("home") and "target" not in v}
        up = os.path.dirname(d)
        # A checkout's root (.git, a directory or a worktree's file) ends the search: a clone nested in
        # another checkout, like the public mirror under 08_public_mirrors/, must not read the outer
        # checkout's table (11-11: the mirror sync on the laptop died on NotRegistered that way).
        if up == d or os.path.exists(os.path.join(d, ".git")):
            return None, None
        d = up


def _table():
    if not _TABLE:
        _TABLE.append(_read_table(os.path.dirname(os.path.realpath(__file__))))
    return _TABLE[0]


def has_table() -> bool:
    """Whether this deployment has a parts table (False in the public build)."""
    return _table()[1] is not None


def _at(name: str, default: Callable[[], Optional[Path]], part: str = OMO_LOCATION_MODULE,
        child: Optional[str] = None) -> Optional[Path]:
    """The data location registered as ``name`` under ``part`` (plus ``child`` inside it), or
    ``default()`` when there is no table. A table without the name raises NotRegistered."""
    src, t = _table()
    if t is None:
        return default()
    names = t.get(part, (None, {}))[1]
    if name not in names:
        raise NotRegistered(f"{name} is not registered: add it under parts.{part}.data in 10_deploy/parts.json, "
                            f"then release (table {src})")
    p = Path.home() / names[name]
    return p / child if child else p


def _part_home(part: str, default: Callable[[], Path]) -> Path:
    """A part's home directory as the table writes it, or ``default()`` without a table."""
    src, t = _table()
    if t is None:
        return default()
    if part not in t:
        raise NotRegistered(f"the parts table {src} has no part {part}")
    return Path.home() / t[part][0]


def _none() -> None:
    return None


def home() -> Path:
    """The home directory itself (relative document paths resolve against it; the disk check reads it)."""
    return Path.home()


# ── is this the live eye? ─────────────────────────────────────────────────────────────────────────
# The launchd services (OmniSeek, its CDP browsers) are installed in the account's own home, the one the
# system's user database names; the release tooling calls the same place the live home
# (10_deploy/lib/omo_release.py live_home). A process started with any other HOME (a release rehearsal,
# a test's temporary home) is not the live eye, and neither is one started with REHEARSAL_ENV set: such a
# process must not start, stop or connect to the live services (_cdp.live_port_refusal reads this).
REHEARSAL_ENV = "OMNISEEK_REHEARSAL"


def account_home() -> Path:
    """The account's own home directory from the user database, whatever HOME says."""
    import os
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def not_live_reason() -> str:
    """Why this process is not the live eye, or "" when it is."""
    import os
    switch = (os.environ.get(REHEARSAL_ENV) or "").strip()
    if switch and switch != "0":
        return f"{REHEARSAL_ENV}={switch}"
    try:
        live = account_home().resolve()
    except (ImportError, KeyError, OSError) as exc:
        return f"the account's home cannot be read ({type(exc).__name__})"
    if home().resolve() != live:
        return f"HOME {home()} is not the account's home {live}"
    return ""


# ── OmniSeek's own data ────────────────────────────────────────────────────────────────────────────

def root() -> Path:
    """OmniSeek's own home (its code and data directories sit under it)."""
    return _part_home(OMO_LOCATION_MODULE, lambda: Path.home() / ".omniseek")


def credentials() -> Path:
    return root() / "credentials"


def state() -> Path:
    return _at("state", lambda: root() / "state")


def logs() -> Path:
    return _at("logs", lambda: root() / "logs")


def debug() -> Path:
    return _at("debug", lambda: root() / "debug")


def models() -> Path:
    return _at("models", lambda: root() / "models")


def scratch() -> Path:
    """Per-process temporary files: an eye process's own temp directory is ``<scratch>/<pid>``, swept
    once that process is gone (omniseek.core.scratch)."""
    return _at("scratch", lambda: root() / "scratch")


def asr_run() -> Path:
    """The speech workers' shared state across eye processes: the transcription slot locks, the
    inference lock and each live eye process's control socket (omniseek.core.asr_sched)."""
    return _at("asr", lambda: root() / "asr")


CHROME_PROFILES = ("cdp", "xhs")


def chrome(name: str) -> Path:
    """The user-data directory of one logged-in browser (``cdp`` is the shared 9222 one)."""
    if name not in CHROME_PROFILES:
        raise ValueError(f"unknown browser profile {name!r} (known: {', '.join(CHROME_PROFILES)})")
    return _at(f"chrome-{name}", lambda: root() / f"chrome-{name}")


def profile_json() -> Path:
    return _at("profile", lambda: root() / "profile.json")


def signposts_json() -> Path:
    return _at("signposts", lambda: root() / "signposts.json")


def watchlist_json() -> Path:
    return _at("watchlist", lambda: root() / "watchlist.json")


def modelscope_cache() -> Path:
    """The speech model cache (the modelscope library's own default place; MODELSCOPE_CACHE moves it)."""
    return Path.home() / ".cache" / "modelscope"


# ── other parts' data OmniSeek reads (each written here once) ─────────────────────────────────────

def doc_inbox() -> Path:
    """The shared document inbox omniseek_read / omniseek_view read local files from."""
    return root() / "inbox"


def brain_repo() -> Path:
    """The brain's live notes repository (read by the weekly briefing and the mirror audit)."""
    return root() / "brain"


def brain_notes() -> Path:
    return brain_repo() / "notes"


def omniseek_source_repo() -> Path:
    """OmniSeek's source repository (its off-machine mirror audit retired 2026-10-16)."""
    return root() / "source"


def notify_outlet() -> Optional[Path]:
    """The deployment's push outlet program, or None when it has none."""
    return None  # the public build ships no default outlet


def notify_redact() -> Optional[Path]:
    """The push outlet's masking rule (07_services/notify/redact.py as the services part installs it): OmniSeek runs
    every query it sends to machines that are not ours through it (omniseek.redact.outward).
    None when the deployment has no such rule (the public build): queries then go out as they are, and that is
    not a fault. This return value is the ONE place that tells the two apart; a path that is set but cannot be
    loaded is a fault (omniseek.redact.outward logs an ERROR and alerts once)."""
    return None  # the public build has no push outlet, so no outward masking rule (not a fault)


def cubox_puller_state() -> Optional[Path]:
    """The digest puller's state file (its share of the Cubox daily quota), or None when there is none."""
    return None  # the public build ships no default puller state


def services_table() -> Optional[Path]:
    """The deployment's resident-task table, or None when sensors run on their own schedules."""
    return None  # the public build ships no resident-task table


def egress2_state() -> Optional[Path]:
    """The second exit egress2's state file (its generator writes it on every start): the exit-node sweep
    (eye.exit_node) reads its test port ``probe``, test group ``probe_group`` and ``control_sock`` there.
    None when the deployment has no egress2."""
    return _at("egress2-state", _none, part="services")


def proxy_control() -> Optional[Path]:
    """The control socket of the operator's proxy app (Clash Verge's mihomo), the one OmniSeek's traffic
    leaves through (its TUN). A fixed place the app itself picks, not under the home directory. The
    exit-node sweep reads its groups and switches the one selector named by PROXY_SELECTOR; nothing else
    of the app is touched. None when the deployment has no such app."""
    return Path("/tmp/verge/verge-mihomo.sock")


# ── names that are not locations, kept here so code asks one place ───────────────────────────────

LABELS = {
    "eye-http": "com.omniseek.organ.eye-http",
    "cdp-cn-forums": "com.omniseek.cdp.cn-forums",
    "cdp-xhs": "com.omniseek.cdp.xhs",
    "brain-http": "com.omniseek.organ.brain-http",
}


# The selector group of the operator's proxy app that picks the node OmniSeek's traffic leaves through
# (its rules send OmniSeek's hosts to it). The exit-node sweep only changes which node it selects.
PROXY_SELECTOR = "🔰 选择节点"


def label(name: str) -> str:
    """The launchd label of one of OmniSeek's (or the brain's) services, by its short name."""
    return LABELS[name]


def launch_agents() -> Path:
    """Where launchd reads per-user service definitions (a fixed place of the operating system)."""
    return Path.home() / "Library" / "LaunchAgents"


def user_applications() -> Path:
    """The per-user Applications folder (a second place a browser may be installed)."""
    return Path.home() / "Applications"


def icloud_drive() -> Path:
    """The iCloud Drive folder, when the operator enabled it (the retired backup's encrypted lane)."""
    return Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


def launch_agent_plist(name: str) -> Path:
    """The installed plist of one service, by its short name (see LABELS)."""
    return launch_agents() / f"{label(name)}.plist"


# ── helpers ───────────────────────────────────────────────────────────────────────────────────────

def show(path) -> str:
    """A path for a human message: ``~/...`` when it is under the home directory, else as is."""
    p = Path(path)
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)


NAMES: Dict[str, Callable[[], Optional[Path]]] = {
    "home": home,
    "root": root,
    "credentials": credentials,
    "state": state,
    "logs": logs,
    "debug": debug,
    "models": models,
    "scratch": scratch,
    "asr": asr_run,
    "modelscope-cache": modelscope_cache,
    "chrome-cdp": lambda: chrome("cdp"),
    "chrome-xhs": lambda: chrome("xhs"),
    "profile": profile_json,
    "signposts": signposts_json,
    "watchlist": watchlist_json,
    "doc-inbox": doc_inbox,
    "brain-repo": brain_repo,
    "brain-notes": brain_notes,
    "eye-source-repo": omniseek_source_repo,
    "notify-outlet": notify_outlet,
    "cubox-puller-state": cubox_puller_state,
    "services-table": services_table,
    "egress2-state": egress2_state,
    "proxy-control": proxy_control,
    "launch-agents": launch_agents,
    "icloud-drive": icloud_drive,
}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in NAMES:
        print("usage: where.py <name>   names: " + " ".join(NAMES), file=sys.stderr)
        return 2
    value = NAMES[args[0]]()
    if value is None:
        print(f"where.py: {args[0]} is not configured in this build", file=sys.stderr)
        return 1
    print(value)
    return 0


if __name__ == "__main__":
    sys.exit(main())

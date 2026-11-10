"""ai_incidents: the AI Incident Database (AIID), read from its weekly public snapshot (STRUCTURE).

The AIID (Responsible AI Collaborative) is the curated ledger of real-world AI harms: ~1.75k
incidents (deepfake abuse, chatbot harms, autonomous-vehicle crashes, biased deployments), each with
the alleged developer / deployer / harmed parties and the news reports behind it. It is OmniSeek's
safety x STRUCTURE source: the cross-incident ledger web search cannot return.

History: until 2026-10-20 this source queried the AIID GraphQL API. That API now answers every query
with 401 API_LOGIN_REQUIRED, so the source was withdrawn (commit 507854d2). The AIID's own bulk
export for research needs no login, so the source is back on it (task EYE-AIID-SNAP, 2026-10-20).

DESIGN: OmniSeek's bulk-file pattern (cordis_eu_source.py), with a local index on disk:
  * The snapshot page (``SNAPSHOT_PAGE``) lists one ``backup-<YYYYMMDDhhmmss>.tar.bz2`` a week (on
    Mondays, 10:00 to 12:xx UTC over the last 60), each with its size in MB, on a public Cloudflare R2
    bucket. The job ``aiid-snapshot`` (jobs.py, weekly) calls ``refresh()``: read the page, and only
    when its newest snapshot is newer than the one in use, download it, check it is whole, build the
    index, then switch to it. A query never touches the network: ``search`` ranks the local index.
  * Whole means: the bytes received equal the Content-Length, they match the size the page lists (to
    its two decimals of MB), and the bzip2 stream decodes to its end (each bzip2 block carries a CRC)
    with both wanted tables found. Nothing is switched in before all of that holds.
  * Only the two tables this source serves are read, as streams out of the archive (nothing else is
    unpacked to disk): ``incidents.csv`` and ``reports.csv``. ``submissions`` is never read: the AIID
    terms of use exclude it from the CC BY-SA licence. The terms also exclude the ``text`` column of
    reports (the article body), so that column is dropped while reading; a report is kept as its
    link, title, outlet and date, and the article is read at its own URL.
  * An archive member whose name is absolute, contains ``..``, or that is a link (or any non-regular
    file) is skipped and logged, and its name is kept in the snapshot's meta.json.
  * Layout under ``<state>/aiid/``: ``snap-<stamp>/`` (index.json + meta.json), ``current`` (a symlink
    to the one in use, swapped atomically), ``check.json`` (the last weekly check). After a successful
    switch the older ``snap-*`` directories are removed, so one snapshot is kept. The downloaded
    archive is removed by ``refresh`` whether the build succeeded or not.
  * ``health_check`` reports the snapshot date in use and calls the source stale when that snapshot is
    more than ``STALE_AFTER_DAYS`` old (provisional 14: one snapshot a week, so two missed ones; the
    page's history since 2025-01 has no gap over 8 days. Recalibrate after 2026-10-27 from check.json).

Licence: the AIID database is CC BY-SA 4.0 (https://incidentdatabase.ai/terms-of-use/, and
license.txt inside each snapshot); every document carries that and its incident URL in
metadata.license. Platform note: docs/platform-notes/aiid-snapshot.md.
"""

from __future__ import annotations

import bz2
import csv
import io
import json
import logging
import os
import re
import shutil
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Optional

import anyio

from omniseek import where
from omniseek.core import diag, http, portal
from omniseek.core.sources._base import SourceBase
from omniseek.core.normalize import Document, html_fragment_text, keyword_score_filter, mk_signal, to_utc

logger = logging.getLogger(__name__)

SNAPSHOT_PAGE = "https://incidentdatabase.ai/research/snapshots/"
CITE_URL = "https://incidentdatabase.ai/cite/{iid}"
TERMS_URL = "https://incidentdatabase.ai/terms-of-use/"
CC_BY_SA_URL = "https://creativecommons.org/licenses/by-sa/4.0/"

# Provisional (task EYE-AIID-SNAP): a snapshot a week, so stale = two missed snapshots. The page's
# whole history (284 snapshots, 2021-03 to 2026-10) has gaps over 14 days only before 2025-01.
# Recalibrate after 2026-10-27 against the intervals check.json records.
STALE_AFTER_DAYS = 14

# The tables this source reads (file names inside the archive) and the ones it must never read.
WANTED = ("incidents.csv", "reports.csv")
EXCLUDED_TABLES = ("submissions",)   # not under the CC BY-SA licence (AIID terms of use, "Exclusions")
REPORT_FIELDS = ("report_number", "title", "url", "source_domain", "date_published", "language")

_SNAP_RE = re.compile(r"""href=["'](https://[^"'\s]+/(backup-(\d{14})\.tar\.bz2))["']""")
_SIZE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*MB")
_UA = "omniseek-eye (AIID weekly snapshot reader)"


def data_root() -> Path:
    return where.state() / "aiid"


# ── the snapshot page ─────────────────────────────────────────────────────────────────────────────

def parse_snapshot_page(html: str) -> list[dict]:
    """Every snapshot the page lists, oldest first: {name, stamp, url, date (ISO, UTC), size_mb}.
    ``size_mb`` is the number the page prints before the link (None when it prints none)."""
    seen: dict[str, dict] = {}
    for m in _SNAP_RE.finditer(html or ""):
        url, name, stamp = m.group(1), m.group(2), m.group(3)
        if stamp in seen:
            continue
        try:
            when = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        before = html_fragment_text(html[max(0, m.start() - 600):m.start()])
        sizes = _SIZE_RE.findall(before)
        seen[stamp] = {"name": name, "stamp": stamp, "url": url, "date": when.isoformat(),
                       "size_mb": float(sizes[-1]) if sizes else None}
    return [seen[k] for k in sorted(seen)]


def _get_page(url: str) -> str:
    r = http.direct("GET", url, headers={"User-Agent": _UA}, timeout=60, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _download(url: str, dest: Path) -> tuple[int, Optional[int]]:
    """Sync entry for a caller on a thread: ``_adownload`` on the event loop (portal.call)."""
    return portal.call(_adownload, url, dest)


async def _adownload(url: str, dest: Path) -> tuple[int, Optional[int]]:
    """Stream ``url`` to ``dest``. Returns (bytes written, Content-Length or None). Stops (raises) as
    soon as more bytes arrive than the Content-Length announced."""
    total = 0
    async with http.adirect_stream("GET", url, timeout=120, headers={"User-Agent": _UA},
                                   follow_redirects=True) as resp:
        resp.raise_for_status()
        cl = resp.headers.get("content-length")
        expect = int(cl) if cl and cl.isdigit() else None
        async with await anyio.open_file(dest, "wb") as f:
            async for chunk in resp.aiter_bytes(1 << 20):
                total += len(chunk)
                if expect is not None and total > expect:
                    raise RuntimeError(f"received more than the announced {expect} bytes")
                await f.write(chunk)
    return total, expect


def check_whole(snap: dict, received: int, content_length: Optional[int]) -> str:
    """'' when the download is whole by size, else why not. The bzip2 pass checks the rest."""
    if received <= 0:
        return "nothing received"
    if content_length is None and snap.get("size_mb") is None:
        return "neither a Content-Length nor a listed size to check the download against"
    if content_length is not None and received != content_length:
        return f"received {received} bytes, Content-Length says {content_length}"
    if snap.get("size_mb") is not None and abs(received / 1e6 - snap["size_mb"]) > 0.0051:
        return f"received {received / 1e6:.3f} MB, the page lists {snap['size_mb']} MB"
    return ""


# ── reading the archive ───────────────────────────────────────────────────────────────────────────

def unsafe_member_reason(m: tarfile.TarInfo) -> str:
    """Why an archive member must not be used ('' when it may): absolute names, '..' parts, links and
    any other non-regular, non-directory member."""
    name = m.name or ""
    if name.startswith("/") or name.startswith("\\") or re.match(r"^[A-Za-z]:", name):
        return "absolute path"
    if ".." in PurePosixPath(name.replace("\\", "/")).parts:
        return "contains '..'"
    if m.issym() or m.islnk():
        return "link"
    if not (m.isfile() or m.isdir()):
        return "not a regular file"
    return ""


def _json_list(s: Any) -> list:
    try:
        v = json.loads(s or "[]")
    except (TypeError, ValueError):
        return []
    return v if isinstance(v, list) else []


def _entity_names(s: Any) -> list[str]:
    """An AIID entity list (JSON array of slugs such as "deepfake-creators") -> readable names."""
    return [str(x).replace("-", " ").strip() for x in _json_list(s) if str(x).strip()]


class _ReadOnly(io.RawIOBase):
    """A member of a stream-mode tar as a plain readable stream (the member object has no
    seekable(), which io.TextIOWrapper asks for)."""

    def __init__(self, f: Any) -> None:
        self._f = f

    def readable(self) -> bool:
        return True

    def readinto(self, b: Any) -> int:
        data = self._f.read(len(b))
        b[:len(data)] = data
        return len(data)


def read_archive(path: Path) -> dict:
    """Stream the archive once: read the wanted tables, skip unsafe members, never read the excluded
    ones. Returns {incidents: [row], reports: {number: row}, skipped: [{name, reason}],
    excluded: [name], members: n}. Raises when the bzip2/tar stream is damaged or a wanted table is
    missing, so a partial archive is never used."""
    old_limit = csv.field_size_limit()
    csv.field_size_limit(sys.maxsize)   # report article bodies can exceed the 128 KiB default
    incidents: list[dict] = []
    reports: dict[str, dict] = {}
    found: dict[str, str] = {}
    skipped: list[dict] = []
    excluded: list[str] = []
    n = 0
    try:
        with bz2.BZ2File(path) as bz, tarfile.open(fileobj=bz, mode="r|") as tar:
            for m in tar:
                n += 1
                why = unsafe_member_reason(m)
                if why:
                    skipped.append({"name": m.name, "reason": why})
                    logger.warning("ai_incidents: skipped archive member %r (%s)", m.name, why)
                    continue
                if not m.isfile():
                    continue
                base = PurePosixPath(m.name).name
                if any(base.startswith(t) for t in EXCLUDED_TABLES):
                    excluded.append(m.name)
                    continue
                if base not in WANTED or "translations" in PurePosixPath(m.name).parts:
                    continue
                if base in found:
                    skipped.append({"name": m.name, "reason": f"second {base} (used {found[base]})"})
                    logger.warning("ai_incidents: skipped archive member %r (second %s)", m.name, base)
                    continue
                found[base] = m.name
                fh = tar.extractfile(m)
                text = io.TextIOWrapper(io.BufferedReader(_ReadOnly(fh), 1 << 20), encoding="utf-8",
                                        errors="replace", newline="")
                if base == "incidents.csv":
                    for row in csv.DictReader(text):
                        incidents.append({k: row.get(k) or "" for k in (
                            "incident_id", "date", "reports", "title", "description",
                            "Alleged deployer of AI system", "Alleged developer of AI system",
                            "Alleged harmed or nearly harmed parties")})
                else:
                    for row in csv.DictReader(text):   # the text column is read past, never kept
                        num = (row.get("report_number") or "").strip()
                        if num:
                            reports[num] = {k: (row.get(k) or "").strip() for k in REPORT_FIELDS}
            # Read the compressed stream to its end: BZ2File checks every block's CRC and raises
            # EOFError when the stream stops before its end-of-stream marker (a truncated file).
            while bz.read(1 << 20):
                pass
    finally:
        csv.field_size_limit(old_limit)
    missing = [w for w in WANTED if w not in found]
    if missing:
        raise RuntimeError(f"archive has no {', '.join(missing)}")
    if not incidents:
        raise RuntimeError("incidents.csv has no rows")
    return {"incidents": incidents, "reports": reports, "skipped": skipped, "excluded": excluded,
            "members": n, "tables": found}


def build_records(parsed: dict) -> list[dict]:
    """Join each incident to its reports (by report number). One compact record per incident."""
    reports = parsed["reports"]
    out: list[dict] = []
    for row in parsed["incidents"]:
        iid = (row.get("incident_id") or "").strip()
        title = (row.get("title") or "").strip()
        if not iid.isdigit() or not title:
            continue
        reps = []
        for num in _json_list(row.get("reports")):
            r = reports.get(str(num))
            if r and r.get("url"):
                reps.append(dict(r))
        out.append({
            "incident_id": int(iid), "title": title, "date": (row.get("date") or "").strip()[:10],
            "description": (row.get("description") or "").strip(),
            "deployers": _entity_names(row.get("Alleged deployer of AI system")),
            "developers": _entity_names(row.get("Alleged developer of AI system")),
            "harmed": _entity_names(row.get("Alleged harmed or nearly harmed parties")),
            "reports": reps,
        })
    out.sort(key=lambda r: -r["incident_id"])
    return out


# ── the weekly refresh (the job) ──────────────────────────────────────────────────────────────────

def current_meta(root: Optional[Path] = None) -> Optional[dict]:
    p = (root or data_root()) / "current" / "meta.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _remove_own(path: Path) -> None:
    """Remove a file or directory this module made (old snapshots, downloads, half builds)."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except OSError as exc:
        logger.warning("ai_incidents: could not remove %s: %s", path, exc)


def refresh(root: Optional[Path] = None, *, get_page: Callable[[str], str] = _get_page,
            download: Callable[[str, Path], tuple[int, Optional[int]]] = _download,
            now: Optional[float] = None) -> dict:
    """The weekly check. Returns a summary (also written to <root>/check.json): outcome is one of
    unchanged | installed | failed. Never raises for an upstream or archive problem: a failure keeps
    the snapshot in use, and health_check calls it stale once it is STALE_AFTER_DAYS old."""
    root = root or data_root()
    root.mkdir(parents=True, exist_ok=True)
    started = time.time() if now is None else now
    summary: dict = {"checked_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
                     "outcome": "failed", "error": ""}
    for leftover in list(root.glob("download-*")) + list(root.glob("*.building-*")):
        _remove_own(leftover)   # a run killed half way leaves these behind
    archive: Optional[Path] = None
    try:
        snaps = parse_snapshot_page(get_page(SNAPSHOT_PAGE))
        if not snaps:
            raise RuntimeError("the snapshot page lists no backup-*.tar.bz2")
        latest = snaps[-1]
        summary["latest_listed"] = latest["name"]
        summary["listed"] = len(snaps)
        cur = current_meta(root) or {}
        if cur.get("stamp") and cur["stamp"] >= latest["stamp"]:
            summary.update(outcome="unchanged", in_use=cur.get("name"))
            return summary
        archive = root / f"download-{latest['stamp']}.tar.bz2"
        t0 = time.monotonic()
        received, clen = download(latest["url"], archive)
        t_download = time.monotonic() - t0
        bad = check_whole(latest, received, clen)
        if bad:
            raise RuntimeError(f"download not whole: {bad}")
        t1 = time.monotonic()
        parsed = read_archive(archive)
        t_unpack = time.monotonic() - t1
        t2 = time.monotonic()
        records = build_records(parsed)
        final = root / f"snap-{latest['stamp']}"
        building = root / f"snap-{latest['stamp']}.building-{os.getpid()}"
        building.mkdir()
        _write_json(building / "index.json", records)
        meta = {
            "name": latest["name"], "stamp": latest["stamp"], "date": latest["date"],
            "url": latest["url"], "size_mb": latest["size_mb"], "bytes": received,
            "installed_at": datetime.now(timezone.utc).isoformat(),
            "incidents": len(records), "reports": len(parsed["reports"]),
            "linked_reports": sum(len(r["reports"]) for r in records),
            "tables": parsed["tables"], "members": parsed["members"],
            "skipped_members": parsed["skipped"], "excluded_members": parsed["excluded"],
        }
        t_index = time.monotonic() - t2
        meta["timings_s"] = {"download": round(t_download, 2), "unpack": round(t_unpack, 2),
                             "index": round(t_index, 2)}
        _write_json(building / "meta.json", meta)
        if final.exists():
            _remove_own(final)
        os.replace(building, final)
        link_tmp = root / f"current.tmp-{os.getpid()}"
        _remove_own(link_tmp)
        os.symlink(final.name, link_tmp)
        os.replace(link_tmp, root / "current")
        for old in root.glob("snap-*"):
            if old.name != final.name:
                _remove_own(old)
        summary.update(outcome="installed", in_use=latest["name"], incidents=len(records),
                       reports=len(parsed["reports"]), skipped_members=parsed["skipped"],
                       timings_s=meta["timings_s"])
        return summary
    except Exception as exc:  # noqa: BLE001 -- recorded, the snapshot in use stays
        summary["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        logger.warning("ai_incidents: weekly snapshot check failed: %s", summary["error"])
        diag.note("ai_incidents.refresh", url=SNAPSHOT_PAGE, exc=exc)
        return summary
    finally:
        if archive is not None:
            _remove_own(archive)
        try:
            _write_json(root / "check.json", summary)
        except OSError as exc:
            logger.warning("ai_incidents: could not write check.json: %s", exc)


# ── the source ────────────────────────────────────────────────────────────────────────────────────

def record_to_doc(rec: dict, meta: dict) -> Document:
    iid = rec["incident_id"]
    cite = CITE_URL.format(iid=iid)
    bits = [rec.get("description") or ""]
    if rec.get("developers"):
        bits.append("Developer: " + ", ".join(rec["developers"]))
    if rec.get("deployers"):
        bits.append("Deployer: " + ", ".join(rec["deployers"]))
    if rec.get("harmed"):
        bits.append("Harmed: " + ", ".join(rec["harmed"]))
    reports = rec.get("reports") or []
    return Document(
        source="ai_incidents",
        source_id=str(iid),
        url=cite,
        title=rec["title"],
        content="\n".join(b for b in bits if b).strip() or rec["title"],
        author=", ".join(rec.get("deployers") or rec.get("developers") or []) or None,
        date=to_utc(rec.get("date")),
        signals=mk_signal("reports", len(reports), kind="engagement", by="aiid/reports") if reports else {},
        tags=["ai-incident"],
        metadata={
            "incident_id": iid,
            "developers": rec.get("developers") or [],
            "deployers": rec.get("deployers") or [],
            "harmed_parties": rec.get("harmed") or [],
            "reports": reports,   # drill-in handles: each report's own URL (omniseek_read it)
            "snapshot": meta.get("name"), "snapshot_date": meta.get("date"),
            "license": (f"CC BY-SA 4.0 ({CC_BY_SA_URL}); source: AI Incident Database, {cite} "
                        f"(terms {TERMS_URL}); report articles keep their own rights, read them at "
                        f"their URLs"),
        },
    )


class AIIncidentsAdapter(SourceBase):
    name = "ai_incidents"
    backend = "aiid"
    needs_credentials = False
    kind = "lookup"
    domains = ["safety"]
    modes = ["STRUCTURE"]
    cache_ttl = 0   # a local index: nothing to cache
    coverage = {"since": "1983", "until": None, "gaps": [],
                "note": "every incident in the newest weekly AIID snapshot (refreshed by OmniSeek job "
                        "aiid-snapshot); search is over title, description and the named parties"}
    license = (f"CC BY-SA 4.0 ({CC_BY_SA_URL}): AI Incident Database snapshot, "
               f"https://incidentdatabase.ai (terms {TERMS_URL}); submissions and report article "
               f"texts are excluded and not kept")
    samples = [{"query": "deepfake"}, {"query": "autonomous vehicle"}, {"query": "chatbot"}]
    description = (
        "AI Incident Database (AIID): the curated ledger of real-world AI HARMS (~1.75k incidents), "
        "read from the AIID's weekly public snapshot into a local index: search incidents by keyword "
        "(deepfake, chatbot, autonomous vehicle, facial recognition, biased hiring) -> each with the "
        "alleged developer + deployer, harmed parties, date, and the report links to drill into. "
        "STRUCTURE, keyless, CC BY-SA 4.0. AI 事故库: 真实世界 AI 伤害事件账本 (每周快照, 本地查)")
    explicit_only = (
        "ai_incidents: named drill only. MEASURED 2026-07-25 over 1986 recorded searches (then on the "
        "GraphQL API): timed out 760 times and reached the ranked top-k ZERO times; its domain "
        "(AI-incident registry) sits outside the queries this eye actually serves. Since 2026-10-20 it "
        "reads a local weekly snapshot (no network per query); whether to rejoin the broad sweep is "
        "open. excluded_relevant still recommends it when a query genuinely matches.")

    def __init__(self, root: Optional[Path] = None) -> None:
        self._root = root
        self._lock = threading.Lock()
        self._loaded_for: Optional[tuple] = None
        self._docs: list[Document] = []

    def _dir(self) -> Path:
        return (self._root or data_root()) / "current"

    def _load(self) -> list[Document]:
        cur = self._dir()
        try:
            real = cur.resolve(strict=True)
            key = (str(real), (real / "index.json").stat().st_mtime_ns)
        except OSError:
            return []
        with self._lock:
            if self._loaded_for == key:
                return self._docs
            try:
                records = json.loads((real / "index.json").read_text(encoding="utf-8"))
                meta = json.loads((real / "meta.json").read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("ai_incidents: local index unreadable: %s", exc)
                return []
            self._docs = [record_to_doc(r, meta) for r in records]
            self._loaded_for = key
            return self._docs

    def search(self, query: str, limit: int = 10) -> list[Document]:
        docs = self._load()
        if not docs:
            diag.note("ai_incidents.index", url=str(self._dir()),
                      exc=RuntimeError("no local AIID index yet (eye job aiid-snapshot builds it)"))
            return []
        q = (query or "").strip()
        hits = docs if not q else keyword_score_filter(docs, q)
        return [d.model_copy(deep=True) for d in hits[:max(0, int(limit))]]

    async def afetch_url(self, url: str) -> Optional[Document]:
        m = re.match(r"^https?://(?:www\.)?incidentdatabase\.ai/cite/(\d+)/?(?:[?#].*)?$", url or "")
        if not m:
            return None
        iid = m.group(1)
        for d in await anyio.to_thread.run_sync(self._load, limiter=portal.IO_LANE):  # local index read OFF loop
            if d.source_id == iid:
                return d.model_copy(deep=True)
        return None

    def health_check(self) -> tuple[Optional[bool], str]:
        """The snapshot in use and its age (stale past STALE_AFTER_DAYS), the last weekly check, and
        one HEAD of the snapshot page (the path the weekly refresh depends on)."""
        root = self._root or data_root()
        meta = current_meta(root)
        try:
            last = json.loads((root / "check.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = {}
        last_txt = ""
        if last.get("outcome") == "failed":
            last_txt = f"; last weekly check {last.get('checked_at', '?')[:10]} failed: {last.get('error', '')[:120]}"
        if not meta or not self._load():
            return False, "no local AIID snapshot index (OmniSeek job aiid-snapshot builds it)" + last_txt
        try:
            snap_at = datetime.fromisoformat(meta["date"])
        except (KeyError, TypeError, ValueError):
            return False, "local AIID snapshot has no readable date" + last_txt
        age = (datetime.now(timezone.utc) - snap_at).total_seconds() / 86400
        head = (f"snapshot {snap_at.date().isoformat()} ({meta.get('incidents')} incidents, "
                f"{meta.get('reports')} reports), {age:.1f} days old")
        if age > STALE_AFTER_DAYS:
            return False, f"STALE: {head} (> {STALE_AFTER_DAYS} days without a newer one){last_txt}"
        try:
            r = http.direct("HEAD", SNAPSHOT_PAGE, headers={"User-Agent": _UA}, timeout=20,
                            follow_redirects=True)
        except Exception as exc:  # noqa: BLE001
            return False, f"{head}; snapshot page unreachable: {type(exc).__name__}: {str(exc)[:80]}"
        if r.status_code == 429:
            return None, f"{head}; snapshot page answered HTTP 429 (rate limited, not verified)"
        if r.status_code != 200:
            return False, f"{head}; snapshot page HTTP {r.status_code}{last_txt}"
        return True, f"{head}; snapshot page HTTP 200{last_txt}"


from omniseek.core.fetcher import register_adapter  # noqa: E402

register_adapter(AIIncidentsAdapter())

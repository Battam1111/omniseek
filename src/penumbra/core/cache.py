"""Disk-based cache for source responses.

Keyed by SHA-256 of the cache key. Values are JSON. Survives restarts.
Default TTL is 15 minutes — adapters can override per-call.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from platformdirs import user_cache_dir

CACHE_DIR = Path(user_cache_dir("penumbra", appauthor=False)) / "penumbra_cache"
DEFAULT_TTL = 900  # 15 minutes

# Per-thread "fresh" flag: when set, get()/get_docs() force a cache MISS (live fetch),
# while set()/set_docs() still WRITE — so a `fresh=True` request gets live data AND
# warms the cache for later normal calls. fetcher sets this inside each search worker.
_fresh_var: contextvars.ContextVar = contextvars.ContextVar("polaris_cache_fresh", default=False)


def set_fresh(value: bool) -> None:
    _fresh_var.set(bool(value))


# Per-thread "refresh margin" (seconds): when > 0, get()/get_docs() treat an entry that
# expires within this many seconds as a MISS (return None → live refetch, which rewrites
# the cache), while a still-comfortably-warm entry is served from cache untouched. This is
# the basis of the prewarmer's refresh-if-near-expiry pass: warm a source's expensive fetch
# ONLY when its cache is missing or about to lapse, instead of re-paying the full live cost
# every cycle on data whose TTL is hours away. It targets the adapter's OWN cache keys (the
# per-PI / per-affiliation / per-source keys the warm fetch would write), because the check
# happens inside the same get() the adapter already calls. `fresh` still wins (it forces a
# miss unconditionally); margin 0 (the default) is a no-op so all normal reads are unchanged.
_refresh_margin_var: contextvars.ContextVar = contextvars.ContextVar(
    "polaris_cache_refresh_margin", default=0.0)


def set_refresh_margin(seconds: float) -> None:
    _refresh_margin_var.set(max(0.0, float(seconds)))


# Per-thread "cache-only" flag: when set, the live egresses (http._request_capped + cdp_call)
# short-circuit (return None / raise), so a search reads ONLY warm cache and does ZERO live work.
# This is the basis of the cache_only=True pickup: pick up walled results that have self-warmed after a fire,
# WITHOUT re-firing a still-cold source (no extra CDP nav / account traffic; poll-safe). The
# adapter always checks the cache BEFORE an egress, so a warm source returns normally; only a
# cache MISS hits the guarded egress and degrades to empty. fetcher sets this in each worker.
_cache_only_var: contextvars.ContextVar = contextvars.ContextVar("polaris_cache_only", default=False)


def set_cache_only(value: bool) -> None:
    _cache_only_var.set(bool(value))


def cache_only() -> bool:
    return _cache_only_var.get()


def _key_path(key: str) -> Path:
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{h}.json"


def get(key: str) -> Optional[Any]:
    """Return cached value if present and not expired, else None."""
    if _fresh_var.get():
        return None  # caller requested fresh → force a live fetch
    path = _key_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupt / half-written (e.g. a kill mid-write from before atomic writes):
        # delete it so a stable sha256 key can't decode-fail on every hit until TTL.
        try:
            path.unlink()
        except OSError:
            pass
        return None
    except OSError:
        return None  # transient read error → miss, but don't delete
    margin = _refresh_margin_var.get()
    # margin > 0 (the prewarmer's refresh-if-near-expiry pass): treat an entry within `margin`
    # seconds of expiry as a miss so the adapter refetches and rewrites it; a comfortably-warm
    # entry is served untouched. margin 0 (the default) collapses this to the plain TTL check.
    if data.get("expires_at", 0) < time.time() + margin:
        return None
    return data.get("value")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write atomically: a unique temp file in the same dir + ``os.replace`` (an atomic
    rename on POSIX & Windows same-volume). Concurrent writers → last rename wins, never
    a half-file; a kill mid-write leaves only a ``.tmp`` (never a corrupt final file)."""
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def set(key: str, value: Any, ttl: Optional[int] = None) -> None:  # noqa: A001 (shadows built-in)
    """Store a value with the given TTL (seconds)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _key_path(key)
    payload = {"expires_at": time.time() + (ttl or DEFAULT_TTL), "value": value}
    _atomic_write_text(path, json.dumps(payload, default=str, ensure_ascii=False))


def clear_expired() -> int:
    """Delete cache files whose TTL has passed. The TTL only gates READS; expired
    files otherwise sit on disk forever, so a long-lived host slowly accumulates
    dead JSON. Called at service startup (serve_http). Returns count removed."""
    if not CACHE_DIR.exists():
        return 0
    now = time.time()
    removed = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            expired = json.loads(f.read_text(encoding="utf-8")).get("expires_at", 0) < now
        except (OSError, json.JSONDecodeError):
            expired = True  # unreadable/corrupt: treat as dead
        if expired:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def clear() -> int:
    """Remove all cache entries. Returns count removed."""
    if not CACHE_DIR.exists():
        return 0
    count = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            f.unlink()
            count += 1
        except OSError:
            pass
    return count


def make_key(*parts: str) -> str:
    """Helper to build a stable cache key from parts."""
    return "|".join(str(p) for p in parts)


def seconds_until_expiry(key: str) -> Optional[float]:
    """Seconds until ``key``'s entry expires, or None if it is missing / corrupt / already
    expired. READ-ONLY (ignores the ``fresh`` and refresh-margin contextvars): it reports the
    on-disk TTL state, it does not fetch. A negative result is never returned (a lapsed entry
    is None, same as absent), so a caller can use ``v is None or v < margin`` as 'needs refresh'."""
    path = _key_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    remaining = data.get("expires_at", 0) - time.time()
    return remaining if remaining > 0 else None


# -----------------------------------------------------------------------------
# PolarisDocument convenience — the list[PolarisDocument] round-trip that ~38
# adapters were re-implementing inline (cache.get → model_validate / cache.set →
# model_dump(mode="json")). Behavior is identical; this just removes the boilerplate.
# -----------------------------------------------------------------------------


def get_docs(key: str):
    """Return a cached ``list[PolarisDocument]`` if present + fresh, else None."""
    from penumbra.core.normalize import PolarisDocument  # local import: avoid import cycle

    cached = get(key)
    if cached is None:
        return None
    return [PolarisDocument.model_validate(d) for d in cached]


def set_docs(key: str, docs, ttl: Optional[int] = None) -> None:
    """Cache a ``list[PolarisDocument]`` (JSON round-trip), with TTL."""
    set(key, [d.model_dump(mode="json") for d in docs], ttl=ttl)

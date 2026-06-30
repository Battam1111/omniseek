"""creds-doctor — report which credential-needing sources are configured.

Run:  python scripts/creds_doctor.py

Scans the live adapter registry for sources with needs_credentials=True and reports whether a
credential file exists at ~/.penumbra/credentials/<source>.json, plus whether the polite-pool
contact email is set. Read-only; NEVER prints secret values, only presence/absence.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a loose script (not just as a module).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from penumbra.core import auth, fetcher  # noqa: E402


def main() -> int:
    print(f"credential store : {auth.CREDS_DIR}")
    contact = auth.contact_email()
    placeholder = contact.endswith("example.com") or contact.endswith("example.org")
    print("contact email    : "
          + ("PLACEHOLDER (set PENUMBRA_CONTACT_EMAIL or ~/.penumbra/credentials/contact.json "
             "for the OpenAlex/Crossref/SEC polite pool)" if placeholder else "configured"))

    names = sorted(fetcher.all_adapter_names())
    need = [n for n in names
            if getattr(fetcher.get_adapter(n), "needs_credentials", False)]

    print()
    if not need:
        print("No registered source declares needs_credentials. Nothing to configure.")
        return 0

    missing = []
    print(f"{len(need)} source(s) declare needs_credentials:")
    for n in need:
        ok = auth.is_configured(n)
        print(f"  [{'  ok ' if ok else 'MISS '}] {n}")
        if not ok:
            missing.append(n)

    print()
    if missing:
        print(f"{len(missing)} missing. For each, create ~/.penumbra/credentials/<source>.json")
        print("(a <source>.json.template is dropped on that source's first import; copy + fill it).")
        return 1
    print("All credential-needing sources are configured.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

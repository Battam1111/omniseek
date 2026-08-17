"""Make the test suite import THIS working tree, whatever the invocation.

Why this file exists. `python -m unittest discover -s tests` imports every test module in
one process. Seventeen of them import omniseek without first putting `src/` on the path, so
whichever module got imported first decided which omniseek the whole suite tested. On a
machine with a stale global install (this one had 0.1.1 in site-packages while the tree was
0.2.0) the entire suite silently validated the installed package instead of the code under
review. That is the worst kind of green: a suite that passes about code nobody changed.

Discovering `tests` as a package runs this module before any test module, so the pin lands
first. `tests/` itself is re-added to the path because three test modules import their
sibling `_repo_only`, which package-ification would otherwise break.

The assertion at the bottom is the actual guard. Pinning can be defeated (a module imported
even earlier, an editable install pointing elsewhere), so we check the outcome rather than
trust the mechanism.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

for _entry in (str(_HERE), str(_ROOT / "bench"), str(_ROOT / "scripts"), str(_ROOT / "src")):
    if _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)

import omniseek as _omniseek  # noqa: E402

_loaded = Path(_omniseek.__file__).resolve()
if _ROOT not in _loaded.parents:
    raise RuntimeError(
        "the test suite loaded omniseek from "
        f"{_loaded}, which is outside this repository ({_ROOT}). "
        "Tests would report on that copy rather than on the working tree. "
        "Uninstall the shadowing package, or run the suite in a venv where "
        "omniseek is installed editable from this tree."
    )

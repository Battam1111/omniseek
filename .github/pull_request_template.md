## What this changes

<!-- One or two sentences. Link the issue it closes, if any. -->

## Type

- [ ] New source
- [ ] Fix a decayed / broken source
- [ ] Bug fix (not a source)
- [ ] Docs / i18n
- [ ] Other

## Gate

- [ ] `python tests/smoke.py` passes locally.
- [ ] `python -m compileall src` is clean.
- [ ] No em-dash in human-facing text (colon, comma, period, or parens instead).

## If this adds a source

- [ ] It beats plain web search via a MODE: <!-- structure / unwall / transcribe / recall / monitor --> and the PR description says how.
- [ ] Facets are declared as class attrs (`kind`, `domains`, `regions`, `modes`); I did NOT hand-edit `facets.json`.
- [ ] I added an offline golden fixture to `tests/smoke.py` (recorded raw payload to expected doc shape) and fabricated no fields.
- [ ] If walled: it is marked `explicit_only` (and `needs_credentials` if it needs a logged-in session), kept out of the default pack, with its legal posture noted (SECURITY.md). Sources that require defeating an access control are not accepted.

## If this touches more than one language README

- [ ] README.md, docs/i18n/README_zh.md, and docs/i18n/README_ja.md stay in parity.

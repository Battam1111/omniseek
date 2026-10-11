# Brand

<sub>[OmniSeek](../README.md)&nbsp;·&nbsp;[Configuration](configuration.md)&nbsp;·&nbsp;[Tools](tools.md)&nbsp;·&nbsp;[Patterns](patterns.md)&nbsp;·&nbsp;[Sites behind a login](walled-sources.md)&nbsp;·&nbsp;**Brand**</sub>

The single canonical reference for OmniSeek's visual identity. Keep it locked so the look does not drift as contributors add diagrams, badges, or assets.

## Name

**OmniSeek**, capital O and capital S, in prose, wordmark, and titles. Code identifiers are always lowercase: the package `omniseek`, the `omniseek_*` tools, paths, the docker service.

## Tagline

`Give your AI agent the parts of the internet web search doesn't reach.`

Chinese: `给你的 AI Agent 接上网页搜索够不到的那部分互联网。` Japanese (working rendering, pending native review): `ウェブ検索が届かないインターネットを、あなたの AI エージェントに。`

Use it as written on the README first screen, the site, and the localized READMEs. The PyPI summary is a separate one-liner kept in `pyproject.toml` (`description`); `server.json` carries a shortened form of it, because the MCP Registry caps that description at 100 characters.

### Retired taglines

Do not use these on any new surface. They stay listed here so old screenshots and posts can be recognized.

- `Your agent seeks what search can't find.` (Chinese `让你的 Agent 找到难以触及之物。`, Japanese `検索では届かないものを、あなたのエージェントが探し当てる。`), retired in the front-page rework (see the Unreleased section of the CHANGELOG).

## Palette

The blue ramp is depth: the further in, the deeper the shade. The single amber point is what you found there.

- Shallow blue: `#93C5FD` (outermost ring)
- Primary blue: `#3B82F6` (middle ring; the accent for badges and links)
- Deep blue: `#1D4ED8` (innermost ring on light backgrounds)
- Deep blue, dark-mode: `#2563EB` (innermost ring on dark backgrounds; `#1D4ED8` loses contrast against navy)
- Navy field: `#0F172A` (dark backgrounds, the hero and social card)
- Amber find: `#F59E0B` (the center point, and only the center point; a thread of lineage from the project's earlier palette)

Text and lines use no other colours. On light backgrounds text is navy, on dark backgrounds white; secondary text, hairlines and tinted surfaces are navy, white or a brand blue at lower opacity (for example `rgba(15,23,42,.62)` for muted text on white). No separate greys. Amber is never set as body text on a light background, where it is too faint to read.

## Mark

**The deep O.** Three concentric rings, each with one opening, the openings rotated layer by layer: the way in exists, but it is not a straight line. That is seeking, as opposed to searching. The amber point at the center is the thing found at depth. Read as a whole, the rings and the point are also an iris and a pupil: the mark quietly remains an eye without saying so.

The mark carries the sharpest claim (depth, the way in, the find). The name carries the breadth (Omni). Together the lockup says both; do not try to draw the whole catalog into the mark, and never put a source count in brand copy (the catalog grows; a number rots).

### Geometry (canonical, from `assets/logo.svg`)

ViewBox `0 0 200 200`, center `(100, 100)`, all `stroke-linecap="round"`.

| Ring | Radius | Stroke | Dash (arc gap) | Rotation |
|------|--------|--------|----------------|----------|
| Outer | 78 | 16 | `416 74` | `-55°` |
| Middle | 50 | 16 | `267 47` | `75°` |
| Inner | 23 | 15 | `116 28` | `195°` |
| Point | r=8 fill | | | |

The SVG masters are the single source of truth for the mark; the logo and social rasters are rendered from them, and the demo figures, comparison GIFs and infographics from the HTML masters and scripts in `assets/masters/`. Edit a master, re-render, never retouch a PNG or GIF.

## Asset inventory

- `assets/logo.svg`: the mark, light-background variant. Canonical master.
- `assets/logo-dark.svg`: the mark, dark-background variant (inner ring lifted one stop).
- `assets/logo-icon.png`: 1024px render of `logo.svg` on transparent; the standalone light-background mark.
- `assets/logo-hero-dark.png`: 1024px render of `logo-dark.svg` on transparent; the standalone dark-background mark.
- `assets/logo-hero.png`: the dark-variant mark centered on the navy field (source hero render).
- `assets/logo-lockup.png`: the README header (mark and wordmark, no text line), one file for both GitHub themes: GitHub picks a `<picture>` source by the system colour scheme, not by the page theme the reader chose, so a light/dark pair can land on the wrong page; these blues read on white and on GitHub's dark page alike. The tagline sits under it as text, in each language. Generated and rendered by `assets/masters/gen_hero.py`.
- `assets/hero-{en,zh,ja}-{light,dark}.png`: the earlier README hero lockups (mark, wordmark, tagline with the amber full stop), per language and theme, kept for reference and no longer shown; same script. The scripts write into `assets/masters/` (ignored by git); copy the renders up into `assets/`.
- `assets/demo-{en,zh,ja}-{light,dark}.png`: the README demo figure, per language and theme; generated by `assets/masters/gen_demo.py` and rendered by `assets/masters/render_demo.py`.
- `assets/demo-en-{light,dark}-narrow.png`: a 400 px column cut of the English demo figure, same words and colours, made for phone screens; no page shows it now (the comparison figures below took its place); same generator and renderer.
- `assets/demo/compare-{en,zh}-{light,dark}.gif`: the comparison GIF, one real question put to an agent with web search only and to an agent with OmniSeek, side by side. The READMEs show it under "See the difference" (English and Japanese use `en`, Chinese uses `zh`) and the site home page shows the English one, switched by theme (see Rules for how). `-narrow` versions stack the two columns for screens under 480 px wide. Frame 0 is the finished comparison (both answers, 1:21:51 highlighted, no end card) for 3 seconds, because GitHub, PyPI, screenshots, thumbnails and reduced-motion readers often show only the first frame; then it replays from the question with each agent's first call already on screen, so no frame shows two empty columns. Every line on screen comes from the recorded run in `assets/masters/compare-demo-run.json` (and `compare-demo-transcript.json` for the transcript lines); `assets/masters/gen_compare_demo.py` renders them (`gif` for the GIFs, `all` also for the videos and posters). Re-render instead of editing frames.
- `assets/demo/compare-{en,zh}-dark.mp4` and `compare-{en,zh}-dark-poster.png`: the same run as a 38-second video, which also shows the video player opened at the quoted minute; linked from `docs/compare.md` and `docs/compare.zh.md` and played in a fold on the site home page; same script.
- `assets/demo/compare-table-{en,zh}-{light,dark}.png` and `-narrow` versions: the infographic of all three recorded questions, in `docs/compare.md`, `docs/compare.zh.md` and on the site home page (narrow under 600 px); rendered by `assets/masters/gen_compare_infographic.py` from the same record, quotes cut verbatim.
- `assets/terminal-demo-{light,dark}.gif`: the earlier README terminal recording (one real run searching Zhihu and Bilibili, saved in `assets/masters/terminal-demo-run.json`, rendered by `assets/masters/gen_terminal_demo.py`). The comparison GIF replaced it in the READMEs and on the site; the files stay in the repo for reference.
- `assets/social-preview.png`: the 1280x640 link-share card; generated and rendered by `assets/masters/gen_social.py`. Upload under Settings > General > Social preview; committing it does not apply it.
- `bench/funnel-s3/funnel-{light,dark}.svg`: the S3 rebuild funnel figure; rendered by `bench/funnel-s3/render_funnel.py` with the same palette tokens as the results chart.

## Rules

- All shields.io badges use color `3B82F6` and `style=flat-square`.
- No em-dash on any human-facing surface (CI-enforced by `scripts/brand_lint.py`).
- Every README or site page that loads a brand image from raw.githubusercontent.com ends the URL with `?v=YYYY-MM-DD` (CI-enforced by `scripts/brand_lint.py`). GitHub's image proxy keeps showing the old picture under an unchanged URL, so when you re-render an image, set the date to the day of the re-render in every URL that points at it. A second re-render on a day that already has a version adds one lowercase letter: `?v=2026-10-11b`, then `c`.
- On GitHub pages (the READMEs, `docs/compare.md`, `docs/compare.zh.md`), an image that comes in a light and a dark version follows the theme the reader picked on GitHub, not the computer's: one `<picture>` per theme, each inside a link, every URL ending in the theme fragment after the version:

  ```html
  <a href="…/compare-en-light.gif?v=…#gh-light-mode-only"><picture>
    <source media="(max-width: 480px)" srcset="…/compare-en-light-narrow.gif?v=…#gh-light-mode-only">
    <img src="…/compare-en-light.gif?v=…#gh-light-mode-only" width="720" alt="…">
  </picture></a>
  ```

  and the same with `dark`. GitHub's stylesheet hides a link whose address ends in `#gh-dark-mode-only` on a light page, and the reverse; the `<source>` only picks the width. The link is needed because GitHub does not put an image inside `<picture>` into a link of its own. Never write the theme into a `<source media>` on GitHub: for a reader with a fixed theme, GitHub's script rewrites theme conditions and drops whatever else the condition held, which showed the phone version on a desktop. PyPI ignores the fragments and would show both versions, so the PyPI page is `docs/pypi.md` (`readme` in `pyproject.toml`): `scripts/gen_pypi_readme.py` writes it from the English README's first screen and "See the difference" section with the light images only, and `scripts/brand_lint.py` fails when it falls behind README.md. Edit README.md, then run the script; never edit `docs/pypi.md` by hand. The site follows the computer's theme and keeps plain `<picture>` with `prefers-color-scheme`.
- No personal information appears in any brand asset.
- Describe the product in plain words, starting from what the reader already knows: an add-on for AI assistants like Claude Code, Cursor, and Codex that lets them search where web search stops. Say MCP last, with a plain line on what it is; give words like self-hosted, stdio, and transcription a common phrase or a plain gloss the first time. Internal project vocabulary stays out of human-facing text; the words below are retired from the README, docs, site, and package metadata (code identifiers and file names that already use them are left alone):
  perception, senses, walled, unwalling, drill, tier, curator, evidence graph, explicit-only, keyed, Apache-clean.
  Say instead, for example: "sites behind your own login" (walled), "off by default, turned on by name" (explicit-only), "needs an API key" (keyed), "citation graph" (evidence graph).
- Do not write the number of sources as an exact count; say "200+".

---

<div align="center"><sub><a href="../README.md">← back to the README</a></sub></div>

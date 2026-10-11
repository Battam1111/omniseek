"""Contrast check for the comparison GIF/video frames and the infographic: every piece of text against
the card it sits on, measured in the rendered page (computed colours, opacity and backgrounds, the
way Chromium paints them), in every state of the storyboard and both themes.

    python assets/masters/check_contrast.py              # report, exit 1 if anything is below its floor
    python assets/masters/check_contrast.py --masters D  # measure the generators in directory D instead

Floors (WCAG 2 contrast ratio): body text 4.5:1. Text that is meant to step back is 3:1: the steps
that are no longer current (dimmed) except their main line, the install note under the right column,
the "real runs" line on the end card and the infographic's footer. The main line of a dimmed step
(the answer itself) stays at 4.5:1, because the finished frame that most readers see first dims the
left column's answers. Text in the faded strip at the top of a column (it scrolls away) is not
counted.

Needs playwright (with its Chromium). Prints the lowest ratio per kind of text.
"""
import argparse
import importlib
import json
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BODY, SOFT = 4.5, 3.0

JS = r"""
(stageSel) => {
  const stage = document.querySelector(stageSel);
  const out = [];
  const walker = document.createTreeWalker(stage, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (seen.has(el)) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    let hidden = false;
    for (let e = el; e; e = e.parentElement) {
      const s = getComputedStyle(e);
      if (s.display === 'none' || s.visibility === 'hidden') { hidden = true; break; }
    }
    if (hidden) continue;
    const feed = el.closest('.feed');
    if (feed && r.bottom < feed.getBoundingClientRect().top + 34) continue;   // faded strip
    const chain = [];
    for (let e = el; e && e !== stage.parentElement; e = e.parentElement) {
      const s = getComputedStyle(e);
      chain.unshift({bg: s.backgroundColor, op: parseFloat(s.opacity)});
    }
    out.push({text: n.textContent.trim().slice(0, 40), color: getComputedStyle(el).color, chain,
              tags: [...(function* () { for (let e = el; e && e !== stage; e = e.parentElement) yield (e.className || '').toString(); })()]});
  }
  return out;
}
"""


def parse(c: str):
    """CSS colour string from getComputedStyle -> (r, g, b, a) with r, g, b in 0..1."""
    c = c.strip()
    m = re.fullmatch(r"rgba?\(([^)]+)\)", c)
    if m:
        p = [x.strip() for x in m.group(1).replace("/", ",").split(",")]
        r, g, b = (float(x) / 255 for x in p[:3])
        a = float(p[3]) if len(p) > 3 else 1.0
        return r, g, b, a
    m = re.fullmatch(r"color\(srgb ([^)]+)\)", c)
    if m:
        p = m.group(1).replace("/", " ").split()
        r, g, b = (float(x) for x in p[:3])
        a = float(p[3]) if len(p) > 3 else 1.0
        return r, g, b, a
    raise ValueError(f"colour not understood: {c!r}")


def over(src, dst):
    a = src[3]
    return tuple(src[i] * a + dst[i] * (1 - a) for i in range(3))


def paint(chain, k, under, text_color):
    """The pixel colour after painting chain[k:] over `under`; text_color None = background only."""
    node = chain[k]
    c = over(parse(node["bg"]), under)
    if k == len(chain) - 1:
        if text_color is not None:
            c = over(parse(text_color), c)
    else:
        c = paint(chain, k + 1, c, text_color)
    op = node["op"]
    if op < 1:
        c = tuple(c[i] * op + under[i] * (1 - op) for i in range(3))
    return c


def lum(c):
    lin = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a, b):
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


SECONDARY = {"call", "lbl", "gl", "badge", "fn", "at"}


def kind(item, page_kind):
    classes = [t.split() for t in item["tags"]]          # the text's element first, out to the stage
    flat = {c for cs in classes for c in cs}
    own = next((c for cs in classes for c in cs if c not in ("mono", "h")), "text")
    if page_kind == "table":
        return ("soft", "footer") if "foot" in flat else ("body", own)
    if "extra" in flat:
        return "soft", "install note"
    if "run" in flat:
        return "soft", "end card: real runs"
    step = next((i for i, cs in enumerate(classes) if "step" in cs), None)
    if step is not None and "dim" in classes[step]:
        if any(c in SECONDARY for cs in classes[:step] for c in cs):
            return "soft", f"dimmed step, {own}"
        return "body", f"dimmed step main line, {own}"
    return "body", own


def measure(masters: Path, langs):
    sys.path.insert(0, str(masters))
    D = importlib.import_module("gen_compare_demo")
    I = importlib.import_module("gen_compare_infographic")
    from playwright.sync_api import sync_playwright
    worst = {}
    with tempfile.TemporaryDirectory() as td, sync_playwright() as p:
        b = D.browser(p)
        for theme in ("light", "dark"):
            page_rgb = tuple(int(D.THEMES[theme]["page"][i:i + 2], 16) / 255 for i in (1, 3, 5))
            for lang in langs:
                if lang not in D.T:
                    continue
                ctx = b.new_context(viewport={"width": D.W + 20, "height": D.GIF_H + 20})
                pg = ctx.new_page()
                f = Path(td) / "demo.html"
                f.write_text(D.page_html(lang, theme, D.W, D.GIF_H, False), encoding="utf-8")
                pg.goto(f.as_uri())
                pg.wait_for_timeout(300)
                for i, (st, _) in enumerate(D.plan(False)):
                    pg.evaluate(D.APPLY, [st, D.T[lang]["cap"]])
                    for item in pg.evaluate(JS, "#stage"):
                        bg = paint(item["chain"], 0, page_rgb, None)
                        fg = paint(item["chain"], 0, page_rgb, item["color"])
                        k, label = kind(item, "demo")
                        r = ratio(fg, bg)
                        key = ("demo", theme, k, label)
                        if key not in worst or r < worst[key][0]:
                            worst[key] = (r, item["text"], f"{lang} state {i}")
                ctx.close()
                if lang not in I.T:
                    continue
                ctx = b.new_context(viewport={"width": I.WIDE + 20, "height": 800})
                pg = ctx.new_page()
                f = Path(td) / "table.html"
                f.write_text(I.page(lang, theme, I.WIDE, False), encoding="utf-8")
                pg.goto(f.as_uri())
                pg.wait_for_timeout(300)
                for item in pg.evaluate(JS, "#stage"):
                    bg = paint(item["chain"], 0, page_rgb, None)
                    fg = paint(item["chain"], 0, page_rgb, item["color"])
                    k, label = kind(item, "table")
                    r = ratio(fg, bg)
                    key = ("table", theme, k, label)
                    if key not in worst or r < worst[key][0]:
                        worst[key] = (r, item["text"], lang)
                ctx.close()
        b.close()
    return worst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--masters", type=Path, default=HERE)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--langs", default="en,zh,ja")
    a = ap.parse_args()
    worst = measure(a.masters.resolve(), a.langs.split(","))
    bad = 0
    print(f"{'figure':6} {'theme':5} {'kind':4} {'floor':>5} {'lowest':>6}  text kind (where)")
    for (fig, theme, k, label), (r, text, where) in sorted(worst.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[1][0])):
        floor = BODY if k == "body" else SOFT
        ok = r >= floor
        bad += not ok
        print(f"{fig:6} {theme:5} {k:4} {floor:5.1f} {r:6.2f}{'' if ok else ' LOW'}  {label}: {text!r} ({where})")
    if a.json:
        a.json.write_text(json.dumps({" | ".join(k): v for k, v in worst.items()}, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    print(f"\n{bad} below the floor" if bad else "\nall text at or above its floor")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

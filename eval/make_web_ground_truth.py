"""Build a FULL ground truth for the two Wikipedia pages (the old files had only 9 and 19 elements).

    python eval/make_web_ground_truth.py        # needs data/web_cache/wikipedia_*.html (run_baselines downloads them)
    python eval/check_ground_truth.py           # re-check, then look at the counts printed here

HOW IT IS MADE (say this to your evaluator):
  The truth of a web page's structure is its own HTML markup, so the elements are read from the page itself, not
  from any extraction tool's output:
    headings    the page title (<h1>) and every <h2>/<h3>/<h4> of the article
    key_values  every label/value row of the infobox (<th> label, <td> value)
    tables      the data tables (class "wikitable"), rows whose cells are short
    images      every figure with a caption (keywords from the caption) and every infobox picture of real size
                (keywords from its alt text). Tiny icons and flag glyphs in tables are not content images.
    text        the first sentence of evenly spaced paragraphs (a sample: it cannot be exhaustive)
  Citation markers such as [14] are removed. Elements are written in document (reading) order.
The old files are copied to eval/ground_truth_old/ first.
"""
import json
import re
import shutil
import sys
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "web_cache"
OUT = ROOT / "eval" / "ground_truth"
PAGES = ["wikipedia_python", "wikipedia_india"]
MAX_TEXT, MAX_TABLES, MAX_KV = 30, 6, 30      # images and headings: ALL of them
STOP = set("the and with from that this which were their other during after about between into over under "
           "also have been more than most such some many first".split())


def clean(el):
    """Visible text of an element with spacing normalised."""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).replace(" ,", ",").replace(" .", ".").strip()


def evenly(items, n):
    return items if len(items) <= n else [items[int(i * len(items) / n)] for i in range(n)]


def build(name):
    soup = BeautifulSoup((CACHE / f"{name}.html").read_text(encoding="utf-8"), "html.parser")
    for e in soup(["script", "style", "noscript"]):
        e.decompose()
    for sup in soup.find_all("sup"):                                  # [14], [a], [citation needed]
        if re.fullmatch(r"\[\s*[\w\s.]{1,30}\s*\]", sup.get_text(strip=True)):
            sup.decompose()
    h1 = soup.find("h1")                       # the title is outside the article body, so read it first
    title = clean(h1) if h1 else ""
    # keep everything a reader sees inside the article (dates like "; 8 days ago" are part of the infobox value)
    for e in soup.select(".mw-editsection, .navbox, .reflist, .refbegin, .mw-references-wrap"):
        e.decompose()
    root = soup.select_one("#mw-content-text") or soup.select_one("main") or soup.body or soup

    heads, kvs, tables, figs, texts = [], [], [], [], []
    if title:
        heads.append((-1, {"type": "heading", "page": 1, "text": title}))
    for pos, node in enumerate(root.find_all(["h2", "h3", "h4", "p", "table", "figure", "img"])):
        n = node.name
        if n in ("h2", "h3", "h4"):
            t = clean(node)
            if t and t.lower() != "contents":
                heads.append((pos, {"type": "heading", "page": 1, "text": t}))
        elif n == "table":
            if node.find_parent("table") is not None:
                continue
            cls = " ".join(node.get("class", []))
            if "infobox" in cls:
                for tr in node.find_all("tr"):
                    cells = tr.find_all(["th", "td"], recursive=False)
                    if len(cells) == 2 and cells[0].name == "th" and cells[1].name == "td":
                        k, v = clean(cells[0]), clean(cells[1])
                        if k and v and len(k) <= 40 and len(v) <= 200:
                            kvs.append((pos, {"type": "key_value", "page": 1, "key": k, "value": v}))
            elif "wikitable" in cls:
                rows = []
                for tr in node.find_all("tr"):
                    cells = [clean(c) for c in tr.find_all(["th", "td"], recursive=False)]
                    if cells and any(cells) and all(len(c) <= 200 for c in cells):
                        rows.append(cells)
                if len(rows) >= 2:
                    tables.append((pos, {"type": "table", "page": 1, "cells": rows[:15]}))
        elif n in ("figure", "img"):
            # A content image is a figure OR a stand-alone <img> (e.g. the infobox logo) that is not an icon
            # (same 40 px limit as the engine and your Scope 1 scraper). Keywords come from the caption when there
            # is one (15+ characters), otherwise from the alt text; with neither, the image is kept with no keywords.
            img = node if n == "img" else node.find("img")
            if n == "img" and node.find_parent("figure") is not None:
                continue                                              # already counted through its figure
            if img is None or any(str(img.get(k, "99")).isdigit() and int(img.get(k, 99)) < 40
                                  for k in ("width", "height")):
                continue
            cap = node.find("figcaption") if n == "figure" else None
            text = clean(cap) if cap else ""
            if len(text) < 15:
                text = (img.get("alt") or "").strip()
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{4,}", text) if w.lower() not in STOP]
            kw = sorted(set(words), key=lambda w: (-len(w), w))[:3]
            figs.append((pos, {"type": "image", "page": 1, "caption_keywords": kw}))
        elif n == "p" and not node.find_parent(["table", "figure", "li"]):
            t = clean(node)
            if len(t) >= 120:
                first = re.split(r"(?<=[.!?])\s+(?=[A-Z])", t)[0]
                if 60 <= len(first) <= 300 and "[" not in first:
                    texts.append((pos, {"type": "text", "page": 1, "text": first}))

    chosen = (heads + kvs[:MAX_KV] + evenly(tables, MAX_TABLES) + figs + evenly(texts, MAX_TEXT))
    chosen.sort(key=lambda x: x[0])
    return [e for _, e in chosen]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    old = ROOT / "eval" / "ground_truth_old"
    old.mkdir(exist_ok=True)
    for name in PAGES:
        if not (CACHE / f"{name}.html").exists():
            print(f"{name}: data/web_cache/{name}.html missing. Run scripts/run_baselines.py once to download it.")
            continue
        target = OUT / f"{name}.json"
        if target.exists() and not (old / target.name).exists():
            shutil.copy(target, old / target.name)
        els = build(name)
        target.write_text(json.dumps({"doc": f"{name}.html", "elements": els}, indent=1, ensure_ascii=False),
                          encoding="utf-8")
        counts = {}
        for e in els:
            counts[e["type"]] = counts.get(e["type"], 0) + 1
        print(f"{name}: {len(els)} elements {counts}")


if __name__ == "__main__":
    sys.exit(main())

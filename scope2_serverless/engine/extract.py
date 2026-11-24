"""Scope 2 extraction engine.

  --level standard   rules only (no AI, fastest)
  --level accurate   rules + AI only on the elements the rules are unsure about (formulas, weak tables)

Every element has: type, page, content_markdown, confidence (0-1), method, flags, bbox (PDF only).
Elements with confidence < LOW are flagged "needs_ai".

    python extract.py paper.pdf out.md --level accurate
    python extract.py https://en.wikipedia.org/wiki/India out.md
"""
import argparse
import json
import os
import re
import time
import unicodedata
from collections import Counter
from pathlib import Path

import pdfplumber

LOW = 0.6
BOLD = re.compile(r"Bold|Medi|Black|-Bd|BX|TB|Semibold", re.I)
MATH_FONT = re.compile(r"Math|CMMI|CMSY|CMEX|MSBM|txsys|txexs|txmi|STIX|Symbol|Cambria", re.I)
NUM_HEAD = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+[A-Z]")
CAPTION = re.compile(r"^(Figure|Fig\.|Table)\s*(\d+)\s*[:.]")
# pseudocode boxes: "Algorithm 1 NOMAD-RL", then "Require:", numbered steps "1: for ... do", "end for"
ALGO = re.compile(r"^(Algorithm|Procedure|Pseudocode)\s+\d+\b")
ALGO_LINE = re.compile(r"^(\d{1,3}\s*:|Require|Ensure|Input|Output|Return|Parameters?\b|end\b)", re.I)
EQ_LINE = re.compile(r"\((\d+)\)\s*$")
# Key-value detection is by STRUCTURE and VALUE TYPE, never by a fixed list of labels:
#   1. a short label followed by a typed value at the end of the line (URL, e-mail, DOI, ISBN, ISSN)
#   2. "Label: value" with a short label (front page only)
#   3. a short heading followed by a one-line comma-separated list (see keyvalue_from_label_lines)
TYPED_VALUE = re.compile(
    r"(https?://\S+|www\.\S+|[\w.+-]+@[\w-]+\.[\w.-]+|\b10\.\d{4,9}/\S+"
    r"|\b97[89]-\d{1,5}-\d{2,7}-\d{2,7}-\d\S*|\b\d{4}-\d{3}[\dXx]\b)")
KV_COLON = re.compile(r"^([A-Z][A-Za-z0-9 ./-]{1,30}):\s+(\S.*)$")
REF_HEADING = re.compile(r"^(references|bibliography|works cited|literature cited)$", re.I)


def kv_from_line(t, allow_colon):
    """Return (key, value, confidence) for a line that looks like a label + value, else None."""
    words = t.split()
    if len(words) > 14:
        return None
    m = TYPED_VALUE.search(t)
    if m and not t[m.end():].strip(" .,;)"):
        key = t[:m.start()].strip(" :-\u2013\u2014")
        if (key and 1 <= len(key.split()) <= 8 and len(re.findall(r"[A-Za-z]", key)) >= 3
                and not re.search(r"[.!?]\s", key) and key.count(",") < 2):    # a label is not a sentence/citation
            return key, t[m.start():].strip().rstrip(".,;"), 0.75
    if allow_colon:
        c = KV_COLON.match(t)
        if c and len(c.group(1).split()) <= 4:
            return c.group(1).strip(), c.group(2).strip(), 0.65
    return None


# ---------------------------------------------------------------------------------------------------------------
# ROUTER: which method handles which element type.  Every entry is backed by the Scope 1 / Scope 2 comparison
# (eval/evaluate.py on the ground truth; see eval/routing_report.py for the table that is printed from the scores).
#   rules      pdfplumber geometry + fonts (PDF) / BeautifulSoup structure (web), with our own confidence
#   pypdf      an independent second reader of the PDF text layer, used to CROSS-CHECK the text the rules produced
#   ai         a vision model (Amazon Bedrock) used only in the "accurate" level, only where the rules are unsure
# Each provider can be switched off WITHOUT editing code, to measure "with and without" (task 3 evidence):
#   set ENGINE_ROUTE_FORMULA=rules      -> formulas are NOT sent to the AI
#   set ENGINE_ROUTE_TABLE=rules        -> weak tables are NOT sent to the AI
#   set ENGINE_ROUTE_KEY_VALUE=rules    -> page-1 key-values are NOT sent to the AI
#   set ENGINE_ROUTE_TEXT=rules         -> no pypdf cross-check
#   set ENGINE_ROUTE_PARAGRAPH=rules    -> paragraphs the witness tools disagree on are NOT re-read by the AI
#   set ENGINE_ROUTE_INLINE_MATH=rules  -> paragraphs with inline math are flagged but NOT rewritten by the AI
#   set ENGINE_ROUTE_QUALITY_CHECK=none -> no AI quality check on the AI-repaired formulas, tables and paragraphs
# ---------------------------------------------------------------------------------------------------------------
ROUTER = {
    "heading":   ["rules"],            # rules 1.00  vs docling 0.81, opensource 0.63, azure 0.60, markitdown 0.23
    "text":      ["rules", "pypdf"],   # rules 0.80  vs opensource 0.70, docling 0.65, azure 0.60, markitdown 0.15
    "table":     ["rules", "ai"],      # rules 0.90  vs opensource 0.50, docling 0.43, markitdown 0.31, azure 0.00
    "formula":   ["rules", "ai"],      # rules detect (math font) ; azure 0.33, docling 0.00 -> LaTeX needs the AI
    "key_value": ["rules", "ai"],      # rules 1.00  vs azure 0.92, docling 0.62, markitdown 0.77
    "image":     ["rules"],            # rules 1.00 = docling 1.00 = opensource 1.00 (Docling is 100x slower)
    # a paragraph the witness tools disagree on ("low_agreement"): the AI re-reads it from its crop (plain text)
    "paragraph": ["ai"],
    # inline math inside a paragraph (J_theta(xi), CO_2): the text layer drops sub/superscripts, so the rules only
    # DETECT it (math-font characters) and flag the paragraph; at accurate the AI rewrites it with inline LaTeX
    "inline_math": ["rules", "ai"],
    "quality_check": ["ai"],           # second AI call that re-reads the PDF crop and judges what the first AI wrote
}


def routed(kind, provider):
    env = os.environ.get("ENGINE_ROUTE_" + kind.upper())
    chosen = [x.strip() for x in env.split(",")] if env else ROUTER[kind]
    return provider in chosen


def effective_router():
    return {k: [p for p in ("rules", "pypdf", "ai") if routed(k, p)] for k in ROUTER}


def new_el(typ, page, text, conf, method, flags=None, **extra):
    flags = list(flags or [])
    if conf < LOW and "needs_ai" not in flags:
        flags.append("needs_ai")
    d = {"type": typ, "page": page, "content_markdown": text,
         "confidence": round(conf, 2), "method": method, "flags": flags}
    d.update(extra)
    return d


def read_words(page):
    """Words with their font size and font name. pdfplumber would START A NEW WORD whenever the font attributes
    change, which splits "67.9%" into "67" "." "9%" (the point is a slightly different glyph) and scrambles the
    order. So words are cut by position only, and size / font are taken from the characters inside each word."""
    out = []
    for w in page.extract_words(x_tolerance=1.5, y_tolerance=2, extra_attrs=["upright"], return_chars=True):
        chars = w.pop("chars")
        sizes = sorted(c["size"] for c in chars)
        w["size"] = sizes[len(sizes) // 2]
        w["fontname"] = Counter(c["fontname"] for c in chars).most_common(1)[0][0]
        out.append(w)
    return out


# ---------- geometry helpers ----------
def group_lines(words):
    """Words -> lines. Words are matched on their BOTTOM edge (the baseline), not their top: digits, "%" and math
    letters have taller boxes than the text around them, so their top sits 1-2 pt higher and "70.4%" used to start
    a line of its own. A word joins the current line when its bottom is within half a font size of the line's main
    text (its biggest word), so subscripts stay in their line too."""
    words = sorted(words, key=lambda w: (w["bottom"], w["x0"]))
    lines, cur, ref = [], [], None
    for w in words:
        if cur and w["bottom"] - ref["bottom"] > 0.5 * max(ref["size"], w["size"]):
            lines.append(cur)
            cur, ref = [], None
        cur.append(w)
        if ref is None or w["size"] > ref["size"] + 0.5:
            ref = w
    if cur:
        lines.append(cur)
    lines = [sorted(l, key=lambda w: w["x0"]) for l in lines]
    return sorted(lines, key=lambda l: min(w["top"] for w in l))


def line_info(ws):
    n = sum(len(w["text"]) for w in ws) or 1
    math = sum(len(w["text"]) for w in ws if MATH_FONT.search(w["fontname"])) / n
    # math-font LETTERS/DIGITS (J, xi, CO, R2): these carry the sub/superscripts the text layer loses.
    # Pure symbols in a math font (arrows, approx, bullets) come through intact and are not counted.
    math_alnum = sum(sum(ch.isalnum() for ch in w["text"]) for w in ws if MATH_FONT.search(w["fontname"]))
    bf = [bool(BOLD.search(w["fontname"])) for w in ws]
    lead = 0
    while lead < len(bf) and bf[lead]:
        lead += 1
    return {"words": [w["text"] for w in ws], "lead_bold": lead, "all_bold": all(bf),
            "text": " ".join(w["text"] for w in ws),
            "size": sum(w["size"] for w in ws) / len(ws),
            "bold": bool(BOLD.search(ws[0]["fontname"])), "math": math, "math_alnum": math_alnum,
            "x0": ws[0]["x0"], "x1": ws[-1]["x1"],
            "top": min(w["top"] for w in ws), "bottom": max(w["bottom"] for w in ws)}


def order_lines(page, words):
    """Lines in reading order. Detects 2-column pages (gutter in the middle)."""
    mid = page.width / 2
    raw = group_lines(words)
    def spans(l):
        """A line runs across the middle of the page: a word crosses it, or the words on both sides are only a
        normal word space apart (in a real two-column layout a wide gutter separates them)."""
        if any(w["x0"] < mid - 3 and w["x1"] > mid + 3 for w in l):
            return True
        left = [w["x1"] for w in l if (w["x0"] + w["x1"]) / 2 < mid]
        right = [w["x0"] for w in l if (w["x0"] + w["x1"]) / 2 >= mid]
        size = max((w.get("size", 10) for w in l), default=10)
        return bool(left and right) and min(right) - max(left) < 0.8 * size

    spanning = [spans(l) for l in raw]
    two_col = sum(spanning) / max(len(raw), 1) < 0.35
    if not two_col:
        return [line_info(l) for l in raw], False
    out, left_band, right_band = [], [], []
    left_words, right_words = [], []

    def header_cut(lb, rb):
        """Short blocks side by side at the top (e.g. two author boxes) followed by a gap in BOTH columns: read the
        left block, then the right block, then the columns below. Index of the cut in each column, or None."""
        if abs(lb[0]["top"] - rb[0]["top"]) > 3:               # the two blocks must start side by side
            return None

        def gaps(col):
            return [(k, col[k - 1]["bottom"], col[k]["top"]) for k in range(1, min(len(col), 9))
                    if col[k]["top"] - col[k - 1]["bottom"] > 0.6 * col[k]["size"]]
        for kl, l0, l1 in gaps(lb):
            for kr, r0, r1 in gaps(rb):
                if max(l0, r0) < min(l1, r1):               # the two gaps overlap: one horizontal white band
                    return kl, kr
        return None

    def flush():
        # each column is grouped into lines ON ITS OWN: the two columns' baselines are offset, and grouping across
        # the page could cut a line of one column in two (a number ending up on the line above).
        left_band.extend(line_info(l) for l in group_lines(left_words))
        right_band.extend(line_info(l) for l in group_lines(right_words))
        left_words.clear()
        right_words.clear()
        cut = header_cut(left_band, right_band) if left_band and right_band else None
        if cut:
            out.extend(left_band[:cut[0]])
            out.extend(right_band[:cut[1]])
            del left_band[:cut[0]]
            del right_band[:cut[1]]
        out.extend(left_band)
        out.extend(right_band)
        left_band.clear()
        right_band.clear()

    for l, sp in zip(raw, spanning):
        if sp:
            flush()
            out.append(line_info(l))
        else:
            for w in l:
                (left_words if (w["x0"] + w["x1"]) / 2 < mid else right_words).append(w)
    flush()
    return out, True


def boxes_of(lines, mid=None):
    """Boxes [x0, top, x1, bottom] (PDF points, top-left origin) covering these lines; one box per column, so a
    paragraph that continues in the next column is highlighted in both places."""
    groups = {}
    for l in lines:
        key = (l["x0"] >= mid - 3) if mid else False
        groups.setdefault(key, []).append(l)
    return [[round(min(l["x0"] for l in g), 1), round(min(l["top"] for l in g), 1),
             round(max(l["x1"] for l in g), 1), round(max(l["bottom"] for l in g), 1)] for g in groups.values()]


def place_on_page(els, lines, mid=None):
    """Give every element of a page a highlight box. Elements with a crop box use it; paragraphs already got the
    box of their own lines; headings, key-values and captions are matched to the page lines they were read from."""
    squash = lambda t: re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", t).lower())
    texts = [squash(l["text"]) for l in lines]
    used = set()
    for e in els:
        if e.get("boxes"):
            continue
        if e.get("bbox"):
            e["boxes"] = [[round(v, 1) for v in e["bbox"]]]
            continue
        want = squash(re.sub(r"^#+\s*|!\[[^\]]*\]\([^)]*\)", "", e["content_markdown"]))
        if e["type"] == "key_value":
            want = squash(e.get("key", "") + e.get("value", ""))
        if not want:
            continue
        for i, t in enumerate(texts):
            if i in used or not t or not (want.startswith(t) or t.startswith(want[:max(len(want), 1)]) or t.startswith(want)):
                continue
            got, j, rest = [i], i + 1, want[len(t):] if want.startswith(t) else ""
            while rest and j < len(texts) and texts[j] and rest.startswith(texts[j]):
                rest = rest[len(texts[j]):]
                got.append(j)
                j += 1
            used.update(got)
            e["boxes"] = boxes_of([lines[k] for k in got], mid)
            break


def absorb_figure_text(els, start, lines, body, page_h):
    """Text printed INSIDE a chart (axis numbers, panel labels "(a)", legends, "PAMPA = 38 nm/s") is part of the
    picture, not of the document's text. A chart drawn with vector lines is not always found as a picture area, so
    such text used to come out as separate, garbled text blocks between the paragraphs and the figure.
    A text block of this page is moved into the figure when it lies above the figure's caption, in its column,
    below the previous caption, outside the running header / footer, touches the figure area (or text already moved into it), and does not read like
    body text: its font is clearly smaller than the body font or almost none of its words are ordinary words.
    The figure's crop box grows to include it, so the saved picture keeps its axis labels."""
    page_els = els[start:]
    figs = [e for e in page_els if e["type"] == "image" and "_fig" in e]
    drop = set()
    for f in figs:
        g = f.pop("_fig")
        box = list(f["bbox"]) if f.get("bbox") else None
        x0 = min(g["x0"], box[0]) if box else g["x0"]
        x1 = max(g["x1"], box[2]) if box else g["x1"]
        top = box[1] if box else g["cap_top"]
        cands = []
        for k, e in enumerate(page_els):
            if e["type"] != "text" or "caption" in e["flags"] or not e.get("boxes") or id(e) in drop:
                continue
            bx = [min(b[0] for b in e["boxes"]), min(b[1] for b in e["boxes"]),
                  max(b[2] for b in e["boxes"]), max(b[3] for b in e["boxes"])]
            if bx[1] < 0.08 * page_h or bx[3] > 0.92 * page_h:     # running header / footer
                continue
            if bx[1] < g["floor"] - 2 or bx[3] > g["cap_top"] + 3 or min(bx[2], x1 + 10) <= max(bx[0], x0 - 10):
                continue
            inl = [l for l in lines if any(inside(l, b) for b in e["boxes"])]
            sizes = sorted(l["size"] for l in inl) or [body]
            toks = e["content_markdown"].split()
            prose = sum(bool(re.fullmatch(r"[a-z]{3,}[,.;:]?", t)) for t in toks) / max(len(toks), 1)
            if sizes[len(sizes) // 2] < 0.85 * body or prose < 0.25:
                cands.append((bx, e))
        for bx, e in sorted(cands, key=lambda c: -c[0][3]):        # from the caption upwards
            if bx[3] < top - 2.5 * body:                            # a gap: not part of this figure
                continue
            drop.add(id(e))
            top = min(top, bx[1])
            box = [min(box[0], bx[0]), min(box[1], bx[1]), max(box[2], bx[2]), max(box[3], bx[3])] if box else list(bx)
        if box and box != f.get("bbox"):
            f["bbox"] = [round(v, 1) for v in box]
            f["confidence"] = max(f["confidence"], 0.75)
    if drop:
        els[start:] = [e for e in page_els if id(e) not in drop]
    for e in els[start:]:
        e.pop("_fig", None)


def figure_boxes(page):
    boxes = [(i["x0"], i["top"], i["x1"], i["bottom"]) for i in page.images]
    if len(page.curves) > 20:
        boxes.append((min(c["x0"] for c in page.curves), min(c["top"] for c in page.curves),
                      max(c["x1"] for c in page.curves), max(c["bottom"] for c in page.curves)))
    return boxes


def inside(ln, box):
    cx, cy = (ln["x0"] + ln["x1"]) / 2, (ln["top"] + ln["bottom"]) / 2
    return box[0] - 2 <= cx <= box[2] + 2 and box[1] - 2 <= cy <= box[3] + 2


def body_font_size(pdf):
    c = Counter()
    for p in pdf.pages[:4]:
        for ch in p.chars:
            c[round(ch["size"], 1)] += 1
    return c.most_common(1)[0][0]


# ---------- tables ----------
def table_to_md(rows):
    rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in rows if any(r)]
    if not rows:
        return "", 0.0
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    empty = sum(1 for r in rows for c in r if not c) / (len(rows) * width)
    ok = 1.0 if width >= 2 and len(rows) >= 2 else 0.4
    md = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    md += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(md), max(0.0, ok - empty * 0.5)


def rule_between(page, y0, y1):
    """A long horizontal rule between two lines (y0 = bottom of the upper line, y1 = top of the lower one): for
    example the top rule of a table right under its caption. The table's header row starts below it."""
    return any(o["bottom"] - o["top"] < 2 and o["x1"] - o["x0"] > 150 and y0 - 1 <= o["top"] <= y1 + 1
               for o in page.lines + page.rects)


def find_rule_table(page, caption_bottom):
    """Table drawn with horizontal rules only. Returns a box (x0, top, x1, bottom) or None."""
    rules = [o for o in (page.lines + page.rects)
             if o["bottom"] - o["top"] < 2 and o["x1"] - o["x0"] > 150 and o["top"] > caption_bottom]
    rules = sorted(rules, key=lambda o: o["top"])
    if len(rules) < 2:
        return None
    bottom = rules[-1]["bottom"]
    for a, b in zip(rules, rules[1:]):
        if b["top"] - a["top"] > 200:
            bottom = a["bottom"]
            break
    return (min(r["x0"] for r in rules), rules[0]["top"], max(r["x1"] for r in rules), bottom)


def build_table(page, box, gap=9.0):
    """Own table builder: cells are runs of words with gaps < `gap` pt; columns anchored on the header row."""
    words = page.crop(box).extract_words(x_tolerance=1.5, y_tolerance=2)
    if not words:
        return None, 0.0
    rows = []
    for w in sorted(words, key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"])):
        cy = (w["top"] + w["bottom"]) / 2
        if rows and abs(cy - rows[-1]["cy"]) < 4.0:
            rows[-1]["words"].append(w)
        else:
            rows.append({"cy": cy, "words": [w]})
    table = []
    for r in rows:
        cells, cur = [], []
        for w in sorted(r["words"], key=lambda w: w["x0"]):
            if cur and w["x0"] - cur[-1]["x1"] > gap:
                cells.append(cur)
                cur = []
            cur.append(w)
        if cur:
            cells.append(cur)
        table.append([{"text": " ".join(w["text"] for w in c), "cx": (c[0]["x0"] + c[-1]["x1"]) / 2} for c in cells])
    anchors = [c["cx"] for c in table[0]]
    grid = []
    for r in table:
        out = [""] * len(anchors)
        for c in r:
            j = min(range(len(anchors)), key=lambda k: abs(anchors[k] - c["cx"]))
            out[j] = (out[j] + " " + c["text"]).strip()
        grid.append(out)
    grid, merged = fill_merged_cells(grid)
    md, conf = table_to_md(grid)
    return md, (min(conf, 0.85) if merged else conf)


# ---------- formulas ----------
INLINE_MATH_MIN_CHARS = 2      # math-font letters/digits in a paragraph that make it "inline math"


def paragraph_box(el, lines, page):
    """Crop box of a paragraph, set only when all its lines are in one column (so the crop shows exactly this
    paragraph). The ensemble can then send a paragraph the witness tools disagree on ("low_agreement") to the AI.
    Inline math: characters set in a math font (J_theta(xi), CO_2) lose their sub/superscripts in the text layer
    ("CO emissions"), so the paragraph is flagged inline_math, its confidence drops to 0.6, and at the accurate level
    the AI rewrites it with inline LaTeX (only when the crop box exists, i.e. the paragraph is in one column)."""
    if not lines:
        return
    math_chars = sum(l.get("math_alnum", 0) for l in lines)
    if routed("inline_math", "rules") and math_chars >= INLINE_MATH_MIN_CHARS:
        el["flags"] += [f for f in ("inline_math", "needs_ai") if f not in el["flags"]]
        el["confidence"] = min(el["confidence"], 0.6)
        el["math_chars"] = math_chars
    first = lines[0]
    one_column = all(min(l["x1"], first["x1"]) > max(l["x0"], first["x0"]) for l in lines) and \
        all(b["top"] >= a["top"] for a, b in zip(lines, lines[1:]))
    if one_column:
        el["bbox"] = [max(min(l["x0"] for l in lines) - 3, 0), max(min(l["top"] for l in lines) - 3, 0),
                      min(max(l["x1"] for l in lines) + 3, page.width), min(max(l["bottom"] for l in lines) + 4, page.height)]


def paragraph_ok(original, text, vocab=""):
    """Accept the AI's re-reading of a paragraph only if it is the SAME paragraph: an even number of $ signs (inline
    LaTeX), balanced brackets, and at least 85% of its words (4+ letters, math between $ ignored) found in the
    original (or in the witness tools' text), with no large additions."""
    if not text or (text.count("$") - text.count("\\$")) % 2:
        return False
    if balanced(original) and not balanced(text):
        return False
    words = lambda t: re.findall(r"[a-z]{4,}", re.sub(r"\$[^$]*\$", " ", t).lower())
    orig, new = words(original), words(text)
    if not orig or not new:
        return False
    if vocab:       # the rules' text may be the wrong one: accept words that the witness tools read on this page
        known = set(orig) | set(words(vocab))
        supported = sum(1 for w in new if w in known) / len(new)
        return supported >= 0.85 and 0.5 * len(orig) <= len(new) <= 1.5 * len(orig) + 5
    kept = sum(1 for w in orig if w in set(new)) / len(orig)
    return kept >= 0.85 and len(new) <= 1.3 * len(orig) + 5


def algorithm_ok(original, lines_out, vocab=""):
    """Accept the AI's version of a pseudocode box only if it is the SAME algorithm: even $ signs, at least 70% of
    the numbered steps kept, and the words outside the math (CamelCase names split: TrainPolicy = train policy)
    found in the rules' text or the witness tools' text (85%), with most of the original words kept (70%)."""
    text = "\n".join(lines_out)
    if not lines_out or (text.count("$") - text.count("\\$")) % 2:
        return False
    steps_in = len(re.findall(r"(?m)^\s*\d{1,3}\s*:", original))
    steps_out = sum(1 for l in lines_out if re.match(r"^\s*(\*\*)?\d{1,3}\s*:", l))
    if steps_out < max(1, int(0.7 * steps_in)) or steps_out > steps_in + 3:
        return False

    def words(t):
        t = re.sub(r"\$[^$]*\$|\\[a-zA-Z]+", " ", t)
        t = re.sub(r"([a-z])([A-Z])", r"\1 \2", t)
        return re.findall(r"[a-z]{3,}", t.lower())
    orig, new = words(original), words(text)
    if not orig or not new:
        return False
    known = set(orig) | set(words(vocab))
    supported = sum(1 for w in new if w in known) / len(new)
    kept = sum(1 for w in set(orig) if w in set(new)) / len(set(orig))
    return supported >= 0.85 and kept >= 0.7


def formula_blocks(lines):
    def mark(l):
        t = l["text"]
        if CAPTION.match(t) or len(t) > 200 or len(t) < 6:
            return False
        if EQ_LINE.search(t) and "=" in t:
            return True
        return "(cid:" in t or l["math"] >= 0.5

    def near(a, b):
        return b["top"] - a["bottom"] < 1.0 * max(a["size"], b["size"]) and a["top"] - b["bottom"] < 1.0 * max(a["size"], b["size"]) and \
            min(a["x1"], b["x1"]) > max(a["x0"], b["x0"])

    def helper(l):
        t = l["text"]
        return (l["math"] >= 0.1 and len(t) <= 60) or (len(t) <= 12 and not re.search(r"[a-z]{4,}", t))

    ranges = []
    for i, l in enumerate(lines):
        if not mark(l):
            continue
        s = e = i

        def block(a, b):
            """The block lines[a..b] as one box: a sum's limits (N above, i=1 below) sit under the sum sign, not under
            the line just before them, so the next line is compared with the WHOLE formula so far."""
            seg = lines[a:b + 1]
            return {"top": min(x["top"] for x in seg), "bottom": max(x["bottom"] for x in seg),
                    "x0": min(x["x0"] for x in seg), "x1": max(x["x1"] for x in seg),
                    "size": max(x["size"] for x in seg)}
        while s > 0 and near(lines[s - 1], block(s, e)) and helper(lines[s - 1]):
            s -= 1
        while e < len(lines) - 1 and near(block(s, e), lines[e + 1]) and helper(lines[e + 1]):
            e += 1
        if ranges and s <= ranges[-1][1] + 0:
            ranges[-1] = (ranges[-1][0], max(e, ranges[-1][1]))
        else:
            ranges.append((s, e))
    return ranges


def merge_overlapping_formulas(els):
    """One printed equation can be detected as two regions when its limits / sub-lines sit on separate rows (for
    example "min" under the minimum, or the limits of a sum). Regions of the same page whose boxes overlap in height
    AND width are ONE equation: merge them, so the AI sees the whole equation in one crop (and is paid once)."""
    out = []
    for e in els:
        prev = next((x for x in reversed(out) if x["type"] == "formula" and x["page"] == e["page"]), None) \
            if e["type"] == "formula" and e.get("bbox") else None
        if prev is not None and prev.get("bbox"):
            a, b = prev["bbox"], e["bbox"]
            if b[1] < a[3] and a[1] < b[3] and b[0] < a[2] and a[0] < b[2]:        # boxes overlap
                prev["bbox"] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                prev["content_markdown"] = prev["content_markdown"].rstrip(" $") + " " + e["content_markdown"].strip(" $") + " $$"
                prev["flags"] = sorted(set(prev["flags"]) | set(e["flags"]))
                continue
        out.append(e)
    return out


def keyvalue_from_label_lines(els):
    """A short heading followed by ONE short comma-separated line is a key-value pair (e.g. a keywords line).
    Structure only: no label names."""
    out = []
    for i, e in enumerate(els):
        out.append(e)
        if e["type"] != "heading" or i + 1 >= len(els):
            continue
        label = e["content_markdown"].lstrip("# ").strip(" :")
        nxt = els[i + 1]
        v = nxt["content_markdown"]
        if (nxt["type"] == "text" and not nxt["flags"] and len(label.split()) <= 3 and len(v) < 250
                and not v.endswith(".") and (v.count(",") >= 2 or v.count(";") >= 2)):
            nxt.update(type="key_value", content_markdown=f"{label}: {v}", key=label, value=v,
                       confidence=0.7, method="label+list-line")
    return out


def fill_merged_cells(grid):
    """A label printed once, vertically centred beside several rows (a merged cell), shows up as blanks above and
    below it. Fill a blank with the nearest non-empty cell of the same column, but only when the label column is
    text (not numbers) and every other cell of that row is filled, so a real missing value is never invented."""
    if len(grid) < 3:
        return grid, False
    numeric = re.compile(r"[-+\d.,()%\s\u2212]+")
    changed = False
    for j in range(len(grid[0])):
        col = [r[j] for r in grid[1:]]
        filled = [k for k, v in enumerate(col) if v]
        if not filled or len(filled) == len(col) or any(numeric.fullmatch(col[k]) for k in filled):
            continue
        for k, v in enumerate(col):
            if v or not all(c for jj, c in enumerate(grid[k + 1]) if jj != j):
                continue
            near = min(filled, key=lambda f: (abs(f - k), f > k))
            grid[k + 1][j] = col[near]
            changed = True
    return grid, changed


# ---------- PDF ----------
def extract_pdf(path, deadline=None, max_pages=None, info=None):
    els = []
    info = info if info is not None else {}
    max_pages = max_pages or int(os.environ.get("MAX_PAGES", "150"))
    in_refs, last_level = False, 2
    with pdfplumber.open(path) as pdf:
        body = body_font_size(pdf)
        info["pages_total"] = len(pdf.pages)
        info["pages_processed"] = 0
        for pno, page in enumerate(pdf.pages, start=1):
            if pno > max_pages or (deadline and time.time() > deadline):
                info["truncated"] = True            # bounded processing time: stop and say so
                break
            info["pages_processed"] = pno
            info.setdefault("page_sizes", []).append([round(float(page.width), 1), round(float(page.height), 1)])
            page_start = len(els)
            words = [w for w in read_words(page) if w["upright"]]
            lines, two_col = order_lines(page, words)
            base = 0.8 if two_col else 0.9
            fboxes = figure_boxes(page)
            last_cap = []          # (x0, x1, bottom) of captions already read on this page
            blocks = {s: e for s, e in formula_blocks(lines)}
            skip_boxes, para, para_top, para_ln, run_in = [], [], None, [], None
            # left margin of each column (most common line start), for first-line indents
            mid = page.width / 2
            margins = {}
            for side in (False, True):
                xs = [round(l["x0"]) for l in lines if (l["x0"] >= mid - 3) == side] if two_col else \
                     [round(l["x0"]) for l in lines]
                if xs:
                    margins[side] = Counter(xs).most_common(1)[0][0]
                if not two_col:
                    break

            def new_paragraph(para_lines, ln):
                """True when line `ln` starts a new paragraph after the lines already in it (same page). Tiny lines
                (a subscript or symbol set on its own line, e.g. "𝜙") never start or end a paragraph."""
                prev = para_lines[-1]
                if ln["top"] - prev["top"] > 1.9 * ln["size"]:                       # vertical gap
                    return True
                def tiny(l):          # a subscript/superscript line: very short, or a few words in a much smaller font
                    return len(l["text"].strip()) <= 4 or (len(l["text"].split()) <= 4 and l["size"] < 0.8 * body)
                if tiny(ln):
                    return False
                full = [l for l in para_lines if not tiny(l)]
                if not full:
                    return False
                prev = full[-1]
                ends = prev["text"].rstrip().endswith((".", "!", "?", ":"))
                if abs(ln["size"] - prev["size"]) > 1.2:                              # other font size
                    return True
                other_col = two_col and ((ln["x0"] >= mid - 3) != (prev["x0"] >= mid - 3))
                if other_col or ln["top"] < prev["top"] - 2:                          # jumped column / upwards
                    starts_low = ln["text"][:1].islower()
                    return ends or not starts_low and not prev["text"].rstrip().endswith((",", "-", "–"))
                m = margins.get((ln["x0"] >= mid - 3) if two_col else False)
                if m is not None and ends and 0.6 * ln["size"] <= ln["x0"] - m <= 4 * ln["size"]:
                    return True                                                       # first-line indent
                return False

            def flush_para():
                nonlocal para, para_ln, run_in
                if para:
                    text = re.sub(r"-\s+(?=[a-z])", "", " ".join(para))
                    text = re.sub(r"\s+", " ", text).strip()
                    if text:
                        el = new_el("text", pno, text, base, "pdfplumber")
                        if run_in:
                            el["run_in_heading"], el["heading_level"] = run_in
                        el["boxes"] = boxes_of(para_ln, page.width / 2 if two_col else None)
                        paragraph_box(el, para_ln, page)
                        els.append(el)
                para, para_ln, run_in = [], [], None

            i = 0
            while i < len(lines):
                ln = lines[i]
                if i in blocks:                                      # display equation block
                    flush_para()
                    blk = lines[i:blocks[i] + 1]
                    box = (max(min(l["x0"] for l in blk) - 4, 0), max(min(l["top"] for l in blk) - 8, 0),
                           min(max(l["x1"] for l in blk) + 4, page.width), min(max(l["bottom"] for l in blk) + 8, page.height))
                    raw = " ".join(l["text"] for l in blk)
                    raw = unicodedata.normalize("NFKC", re.sub(r"\(cid:\d+\)", " ", raw))
                    raw = re.sub(r"\s+", " ", raw).strip()
                    els.append(new_el("formula", pno, "$$ " + raw + " $$", 0.3, "math-font-detect", bbox=list(box)))
                    i = blocks[i] + 1
                    continue
                t = ln["text"].strip()
                i += 1
                if not t or re.fullmatch(r"\d{1,3}", t):
                    continue
                if ln["size"] < 0.95 * body and any(inside(ln, b) for b in fboxes) and not CAPTION.match(t):
                    continue                                          # text inside figures
                if any(inside(ln, b) for b in skip_boxes):
                    continue
                if ALGO.match(t):                                     # algorithm / pseudocode box
                    flush_para()
                    blk = [ln]
                    while i < len(lines):
                        nx = lines[i]
                        nt = nx["text"].strip()
                        same_col = min(nx["x1"], ln["x1"]) > max(nx["x0"], ln["x0"])
                        close = nx["top"] - blk[-1]["bottom"] < 2.2 * max(nx["size"], 1)
                        if not (same_col and close) or NUM_HEAD.match(nt) or CAPTION.match(nt) or ALGO.match(nt):
                            break
                        if not (ALGO_LINE.match(nt) or nx["math"] >= 0.5 or len(blk) == 1 or nx["x0"] > ln["x0"] + 6):
                            break
                        blk.append(nx)
                        i += 1
                    first = "**" + " ".join(t.split()[:2]) + "** " + " ".join(t.split()[2:])
                    body_lines = [unicodedata.normalize("NFKC", l["text"].strip()) for l in blk[1:]]
                    box = [max(min(l["x0"] for l in blk) - 4, 0), max(min(l["top"] for l in blk) - 6, 0),
                           min(max(l["x1"] for l in blk) + 4, page.width), min(max(l["bottom"] for l in blk) + 6, page.height)]
                    # the text layer scrambles the math in pseudocode (sub/superscripts, symbols out of order):
                    # low confidence and needs_ai, so the accurate level rewrites it from the image
                    els.append(new_el("algorithm", pno, "  \n".join([first] + body_lines), 0.4, "algorithm-detect",
                                      ["needs_ai"], bbox=box))
                    continue
                m = CAPTION.match(t)
                if m:
                    flush_para()
                    cap = [t]
                    cap_x0, cap_x1, cap_top = ln["x0"], ln["x1"], ln["top"]
                    while i < len(lines) and lines[i]["top"] - ln["bottom"] < 4 and abs(lines[i]["size"] - ln["size"]) < 1 \
                            and not rule_between(page, ln["bottom"], lines[i]["top"]):     # a table's top rule ends the caption
                        cap.append(lines[i]["text"]); ln = lines[i]; i += 1
                    caption = " ".join(cap)
                    if m.group(1) == "Table":
                        els.append(new_el("text", pno, caption, base, "pdfplumber", ["caption"]))
                        box = find_rule_table(page, ln["bottom"])
                        if box:
                            skip_boxes.append(box)
                            md, conf = build_table(page, box)
                            if md:
                                els.append(new_el("table", pno, md, conf, "rule-table+gap-columns",
                                                  bbox=[box[0] - 3, box[1] - 3, box[2] + 3, box[3] + 3]))
                    else:
                        # the figure = drawing/image areas just above its caption, in the caption's column, and
                        # below any earlier caption in that column (so a figure never takes the previous figure)
                        cap_x1 = max(cap_x1, ln["x1"])
                        floor = max([c[2] for c in last_cap if min(c[1], cap_x1) > max(c[0], cap_x0)], default=0)
                        above = [b for b in fboxes if cap_top - 400 <= b[3] <= cap_top + 5 and b[1] >= floor - 2
                                 and min(b[2], cap_x1 + 10) > max(b[0], cap_x0 - 10)]
                        box = ([min(b[0] for b in above), min(b[1] for b in above),
                                max(b[2] for b in above), max(b[3] for b in above)] if above else None)
                        conf = 0.75 if box else 0.55
                        els.append(new_el("image", pno, f"![{m.group(1)} {m.group(2)}](figure)\n\n{caption}",
                                          conf, "caption-detect", bbox=box,
                                          _fig={"floor": floor, "cap_top": cap_top, "x0": cap_x0, "x1": cap_x1}))
                    last_cap.append((cap_x0, max(cap_x1, ln["x1"]), ln["bottom"]))
                    continue
                nm = NUM_HEAD.match(t)
                is_title = pno == 1 and ln["size"] >= 1.5 * body
                short = len(t.split()) <= 10 and not t.endswith((".", ",", ";"))
                # an unnumbered heading is a short line clearly set apart by font: bold and larger than body text,
                # or much larger (centred author names are only slightly larger and not bold). No word list.
                styled = short and ((ln["bold"] and ln["size"] >= body * 1.1) or ln["size"] >= body * 1.4)
                if is_title or (nm and len(t) < 90 and (ln["size"] > body * 1.05 or ln["bold"])) or styled:
                    flush_para()
                    if is_title:
                        while i < len(lines) and lines[i]["size"] >= 1.5 * body and lines[i]["top"] - ln["bottom"] < 6:
                            t += " " + lines[i]["text"]; ln = lines[i]; i += 1
                        level, conf = 1, 0.9
                    elif nm:
                        level, conf = nm.group(1).count(".") + 2, 0.9
                    else:
                        level, conf = 2, 0.7
                    last_level = level if not is_title else 1
                    in_refs = bool(REF_HEADING.match(t.strip(" :")))
                    els.append(new_el("heading", pno, "#" * level + " " + t, conf, "font-size+numbering"))
                    continue
                kv = None if (in_refs or CAPTION.match(t)) else kv_from_line(t, allow_colon=(pno == 1 and ln["size"] <= body * 1.05))
                if kv:
                    flush_para()
                    els.append(new_el("key_value", pno, f"{kv[0]}: {kv[1]}", kv[2], "label+typed-value",
                                      key=kv[0], value=kv[1]))
                    continue
                if para and new_paragraph(para_ln, ln):
                    flush_para()
                # RUN-IN heading: bold words at the very start of a paragraph, text continuing on the same line
                # ("Original dataset  Current publicly available ..."). It is a sub-heading of the last heading.
                lead = ln["lead_bold"]
                rest = len(ln["words"]) - lead
                head = " ".join(ln["words"][:lead]).strip()
                if (not para and not in_refs and not ln["all_bold"] and ln["size"] <= body * 1.1 and rest >= 3
                        and 1 <= lead <= 10 and len(re.findall(r"[A-Za-z]", head)) >= 4
                        and (lead >= 2 or head.endswith((".", ":")))):
                    # kept IN its paragraph, as printed ("**Original dataset** Current publicly ..."); the paragraph
                    # element records it as a sub-heading (run_in_heading + heading_level) for the outline.
                    run_in = (head.rstrip(".:"), min(last_level + 1, 6))
                    t = "**" + head + "** " + " ".join(ln["words"][lead:])
                para.append(t)
                para_ln.append(ln)
                para_top = ln["top"]
            flush_para()
            absorb_figure_text(els, page_start, lines, body, float(page.height))
            place_on_page(els[page_start:], lines, page.width / 2 if two_col else None)
    return keyvalue_from_label_lines(merge_overlapping_formulas(els))


# ---------- ACCURATE level: AI only where the rules are unsure ----------
def balanced(s):
    return s.count("{") == s.count("}") and s.count("(") == s.count(")") and s.count("[") == s.count("]")


def add_ai_keyvalues(els, text, page_text):
    """Add key-value pairs the model read from page 1. Validation against the page's own text layer: the LABEL must be
    printed directly followed by the VALUE (so the model can neither invent a value nor make up a label such as
    "Author" for a name), the label must not be a heading, and the pair must not already be in the output."""
    if not text:
        return
    norm = lambda x: re.sub(r"\W+", "", unicodedata.normalize("NFKC", x)).lower()
    page_n = norm(page_text)
    have = {norm(e.get("value", "")) for e in els if e["type"] == "key_value"}
    headings = {norm(re.sub(r"^#+\s*", "", e["content_markdown"])) for e in els if e["type"] == "heading"} | \
               {norm(e["run_in_heading"]) for e in els if e.get("run_in_heading")}
    new = []
    for line in re.sub(r"^```[a-z]*\n?|```$", "", text.strip()).splitlines():
        if ":" not in line:
            continue
        k, v = (x.strip(" *-\u2022") for x in line.split(":", 1))
        if not k or not v or len(k.split()) > 8 or norm(v) in have:
            continue
        if norm(v) and norm(k) and norm(k) + norm(v) in page_n and norm(k) not in headings:
            have.add(norm(v))
            new.append(new_el("key_value", 1, f"{k}: {v}", 0.8, "ai-keyvalue", ["ai_added"], key=k, value=v))
    if new:
        at = next((i for i, e in enumerate(els) if e["page"] > 1), len(els))
        els[at:at] = new


def crosscheck_text(pdf_path, els):
    """Second opinion on the text: read the same pages with pypdf (a different library, different reading order and
    spacing) and measure how much of each paragraph the rules produced is also in pypdf's text. Agreement of two
    independent readers is real evidence of correctness. Low agreement lowers the confidence and flags the paragraph
    (the AI quality check can then look only at those). Returns the number of flagged paragraphs."""
    from pypdf import PdfReader
    squash = lambda t: re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", t).lower())
    try:
        pages = [squash(p.extract_text() or "") for p in PdfReader(str(pdf_path)).pages]
    except Exception:
        return 0
    flagged = 0
    for e in els:
        if e["type"] != "text" or "caption" in e["flags"] or not (1 <= e["page"] <= len(pages)):
            continue
        s = squash(e["content_markdown"])
        if len(s) < 40:
            continue
        shingles = [s[i:i + 12] for i in range(0, len(s) - 11, 4)]
        agree = sum(1 for g in shingles if g in pages[e["page"] - 1]) / len(shingles)
        e["agreement"] = round(agree, 2)
        if agree < 0.85:
            e["flags"].append("text_disagree")
            e["confidence"] = min(e["confidence"], round(agree, 2))
            flagged += 1
    return flagged


def ai_repair(pdf_path, els, provider, stats, workers=4, budget=None):
    try:
        from . import ai
    except ImportError:
        import ai
    kinds = [k for k in ("formula", "table") if routed(k, "ai")]
    todo = [e for e in els if e["type"] in kinds and e.get("bbox") and
            (e["type"] == "formula" or "needs_ai" in e["flags"] or e["confidence"] < 0.85)]
    todo += [e for e in els if e["type"] == "algorithm" and e.get("bbox")]
    todo += [e for e in els if e["type"] == "text" and e.get("bbox") and
             (("low_agreement" in e["flags"] and routed("paragraph", "ai")) or
              ("inline_math" in e["flags"] and routed("inline_math", "ai")))]
    want_kv = routed("key_value", "ai")
    jobs, page1_text = [], ""
    with pdfplumber.open(pdf_path) as pdf:                  # render crops one by one (pdfium is not thread-safe)
        import io
        if want_kv:
            page1_text = pdf.pages[0].extract_text() or ""
            buf = io.BytesIO()
            pdf.pages[0].to_image(resolution=130).original.convert("RGB").save(buf, format="PNG")
            jobs.append((buf.getvalue(), "kv"))            # job 0: key-value pairs printed on page 1
        for e in todo:
            res = 200 if e["type"] in ("formula", "text", "algorithm") else 150
            img = pdf.pages[e["page"] - 1].crop(tuple(e["bbox"])).to_image(resolution=res).original
            import io
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            kind = e["type"]
            if kind == "text":
                kind = "inline_math" if "inline_math" in e["flags"] else "paragraph"
            jobs.append((buf.getvalue(), kind))
    if not jobs:
        return
    results = ai.run_parallel(provider, stats, jobs, workers=workers, budget=budget)
    if want_kv:
        add_ai_keyvalues(els, results[0], page1_text)
        results = results[1:]
    for e, text in zip(todo, results):
        if not text:
            e["flags"].append("ai_skipped_or_failed")
            continue
        text = re.sub(r"^```[a-z]*\n?|```$", "", text.strip()).strip()
        # Confidence after a repair is 0.8 = "the AI answer is well-formed" (not yet checked against the image).
        # The AI quality check (qc.py) then raises it (pass, up to 0.95) or lowers it (fail, 0.5).
        if e["type"] == "algorithm":
            lines_out = [l.rstrip() for l in text.strip().strip("`").splitlines() if l.strip()]
            if algorithm_ok(e["content_markdown"], lines_out, e.get("_vocab", "")):
                e["rules_text"] = e["content_markdown"]
                e["content_markdown"] = "  \n".join(lines_out)
                e.update(confidence=0.8, method="algorithm-detect+ai-latex")
                e["flags"] = [f for f in e["flags"] if f != "needs_ai"] + ["ai_repaired"]
            else:
                e["flags"].append("ai_rejected")
            continue
        if e["type"] == "text":
            text = re.sub(r"\s+", " ", text)
            if paragraph_ok(e["content_markdown"], text, e.get("_vocab", "")):
                head = e.get("run_in_heading")
                if head and not text.startswith("**"):               # keep the run-in heading bold, as printed
                    rest = text[len(head):].lstrip(" .:") if text.lower().startswith(head.lower()) else text
                    text = f"**{head}** {rest}"
                e["rules_text"] = e["content_markdown"]
                e["content_markdown"] = text
                e.update(confidence=0.8, method="pdfplumber+ai-inline-latex" if "inline_math" in e["flags"]
                         else "pdfplumber+ai-reread")
                e["flags"] = [f for f in e["flags"] if f != "needs_ai"] + ["ai_repaired"]
            else:
                e["flags"].append("ai_rejected")
            continue
        if e["type"] == "formula":
            text = text.strip("$ \n")
            if text and balanced(text):
                e["content_markdown"] = "$$ " + text + " $$"
                e.update(confidence=0.8, method="math-detect+ai-latex")
                e["flags"] = [f for f in e["flags"] if f != "needs_ai"] + ["ai_repaired"]
            else:
                e["flags"].append("ai_rejected")
        else:
            rows = [l for l in text.splitlines() if l.strip().startswith("|")]
            widths = {l.count("|") for l in rows}
            if len(rows) >= 3 and len(widths) == 1:
                e["content_markdown"] = "\n".join(rows)
                e.update(confidence=0.8, method="rule-table+ai-markdown")
                e["flags"] = [f for f in e["flags"] if f != "needs_ai"] + ["ai_repaired"]
            else:
                e["flags"].append("ai_rejected")


# ---------- Web ----------
def main_content(soup):
    """The element that holds the article: <main>/<article> if present, else the block with the most paragraph text.
    Works on any site (no Wikipedia ids)."""
    for sel in ("main", "article", "[role=main]"):
        el = soup.select_one(sel)
        if el is not None and len(el.get_text(" ", strip=True)) > 500:
            return el
    best, score = None, 0
    for el in soup.find_all(["div", "section"]):
        n = sum(len(p.get_text(" ", strip=True)) for p in el.find_all("p", recursive=False))
        if n > score:
            best, score = el, n
    return best if best is not None and score > 300 else (soup.body or soup)


def is_label_value_table(table):
    """A table whose rows are mostly <th>label</th><td>value</td> is a set of key-value pairs, not a data table."""
    rows = [tr.find_all(["th", "td"]) for tr in table.find_all("tr")]
    pairs = sum(1 for c in rows if len(c) == 2 and c[0].name == "th" and c[1].name == "td")
    return pairs >= 3 and pairs >= 0.6 * len(rows)

def tidy(t):
    t = re.sub(r"\s+([,.;:!?%)\]])", r"\1", t)   # "word ," -> "word,"
    t = re.sub(r"([(\[])\s+", r"\1", t)           # "( x" -> "(x"
    return t

def extract_web(url_or_html):
    from bs4 import BeautifulSoup
    if url_or_html.startswith("http"):
        import requests
        r = requests.get(url_or_html, timeout=30, headers={"User-Agent": "scope2-extractor/0.1 (student project)"})
        r.raise_for_status()
        html = r.text
    else:
        html = url_or_html
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        el.decompose()
    for sup in soup.find_all("sup"):                     # citation / footnote markers: [14], [a], [citation needed]
        if re.fullmatch(r"\[\s*[\w\s.]{1,30}\s*\]", sup.get_text(strip=True)):
            sup.decompose()
    root = main_content(soup)
    els = []
    title = soup.find("h1")
    if title:
        els.append(new_el("heading", 1, "# " + title.get_text(" ", strip=True), 0.95, "html-h1"))
    for node in root.find_all(["h2", "h3", "h4", "p", "table","img"]):
        if node.name == "img":
          fig = node.find_parent("figure")
          caption = fig.get_text(" ", strip=True) if fig else (node.get("alt") or "")
          els.append(new_el("image", 1, f"![image]({node.get('src', '')})\n\n{caption}", 0.8, "html-img"))
          continue
        if node.find_parent("table") is not None and node.name != "table":
            continue
        if node.name in ("h2", "h3", "h4"):
            txt = re.sub(r"\[edit\]", "", node.get_text(" ", strip=True)).strip()
            els.append(new_el("heading", 1, "#" * int(node.name[1]) + " " + txt, 0.95, "html-heading"))
        elif node.name == "p":
            txt = tidy(re.sub(r"\[\d+\]|\[[a-z]\]", "", node.get_text(" ", strip=True)).strip())
            if len(txt) > 40:
                els.append(new_el("text", 1, txt, 0.9, "html-p"))
        else:
            cls = " ".join(node.get("class", []))
            rows = []
            for tr in node.find_all("tr"):
                cells = [re.sub(r"\[\d+\]|\[[a-z]\]", "", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
                if cells:
                    rows.append(cells)
            if is_label_value_table(node):
                for r in rows:
                    if len(r) == 2 and r[0] and r[1]:
                        els.append(new_el("key_value", 1, f"{r[0]}: {r[1]}", 0.85, "html-label-value-table", key=r[0], value=r[1]))
            elif len(rows) >= 2:
                md, conf = table_to_md(rows)
                els.append(new_el("table", 1, md, max(conf, 0.7), "html-table"))
    return els


def save_figures(pdf_path, els, img_dir, resolution=150):
    """Every PDF figure the rules found (caption "Figure N" + the drawing/image area above it) is cut out of its page
    and saved as a PNG, and its Markdown link points to that file: ![Figure 3](images/page2_figure_3.png).
    Markdown is text only, so pictures always travel as separate files next to the .md. A figure whose area was not
    found on the page keeps its caption and gets no link. Returns the list of saved file names."""
    figs = [e for e in els if e["type"] == "image" and "](figure)" in e["content_markdown"]]
    if not figs:
        return []
    os.makedirs(img_dir, exist_ok=True)
    saved, used = [], set()
    with pdfplumber.open(pdf_path) as pdf:
        for e in figs:
            m = re.match(r"!\[([^\]]*)\]\(figure\)", e["content_markdown"])
            label = m.group(1) if m else "figure"
            if not e.get("bbox"):
                e["content_markdown"] = e["content_markdown"].replace(f"![{label}](figure)", f"**{label}** (picture area not found)", 1)
                continue
            page = pdf.pages[e["page"] - 1]
            x0, y0, x1, y1 = e["bbox"]
            box = (max(x0 - 2, 0), max(y0 - 2, 0), min(x1 + 2, page.width), min(y1 + 2, page.height))
            if box[2] - box[0] < 5 or box[3] - box[1] < 5:
                continue
            name = f"page{e['page']}_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") + ".png"
            while name in used:
                name = name[:-4] + "_b.png"
            used.add(name)
            page.crop(box).to_image(resolution=resolution).original.convert("RGB").save(os.path.join(img_dir, name), format="PNG")
            e["image_file"] = "images/" + name
            e["content_markdown"] = e["content_markdown"].replace("](figure)", f"](images/{name})", 1)
            saved.append(name)
    return saved


def to_markdown(els):
    return "\n\n".join(e["content_markdown"] for e in els) + "\n"


def run(src, out_md, level="standard", budget=None):
    """budget = max seconds for the whole job (default env JOB_BUDGET_SECONDS, 120). After that the engine
    stops reading pages and/or stops waiting for AI, and says so in the report (truncated / ai_failed_or_timeout)."""
    t0 = time.time()
    budget = budget or float(os.environ.get("JOB_BUDGET_SECONDS", "120"))
    is_web = src.startswith("http")
    info = {}
    els = extract_web(src) if is_web else extract_pdf(src, deadline=t0 + budget * 0.6, info=info)
    flagged_text, qc_d = 0, {}
    if not is_web and routed("text", "pypdf"):
        flagged_text = crosscheck_text(src, els)
    t_rules = time.time() - t0
    stats_d = {}
    if level == "accurate" and not is_web:
        try:
            from . import ai
        except ImportError:
            import ai
        provider = ai.get_provider()
        if provider is None:
            print("AI_PROVIDER not set -> running Standard level")
        else:
            stats = ai.Stats()
            # the repair may use 75% of the time left; the rest is reserved for the quality check, so a long repair
            # can no longer starve the check (the check always gets at least 30 s)
            left = budget - (time.time() - t0)
            check = routed("quality_check", "ai")
            ai_repair(src, els, provider, stats, budget=max(5.0, left * (0.75 if check else 1.0)))
            if check:
                try:
                    from .qc import qc_elements
                except ImportError:
                    from qc import qc_elements
                qc_d = qc_elements(src, els, stats, budget=max(30.0, budget - (time.time() - t0)))
            stats_d = stats.as_dict()          # calls, tokens, cost per AI use (formula / table / kv / qc)
    total = time.time() - t0
    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(out_md).write_text(to_markdown(els), encoding="utf-8")
    report = {"source": src, "level": level, "elements": len(els),
              "needs_ai_left": sum(1 for e in els if "needs_ai" in e["flags"]),
              "router": effective_router(), "text_disagree": flagged_text,
              "seconds_rules": round(t_rules, 2), "seconds_total": round(total, 2),
              "budget_seconds": budget, **info, **stats_d, **qc_d}
    Path(out_md).with_suffix(".json").write_text(
        json.dumps({"report": report, "elements": els}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report))
    return els, report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out_md")
    ap.add_argument("--level", choices=["standard", "accurate"], default="standard")
    a = ap.parse_args()
    run(a.src, a.out_md, a.level)

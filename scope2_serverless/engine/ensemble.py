"""Ensemble: compare the engine's output with independent witness tools, WITHOUT ground truth.

How two tools are compared
    Text is normalised (Unicode NFKC, lower case, LaTeX commands and every space/punctuation removed) and cut into
    overlapping 6-character pieces ("shingles"). Two readings agree as much as their pieces overlap. Removing spaces
    makes the comparison fair to tools that glue words together (MarkItDown) or split them differently.

Signals computed here (per element)
    tool_support   for each witness: share of the element's pieces found on the same page of that witness (0..1)
    agreement      mean of tool_support over the witnesses that read that page
    checks         simple structure checks that need no answer key (table is rectangular, brackets balance,
                   no unreadable glyphs, AI-written text is supported by the witnesses)
Per page / per document
    coverage       share of the text that at least 2 witnesses agree is on the page, found in our output
    possibly_missing   witness lines with almost none of their pieces in our output
    tool_scores    leave-one-out: each tool is scored against the majority of the OTHER tools (precision, recall, F1)
Tables: the engine's table and the witnesses' tables for the same page are compared pairwise; the candidate that
agrees most with all others (the "medoid") is kept. This replaces a weak engine table with e.g. Azure's.
"""
import math
import re
import unicodedata
from collections import Counter

K = 6
TEXTUAL = ("heading", "text", "caption", "key_value", "list", "algorithm")


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"\\[a-z]+", " ", s)                      # LaTeX commands (\theta, \frac) are not content letters
    return re.sub(r"[\W_]+", "", s)


def shingles(s, k=K):
    n = norm(s)
    if not n:
        return set()
    if len(n) <= k:
        return {n}
    return {n[i:i + k] for i in range(len(n) - k + 1)}


def contained(a, b):
    """share of a's pieces that are in b (None if a is empty)."""
    return len(a & b) / len(a) if a else None


def f1(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    p, r = inter / len(a), inter / len(b)
    return 0.0 if inter == 0 else 2 * p * r / (p + r)


IMG_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)")


def el_text(e):
    """The element's readable text; for an image element that is its caption (the ![..](..) part is dropped)."""
    return IMG_MD.sub(" ", e["content_markdown"]) if e["type"] == "image" else e["content_markdown"]


def ok_witnesses(witnesses):
    return [w for w in witnesses if w["status"] == "ok" and w["pages"]]


def page_sets(witnesses):
    """{tool: {page: shingle set}} for witnesses that produced real text on that page."""
    out = {}
    for w in ok_witnesses(witnesses):
        out[w["tool"]] = {p: shingles(v.get("text", "")) for p, v in w["pages"].items()
                          if len(norm(v.get("text", ""))) >= 20}
    return out


def grid_text(grid):
    return " ".join(c for row in grid for c in row)


# ------------------------------------------------------------------ scanned pages
def fill_scanned_pages(els, witnesses, table_to_md, new_el):
    """Pages where the PDF has (almost) no text layer -> take the content from the best OCR witness
    (Azure, then Textract, then Docling). Returns the pages filled."""
    by = {w["tool"]: w for w in ok_witnesses(witnesses)}
    engine_chars = Counter()
    for e in els:
        engine_chars[e["page"]] += len(norm(el_text(e)))
    all_pages = sorted({p for w in by.values() for p in w["pages"]})
    filled = []
    for p in all_pages:
        if engine_chars[p] >= 30:
            continue
        for tool in ("azure", "textract", "docling"):
            pg = by.get(tool, {}).get("pages", {}).get(p)
            if not pg or len(norm(pg.get("text", ""))) < 30:
                continue
            prior = pg.get("ocr_confidence") or 0.7
            new = []
            paras = pg.get("paragraphs") or [{"text": l, "role": "text"} for l in pg["text"].split("\n") if l.strip()]
            for para in paras:
                role = (para.get("role") or "text").lower()
                if role in ("pageheader", "pagefooter", "pagenumber", "page_header", "page_footer"):
                    continue
                typ = "heading" if role in ("title", "sectionheading", "section_header") else "text"
                txt = ("## " if typ == "heading" else "") + para["text"].strip()
                new.append(new_el(typ, p, txt, prior, f"ocr:{tool}", ["ocr"]))
            for grid in pg.get("tables", []):
                md, _ = table_to_md(grid)
                if md:
                    new.append(new_el("table", p, md, prior, f"ocr:{tool}", ["ocr"]))
            if new:
                at = next((i for i, e in enumerate(els) if e["page"] > p), len(els))
                els[at:at] = new
                filled.append({"page": p, "tool": tool, "elements": len(new)})
            break
    return filled


# ------------------------------------------------------------------ tables: keep the candidate most tools agree with
def choose_tables(els, witnesses, table_to_md):
    changed = 0
    for e in els:
        if e["type"] != "table":
            continue
        own = shingles(e["content_markdown"])
        cands = {"engine": (e["content_markdown"], own)}
        for w in ok_witnesses(witnesses):
            grids = w["pages"].get(e["page"], {}).get("tables") or []
            best = max(grids, key=lambda g: f1(own, shingles(grid_text(g))), default=None)
            if best is not None and f1(own, shingles(grid_text(best))) >= 0.3:
                md, _ = table_to_md(best)
                if md:
                    cands[w["tool"]] = (md, shingles(md))
        if len(cands) < 3:                                  # need at least 2 witnesses to out-vote the engine
            e.setdefault("evidence", {})["table_candidates"] = list(cands)
            continue
        mean_sim = {t: sum(f1(s, o[1]) for u, o in cands.items() if u != t) / (len(cands) - 1)
                    for t, (_, s) in cands.items()}
        best = max(mean_sim, key=mean_sim.get)
        e.setdefault("evidence", {})["table_candidates"] = {t: round(v, 3) for t, v in mean_sim.items()}
        # replace only when another candidate is clearly the consensus: it agrees with the others at least 0.9 and
        # better than the engine's own table does (a lost row or a wrong cell moves the similarity only a little)
        if best != "engine" and mean_sim[best] >= 0.9 and mean_sim[best] >= mean_sim["engine"] + 0.02:
            e["engine_markdown"] = e["content_markdown"]
            e["content_markdown"] = cands[best][0]
            e["method"] = f"ensemble:{best}-table"
            e["flags"].append("replaced_by_consensus")
            changed += 1
    return changed


# ------------------------------------------------------------------ per-element agreement
def formula_support(e, witnesses):
    out = {}
    own = shingles(e["content_markdown"], 4)
    for w in ok_witnesses(witnesses):
        pg = w["pages"].get(e["page"], {})
        forms = (pg.get("formulas") or []) + (pg.get("inline_formulas") or [])
        if "formulas" not in pg:
            continue                                         # this tool cannot read formulas at all
        out[w["tool"]] = round(max((f1(own, shingles(f, 4)) for f in forms), default=0.0), 3)
    return out


def score_agreement(els, witnesses, only_repaired=False):
    """only_repaired=True: re-measure the elements the AI rewrote (after the repair), without new flags."""
    sets = page_sets(witnesses)
    for e in els:
        if only_repaired and "ai_repaired" not in e["flags"]:
            continue
        ev = e.setdefault("evidence", {})
        if e["type"] == "formula":
            sup = formula_support(e, witnesses)
        else:
            own = shingles(re.sub(r"\$[^$]*\$", " ", el_text(e)) if "ai_repaired" in e["flags"] else el_text(e))
            sup = {t: round(contained(own, pages[e["page"]]), 3) for t, pages in sets.items()
                   if e["page"] in pages and own}
        ev["tool_support"] = sup
        ev["agreement"] = round(sum(sup.values()) / len(sup), 3) if sup else None
        if not only_repaired and e["type"] in TEXTUAL and len(sup) >= 2 and ev["agreement"] is not None and ev["agreement"] < 0.6 \
                and "low_agreement" not in e["flags"]:
            e["flags"] += ["low_agreement"] + (["needs_ai"] if "needs_ai" not in e["flags"] else [])


# ------------------------------------------------------------------ checks that need no answer key
def run_checks(els, witnesses, balanced):
    sets = page_sets(witnesses)
    for e in els:
        if e["type"] == "image":
            continue
        c = e["content_markdown"]
        checks = {"readable": "(cid:" not in c and "�" not in c}
        if e["type"] == "table":
            rows = [r for r in c.splitlines() if r.strip().startswith("|")]
            widths = {r.count("|") for r in rows}
            cells = [x.strip() for r in rows for x in r.strip().strip("|").split("|") if not set(x.strip()) <= {"-"}]
            checks["rectangular"] = len(widths) == 1 and len(rows) >= 2
            checks["mostly_filled"] = bool(cells) and sum(1 for x in cells if x) / len(cells) >= 0.5
        if e["type"] == "formula" or "$" in c:
            checks["brackets_balanced"] = balanced(c)
        if e["type"] in TEXTUAL and "ai_repaired" in e["flags"] and sets:
            # math is checked by the AI checker, words here. Each piece of words between two math parts (and each
            # line, without its "3:" step number) is shingled ON ITS OWN: gluing the pieces together would make
            # word pairs no tool ever printed ("parameters|1:for", "policy|3:sample") and fail correct text.
            pieces = [re.sub(r"^\s*\d{1,3}\s*:", "", x) for x in re.split(r"\$[^$]*\$|\n", c)]
            sh = set().union(*(shingles(x) for x in pieces if len(norm(x)) >= K)) if pieces else set()
            union = set().union(*(pages.get(e["page"], set()) for pages in sets.values()))
            sup = contained(sh, union) if sh else None
            checks["words_supported_by_tools"] = sup is None or sup >= 0.85
        e.setdefault("evidence", {})["checks"] = checks


# ------------------------------------------------------------------ coverage and tool leaderboard
def coverage(els, witnesses):
    by = {w["tool"]: w for w in ok_witnesses(witnesses)}
    sets = page_sets(witnesses)
    pages = sorted({p for s in sets.values() for p in s})
    out = []
    for p in pages:
        have = [s[p] for s in sets.values() if p in s]
        if len(have) < 2:
            continue
        cons = {x for x in set().union(*have) if sum(x in h for h in have) >= 2}
        ours = set().union(*(shingles(el_text(e)) for e in els if e["page"] == p))
        cov = round(len(cons & ours) / len(cons), 3) if cons else None
        missing = []
        src = by.get("pypdf") or next(iter(by.values()))
        for line in (src["pages"].get(p, {}).get("text", "") or "").splitlines():
            sh = shingles(line)
            if len(norm(line)) >= 25 and sh and len(sh & ours) / len(sh) < 0.2 and len(sh & cons) / len(sh) > 0.5:
                missing.append(line.strip()[:120])
        out.append({"page": p, "coverage": cov, "possibly_missing": missing[:5]})
    return out


def tool_scores(els, witnesses):
    """Leave-one-out leaderboard: each tool (and our final output, 'engine') against the majority of the others."""
    sets = page_sets(witnesses)
    sets["engine"] = {}
    for e in els:
        sets["engine"].setdefault(e["page"], set()).update(shingles(el_text(e)))
    res = {}
    for tool, pages in sets.items():
        tp = fp_ = fn = 0
        for p, mine in pages.items():
            others = [s[p] for t, s in sets.items() if t != tool and p in s]
            if not others:
                continue
            need = math.ceil(len(others) / 2)
            cons = {x for x in set().union(*others) if sum(x in o for o in others) >= need}
            tp += len(mine & cons)
            fp_ += len(mine - cons)
            fn += len(cons - mine)
        if tp + fp_ + fn == 0:
            continue
        prec = tp / (tp + fp_) if tp + fp_ else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        res[tool] = {"precision": round(prec, 3), "recall": round(rec, 3),
                     "f1": round(2 * prec * rec / (prec + rec), 3) if prec + rec else 0.0, "pages": len(pages)}
    return dict(sorted(res.items(), key=lambda kv: -kv[1]["f1"]))


# ------------------------------------------------------------------ final confidence from the evidence
WEIGHTS = {"judge": 0.45, "agreement": 0.35, "method": 0.20}


def final_confidence(els):
    """confidence = weighted mean of the signals that exist for this element (weights re-normalised):
         judge      AI judge's score for this element on the page image (accurate level)       weight 0.45
         agreement  how much the witness tools agree with it                                   weight 0.35
         method     what the method itself is worth: the rule's prior, raised/lowered by the
                    AI repair check (qc.py)                                                    weight 0.20
       Structure checks can only LOWER the result: one failed check caps it at 0.79 (never "high"),
       two or more at 0.5. Everything used is stored in element["evidence"], so the customer can see why."""
    for e in els:
        ev = e.setdefault("evidence", {})
        sig = {"method": e["confidence"]}
        if ev.get("agreement") is not None:
            sig["agreement"] = ev["agreement"]
        if ev.get("judge") and ev["judge"].get("score") is not None:
            sig["judge"] = ev["judge"]["score"]
        w = {k: WEIGHTS[k] for k in sig}
        total = sum(w.values())
        final = sum(sig[k] * w[k] for k in sig) / total
        failed = [k for k, ok in (ev.get("checks") or {}).items() if not ok]
        if failed:
            final = min(final, 0.79 if len(failed) == 1 else 0.5)
            ev["failed_checks"] = failed
        ev["signals"] = {k: round(v, 3) for k, v in sig.items()}
        ev["weights"] = {k: round(v / total, 3) for k, v in w.items()}
        e["confidence_method"] = e["confidence"]
        e["confidence"] = round(final, 2)
        if e["confidence"] < 0.6 and "needs_review" not in e["flags"]:
            e["flags"].append("needs_review")

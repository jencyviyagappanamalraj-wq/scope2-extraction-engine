"""Confidence = how exactly an extracted element matches the PDF, measured ONLY against the answer key
(eval/ground_truth/<doc>.json: elements read by a person from the rendered pages). No tool output is used as a
reference and no other evaluation is involved.

    1.0      the element is exactly as printed (after ignoring line breaks, repeated spaces, letter case and
             Markdown symbols such as # * |)
    0.0-1.0  character similarity with the printed element (difflib ratio): "Abstrac" vs "Abstract" = 0.93
    0.0      the tool returned something that is not in the answer key (e.g. a heading that is not a heading)
    missing  an answer-key element no extracted element matches: counted as 0 in the coverage score
    not checked  text paragraphs the answer key does not cover (the key holds a sample of sentences, not every
             paragraph): shown without a score, never counted

Per type the matching is one-to-one (greedy, best pairs first, same page), so one answer-key heading can not be
claimed by two extracted headings.
"""
import difflib
import re
import unicodedata

MATCH_MIN = 0.5            # below this similarity an extracted element is "not in the answer key"
TEXT_MIN = 0.6             # a sentence of the key "is in" a paragraph when its best window reaches this


def clean(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"[#*_`|]", " ", s)
    return re.sub(r"\s+", " ", s).strip().casefold()


def sim(a, b):
    a, b = clean(a), clean(b)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def window_sim(sentence, paragraph):
    """Best similarity of the sentence with any run of about the same number of words inside the paragraph.
    Speed: paragraphs sharing under half of the sentence's words are skipped, and only the 5 windows with the most
    shared words are compared character by character."""
    s, p = clean(sentence).split(), clean(paragraph).split()
    if not s or not p:
        return 0.0
    n = len(s)
    if len(p) <= n:
        return sim(sentence, paragraph)
    want = set(s)
    if len(want & set(p)) / len(want) < 0.5:
        return 0.0
    hits = [1 if w in want else 0 for w in p]
    run = sum(hits[:n])
    counts = [run]
    for i in range(1, len(p) - n + 1):
        run += hits[i + n - 1] - hits[i - 1]
        counts.append(run)
    starts = sorted(range(len(counts)), key=lambda i: -counts[i])[:5]
    target = " ".join(s)
    best = 0.0
    for i in starts:
        for j in (i - 1, i, i + 1):
            for m in (n - 1, n, n + 1):
                if j < 0 or m < 1 or j + m > len(p):
                    continue
                best = max(best, difflib.SequenceMatcher(None, target, " ".join(p[j:j + m]), autojunk=False).ratio())
    return best


def latex_key(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r"\\(left|right|mathrm|operatorname|text|mathit|displaystyle|,|;|!|quad)", "", s)
    return re.sub(r"[\s{}$]", "", s)


def latex_sim(a, key_el):
    options = [key_el.get("latex", "")] + key_el.get("alternatives", [])
    A = latex_key(a)
    return max((difflib.SequenceMatcher(None, A, latex_key(o), autojunk=False).ratio() for o in options if o),
               default=0.0)


def cells_of(rows):
    return [clean(c) for r in rows for c in r if clean(c)]


def table_sim(rows, key_el):
    """Cells that match exactly, over the larger of the two cell counts (missing AND extra cells both count)."""
    a, b = cells_of(rows), cells_of(key_el["cells"])
    if not a or not b:
        return 0.0
    pool = list(b)
    hit = 0
    for c in a:
        if c in pool:
            pool.remove(c)
            hit += 1
    return hit / max(len(a), len(b))


def image_sim(el, key_el):
    """A figure on the right page with its caption: 1 point for the figure + 1 per caption keyword found."""
    words = [clean(w) for w in key_el.get("caption_keywords", [])]
    cap = clean(el.get("caption", ""))
    found = sum(1 for w in words if w and w in cap)
    return (1 + found) / (1 + len(words))


def key_text(k):
    t = k["type"]
    if t == "key_value":
        return f'{k["key"]}: {k["value"]}'
    if t == "formula":
        return k.get("latex", "")
    if t == "table":
        return " | ".join(" ".join(r) for r in k["cells"][:2]) + " ..."
    if t == "image":
        return "figure with caption words: " + ", ".join(k.get("caption_keywords", []))
    return k.get("text", "")


def pair_score(el, k):
    t = el["type"]
    if t == "heading":
        return sim(el["text"], k["text"])
    if t == "key_value":
        return sim(el["text"], key_text(k))
    if t == "formula":
        return latex_sim(el.get("latex", el["text"]), k)
    if t == "table":
        return table_sim(el.get("cells", []), k)
    if t == "image":
        return image_sim(el, k)
    raise ValueError(t)


def score(elements, key_elements):
    """Adds to every element: confidence (or None = not checked), key_index, key_text, verdict.
    Returns (missing key elements, per-key best score list)."""
    for i, k in enumerate(key_elements):
        k["_i"] = i
    key_best = {k["_i"]: 0.0 for k in key_elements}
    for typ in ("heading", "key_value", "formula", "table", "image"):
        els = [e for e in elements if e["type"] == typ]
        keys = [k for k in key_elements if k["type"] == typ]
        pairs = []
        for ei, e in enumerate(els):
            for k in keys:
                if k.get("page", 1) == e["page"]:
                    pairs.append((pair_score(e, k), ei, k["_i"]))
        pairs.sort(key=lambda p: -p[0])
        used_e, used_k = set(), set()
        for s, ei, ki in pairs:
            if s < MATCH_MIN or ei in used_e or ki in used_k:
                continue
            used_e.add(ei)
            used_k.add(ki)
            e = els[ei]
            e["confidence"] = round(s, 3)
            e["key_index"] = ki
            e["key_indices"] = [ki]
            e["key_text"] = key_text(key_elements[ki])
            e["verdict"] = "exact" if s >= 0.999 else "close"
            key_best[ki] = s
        for ei, e in enumerate(els):
            if ei not in used_e:
                e["confidence"] = 0.0
                e["verdict"] = "not in the answer key"
    # text: the key holds sample sentences; a paragraph is scored on the key sentences it contains
    texts = [e for e in elements if e["type"] == "text"]
    for k in (k for k in key_elements if k["type"] == "text"):
        best, where = 0.0, None
        for e in texts:
            if e["page"] != k.get("page", 1):
                continue
            s = window_sim(k["text"], e["text"])
            if s > best:
                best, where = s, e
        if where is not None and best >= TEXT_MIN:
            where.setdefault("_hits", []).append((k["_i"], best))
            key_best[k["_i"]] = best
    for e in texts:
        hits = e.pop("_hits", None)
        if hits:
            e["confidence"] = round(sum(s for _, s in hits) / len(hits), 3)
            e["key_index"] = hits[0][0]
            e["key_indices"] = [i for i, _ in hits]
            e["key_text"] = " / ".join(key_text(key_elements[i]) for i, _ in hits)
            e["verdict"] = "exact" if all(s >= 0.999 for _, s in hits) else "close"
            e["key_hits"] = len(hits)
        else:
            e["confidence"] = None
            e["verdict"] = "not checked (no answer-key sentence in this paragraph)"
    missing = [k for k in key_elements if key_best[k["_i"]] < MATCH_MIN and
               not (k["type"] == "text" and key_best[k["_i"]] >= TEXT_MIN)]
    return missing, key_best


def summary(elements, key_elements, key_best, timings, plan):
    rows = []
    for typ, tool, _ in plan:
        els = [e for e in elements if e["type"] == typ]
        checked = [e["confidence"] for e in els if e.get("confidence") is not None]
        keys = [k for k in key_elements if k["type"] == typ]
        tm = timings.get(typ, {})
        rows.append({
            "type": typ, "tool": tool, "status": tm.get("status", "not run"), "error": tm.get("error", ""),
            "seconds": tm.get("seconds"), "extracted": len(els), "checked": len(checked),
            "mean_confidence": round(sum(checked) / len(checked), 3) if checked else None,
            "exact": sum(1 for c in checked if c >= 0.999),
            "in_key": len(keys),
            "key_found": sum(1 for k in keys if key_best[k["_i"]] >= (TEXT_MIN if typ == "text" else MATCH_MIN)),
            "key_coverage": round(sum(key_best[k["_i"]] for k in keys) / len(keys), 3) if keys else None,
            "ms_per_element": round(1000 * tm["seconds"] / len(els), 1) if tm.get("seconds") and els else None,
        })
    return rows


def diff_html(got, want):
    """Character diff: what the tool wrote that is not printed (<del>) and what is printed but missing (<ins>)."""
    import html
    a, b = re.sub(r"\s+", " ", got or "").strip(), re.sub(r"\s+", " ", want or "").strip()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(html.escape(a[i1:i2]))
        if op in ("delete", "replace"):
            out.append("<del>" + html.escape(a[i1:i2]) + "</del>")
        if op in ("insert", "replace"):
            out.append("<ins>" + html.escape(b[j1:j2]) + "</ins>")
    return "".join(out)

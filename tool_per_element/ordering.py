"""Reading order for elements that come from six different tools.

1. Every element gets a box as fractions of its page (top-left origin). The tools already give one, each in its own
   unit (pypdf: points from the bottom, pdfplumber: points from the top, Azure: inches, Textract: page fractions,
   Docling: points from the bottom) and extractors.py converts them. MarkItDown gives no positions: its paragraphs
   are LOCATED on the page by finding their first and last words in the page's word list (the PDF's own text layer,
   read with pdfplumber purely as a ruler; nothing it reads becomes output).
2. Per page: the page is two-column when almost no word of the text layer crosses the middle of the page. Then an
   element wider than 55% of the page is "full width" (title, wide figure); the rest is left or right column by its
   centre. Full-width elements cut the page into bands; inside a band the left column is read top to bottom, then
   the right column. A one-column page is read top to bottom.
3. Duplicates for the merged view: MarkItDown reads ALL text, so its paragraphs also contain the headings, table
   cells, key-values and captions the other tools own. A paragraph that starts with a heading loses that line; a
   paragraph whose words are 80% inside one element of another type is hidden (it stays in the scores).
Web pages: the position is the place of the element's first words in the page's visible text (BeautifulSoup).
"""
import re
import unicodedata


def toks(s):
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.findall(r"[a-z0-9]+", s)


# ------------------------------------------------------------------ locating MarkItDown paragraphs on the page
def page_words(pdf_path):
    import pdfplumber
    out = {}
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            W, H = float(page.width), float(page.height)
            seq = []
            for w in page.extract_words(x_tolerance=1.5, y_tolerance=2, extra_attrs=["upright"]):
                if not w["upright"]:                       # rotated margin text (arXiv stamp)
                    continue
                for t in toks(w["text"]):
                    seq.append((t, w["x0"] / W, w["top"] / H, w["x1"] / W, w["bottom"] / H))
            if two_columns(seq):                            # left column first, then right (like a reader)
                seq.sort(key=lambda w: (w[1] >= 0.5, round(w[2], 3), w[1]))
            out[pno] = seq
    return out


def _find(seq, needle, start=0):
    n = len(needle)
    words = [s[0] for s in seq]
    for i in range(start, len(words) - n + 1):
        if words[i:i + n] == needle:
            return i
    return None


def _locate_from(t, seq, start):
    for n in (6, 4, 3, 2):
        for off in range(0, min(6, max(1, len(t) - n + 1))):
            head = t[off:off + n]
            if len(head) < n:
                continue
            i = _find(seq, head, start)
            if i is None:
                continue
            j_end = i + n - 1
            for m in (6, 4, 3):
                if len(t) > n + m:
                    j = _find(seq[:i + len(t) + 30], t[-m:], i)        # the end must be close to the start
                    if j is not None:
                        j_end = j + m - 1
                        break
            return i, j_end
    return None


def locate(el, words, cursor=0):
    """Box of a paragraph + the index after its last word. Its first words are searched from the end of the previous
    paragraph (cursor) first, so a sentence printed twice on a page is found in the right place."""
    t = toks(el["text"])
    seq = words.get(el["page"], [])
    if not t or not seq:
        return None, cursor
    hit = _locate_from(t, seq, cursor) if cursor else None
    hit = hit or _locate_from(t, seq, 0)
    if not hit:
        return None, cursor
    i, j = hit
    span = seq[i:j + 1]
    first = seq[i]
    one_col = any(w[1] < 0.5 < w[3] for w in span)                            # lines run across the middle
    col = span if one_col else [w for w in span if (w[1] < 0.5) == (first[1] < 0.5)] or span
    return [min(w[1] for w in col), first[2], max(w[3] for w in col), max(w[4] for w in col)], j + 1


# ------------------------------------------------------------------ order one page
def two_columns(seq):
    """Two-column page: almost no word crosses the middle of the page (in one-column text most lines do)."""
    if len(seq) < 40:
        return False
    cross = sum(1 for w in seq if w[1] < 0.5 < w[3])
    right = sum(1 for w in seq if w[1] >= 0.5)
    return cross / len(seq) < 0.03 and right / len(seq) > 0.2


def order_page(els, two_col=True):
    placed = [e for e in els if e.get("bbox")]
    lost = [e for e in els if not e.get("bbox")]
    if not two_col:                                     # one column: top to bottom
        return sorted(placed, key=lambda e: e["bbox"][1]) + lost
    full = [e for e in placed if e["bbox"][2] - e["bbox"][0] > 0.55]
    side = [e for e in placed if e not in full]
    full.sort(key=lambda e: e["bbox"][1])
    cuts = [e["bbox"][1] for e in full] + [9.0]
    out, lo = [], -1.0
    bands = []
    for i, cut in enumerate(cuts):
        band = [e for e in side if lo <= (e["bbox"][1] + e["bbox"][3]) / 2 < cut]
        bands.append(band)
        lo = cut
    for i, band in enumerate(bands):
        left = sorted((e for e in band if (e["bbox"][0] + e["bbox"][2]) / 2 < 0.5), key=lambda e: e["bbox"][1])
        rgt = sorted((e for e in band if (e["bbox"][0] + e["bbox"][2]) / 2 >= 0.5), key=lambda e: e["bbox"][1])
        out += left + rgt
        if i < len(full):
            out.append(full[i])
    return out + lost


def hide_duplicates(elements):
    """Merged view only: MarkItDown paragraphs that repeat what another tool owns."""
    by_page = {}
    for e in elements:
        by_page.setdefault(e["page"], []).append(e)
    for page_els in by_page.values():
        others = [e for e in page_els if e["type"] != "text"]
        for e in (x for x in page_els if x["type"] == "text"):
            t = toks(e["text"])
            disp = e["md"]
            for h in (o for o in others if o["type"] == "heading"):
                ht = toks(h["text"])
                if ht and t[:len(ht)] == ht and len(t) > len(ht):
                    words = disp.split()
                    disp = " ".join(words[len(h["text"].split()):]) if len(words) > len(h["text"].split()) else disp
                    t = t[len(ht):]
            e["md_merged"] = disp
            if not t:
                e["hidden"] = "repeats a heading"
                continue
            ts = set(t)
            for o in others:
                ot = set(toks(o["text"] + " " + o.get("caption", "")))
                if ot and len(ts & ot) / len(ts) >= 0.8:
                    e["hidden"] = f'repeats the {o["type"].replace("_", "-")} found by {o["tool"]}'
                    break


def merge_pdf(elements, pdf_path):
    words = page_words(pdf_path)
    cursor = {}
    for e in elements:
        if e.get("bbox") is None and e["type"] == "text":
            e["bbox"], cursor[e["page"]] = locate(e, words, cursor.get(e["page"], 0))
            e["located"] = e["bbox"] is not None
    hide_duplicates(elements)
    out = []
    for p in sorted({e["page"] for e in elements}):
        out += order_page([e for e in elements if e["page"] == p], two_columns(words.get(p, [])))
    return out


def merge_web(elements, html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer"]):
        t.decompose()
    for t in soup.select('#toc, .toc, #vector-toc, .vector-toc, [id^="vector-toc"], [id^="toc-"]'):
        if t.name not in ("html", "body"):                   # the table of contents repeats every heading
            t.decompose()
    page = toks(soup.get_text(" "))
    index = {}
    for i, w in enumerate(page):
        index.setdefault(w, []).append(i)

    def pos(e, start):
        t = toks(e["text"])[:8]
        if not t:
            return None
        for n in (min(6, len(t)), 3, 2, 1):
            for i in index.get(t[0], []):
                if i >= start and page[i:i + n] == t[:n]:
                    return i
        return None

    cursor = {}                      # each tool returns its elements in document order: search after the previous one
    for e in elements:
        c = cursor.get(e["tool"], 0)
        p = pos(e, c)
        if p is None and c:
            p = pos(e, 0)
        e["_pos"] = p
        if p is not None:
            cursor[e["tool"]] = p + 1
    hide_duplicates(elements)
    return sorted(elements, key=lambda e: (e["_pos"] is None, e["_pos"] or 0))


def order_score(merged, key_elements):
    """Answer-key headings and sentences are listed in reading order. Take the ones we found, look up where they sit
    in the merged output, and count the neighbouring pairs that keep their order."""
    where = {}
    for n, e in enumerate(merged):
        if e["type"] in ("heading", "text"):
            for ki in e.get("key_indices", []):
                where.setdefault(ki, n)
    seq = [where[i] for i, k in enumerate(key_elements) if k["type"] in ("heading", "text") and i in where]
    pairs = list(zip(seq, seq[1:]))
    ok = sum(1 for a, b in pairs if b >= a)
    return {"pairs": len(pairs), "in_order": ok, "score": round(ok / len(pairs), 3) if pairs else None}

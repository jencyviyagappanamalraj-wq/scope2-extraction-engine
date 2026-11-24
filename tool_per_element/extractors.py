"""One tool per element type. Each function reads the WHOLE document with ONE tool and returns only the elements
of its own type, so the time it takes is the time that tool needs for that element type.

    headings   -> pypdf          (font size + bold font name of each text line, from pypdf's text visitor)
    tables     -> pdfplumber     (pdfplumber's own table finder, run on the area between horizontal table rules)
    text       -> MarkItDown     (its paragraphs, page by page)
    formulas   -> Amazon Textract (it has no formula feature: it reads the page text, a rule picks the formula lines)
    key-values -> Azure DI       (prebuilt-layout with the free "keyValuePairs" add-on)
    images     -> Docling        (its layout model's picture items + their captions)
Web pages (URL or saved .html): BeautifulSoup + requests take the place of pypdf and pdfplumber (headings, tables);
MarkItDown (text) and Docling (images) read the HTML too. Azure DI and Textract read PDFs and images only, so they
are not run on web pages (shown as "not run" in the viewer).

Every element: {"type", "tool", "page", "bbox": [x0, y0, x1, y1] as fractions of the page (top-left origin) or None,
"md": Markdown shown in the viewer, "text": the plain text that is scored, + type-specific fields}.
"""
import io
import os
import re
import time
from collections import Counter

BOLD = re.compile(r"Bold|Black|Heavy|Semibold|Demi|Medi|-Bd|BX|TB\b|TB$", re.I)
NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")


def _el(typ, tool, page, bbox, md, text, **extra):
    d = {"type": typ, "tool": tool, "page": page, "bbox": bbox, "md": md, "text": text}
    d.update(extra)
    return d


def page_count(pdf_path):
    from pypdf import PdfReader
    return len(PdfReader(pdf_path).pages)


def split_pdf(pdf_path, size):
    """PDF -> [(first_page_number, pdf_bytes)] pieces of `size` pages (file cutting only, no text is read)."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(pdf_path)
    out = []
    for start in range(0, len(reader.pages), size):
        w = PdfWriter()
        for i in range(start, min(start + size, len(reader.pages))):
            w.add_page(reader.pages[i])
        buf = io.BytesIO()
        w.write(buf)
        out.append((start + 1, buf.getvalue()))
    return out


# ================================================================== headings: pypdf
def headings_pypdf(pdf_path):
    """pypdf's text visitor gives every text fragment with its font and transformation matrix. Fragments on the same
    baseline are joined into lines; a line is a heading when it is short, all bold, and either larger than the body
    text or numbered ("2.1 Method"). Font size = nominal size x the vertical scale of the text matrix."""
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    pages = []
    for pno, page in enumerate(reader.pages, start=1):
        frags, rotated = [], set()

        def visit(text, cm, tm, font_dict, font_size):
            if not text or not text.strip():
                return
            a, b, c, d, e, f = tm
            A, B, C, D, E, F = cm
            if abs(a * B + b * D) > 0.01 * (abs(a * A + b * C) or 1):      # rotated text (margin stamps): skip
                rotated.add(text.strip())
                return
            x = e * A + f * C + E
            y = e * B + f * D + F
            scale = abs(b * C + d * D) or abs(d) or 1.0
            name = str((font_dict or {}).get("/BaseFont", ""))
            frags.append({"x": x, "y": y, "size": round(font_size * scale, 2), "font": name, "text": text})

        page.extract_text(visitor_text=visit)
        frags = [f for f in frags if f["text"].strip() not in rotated]     # pypdf also reports the stamp unrotated
        w, h = float(page.mediabox.width), float(page.mediabox.height)
        # consecutive fragments on the same baseline = one line (content-stream order keeps columns apart)
        lines = []
        for fr in frags:
            for part_i, part in enumerate(fr["text"].split("\n")):
                if part_i and lines:
                    lines[-1]["closed"] = True
                if not part.strip():
                    continue
                cur = lines[-1] if lines else None
                if cur and not cur.get("closed") and abs(cur["y"] - fr["y"]) < 1.0:
                    cur["parts"].append((part, fr["font"], fr["size"]))
                else:
                    lines.append({"x": fr["x"], "y": fr["y"], "parts": [(part, fr["font"], fr["size"])]})
            if fr["text"].endswith("\n") and lines:
                lines[-1]["closed"] = True
        pages.append((pno, w, h, lines))

    sizes = Counter()
    for _, _, _, lines in pages:
        for ln in lines:
            for t, _, s in ln["parts"]:
                sizes[s] += len(t)
    body = sizes.most_common(1)[0][0] if sizes else 10.0
    top_size = max((s for s in sizes), default=body)

    out = []
    for pno, w, h, lines in pages:
        cand = []
        for ln in lines:
            text = re.sub(r"\s+", " ", "".join(t for t, _, _ in ln["parts"])).strip()
            size = max(s for _, _, s in ln["parts"])
            letters = [(t, f) for t, f, _ in ln["parts"] if re.search(r"[A-Za-z]", t)]
            all_bold = bool(letters) and all(BOLD.search(f) for _, f in letters)
            words = text.split()
            # run-in heading: bold words at the start of a body-size line, normal text after them
            lead = []
            for t, f, sz in ln["parts"]:
                if re.search(r"[A-Za-z]", t) and not BOLD.search(f):
                    break
                lead.append(t)
            head = re.sub(r"\s+", " ", "".join(lead)).strip()
            if (head and head[0].isupper() and not all_bold and size < body * 1.15 and abs(size - body) < 0.6 and len(head.split()) <= 12
                    and len(re.findall(r"[A-Za-z]", head)) >= 4 and (len(head.split()) >= 2 or head.endswith((".", ":")))):
                cand.append({"text": head.rstrip(".:").strip(), "size": size, "x": ln["x"], "y": ln["y"], "run_in": True})
                continue
            if not words or len(words) > 14 or text.endswith((".", ",", ";")) or len(re.findall(r"[A-Za-z]", text)) < 3:
                continue
            big = size >= body * 1.15
            numbered = bool(NUMBERED.match(text)) and size >= body * 0.98
            if all_bold and (big or numbered) or (size >= body * 1.5):
                cand.append({"text": text, "size": size, "x": ln["x"], "y": ln["y"]})
        # a title broken over two lines: same size, right below each other -> one heading
        merged = []
        for c in cand:
            p = merged[-1] if merged else None
            if p and not p.get("run_in") and not c.get("run_in") and abs(p["size"] - c["size"]) < 0.3 \
                    and 0 < p["y_last"] - c["y"] < 1.6 * c["size"]:
                p["text"] += " " + c["text"]
                p["y_last"] = c["y"]
                continue
            merged.append({**c, "y_last": c["y"]})
        for c in merged:
            m = NUMBERED.match(c["text"])
            level = 1 if (pno == 1 and c["size"] >= top_size - 0.01) else (m.group(1).count(".") + 2 if m else 2)
            if c.get("run_in"):
                level = 4
            # pypdf gives no line width: the box width is estimated from the text length (shown dashed)
            x0 = c["x"] / w
            x1 = min(1.0, (c["x"] + 0.5 * c["size"] * len(c["text"])) / w)
            y0 = (h - c["y"] - c["size"]) / h
            y1 = (h - c["y_last"] + 0.3 * c["size"]) / h
            out.append(_el("heading", "pypdf", pno, [x0, y0, x1, y1], "#" * level + " " + c["text"], c["text"],
                           level=level, approx_box=True, font_size=c["size"]))
    return out


# ================================================================== tables: pdfplumber
def _rule_regions(page):
    """Areas between long horizontal rules (booktabs tables have no vertical lines)."""
    rules = sorted((o for o in page.lines + page.rects if o["bottom"] - o["top"] < 2 and o["x1"] - o["x0"] > 150),
                   key=lambda o: o["top"])
    regions, cur = [], []
    for r in rules:
        if cur and r["top"] - cur[-1]["top"] > 200:
            regions.append(cur)
            cur = []
        cur.append(r)
    if cur:
        regions.append(cur)
    out = []
    for rs in regions:
        if len(rs) >= 2:
            out.append((min(r["x0"] for r in rs) - 2, rs[0]["top"] - 2, max(r["x1"] for r in rs) + 2, rs[-1]["bottom"] + 2))
    return out


def tables_pdfplumber(pdf_path):
    """pdfplumber's find_tables: first with its default (ruled grid) settings; for tables drawn with horizontal rules
    only, on the area between the rules with text-based column and row detection. Empty rows are dropped."""
    import pdfplumber
    out = []
    with pdfplumber.open(pdf_path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            W, H = float(page.width), float(page.height)
            found = []
            for t in page.find_tables():                                    # fully ruled tables
                rows = t.extract(x_tolerance=1.5)
                if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                    found.append((t.bbox, rows))
            for box in _rule_regions(page):
                if any(not (box[2] < b[0] or b[2] < box[0] or box[3] < b[1] or b[3] < box[1]) for b, _ in found):
                    continue
                area = page.crop((max(box[0], 0), max(box[1], 0), min(box[2], W), min(box[3], H)))
                for t in area.find_tables({"vertical_strategy": "text", "horizontal_strategy": "text"}):
                    rows = t.extract(x_tolerance=1.5)
                    if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
                        found.append((t.bbox, rows))
            for bbox, rows in found:
                rows = [[re.sub(r"\s+", " ", c or "").strip() for c in r] for r in rows]
                rows = [r for r in rows if any(r)]
                if len(rows) < 2:
                    continue
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                md = "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width] +
                               ["| " + " | ".join(r) + " |" for r in rows[1:]])
                out.append(_el("table", "pdfplumber", pno, [bbox[0] / W, bbox[1] / H, bbox[2] / W, bbox[3] / H],
                               md, " ".join(c for r in rows for c in r), cells=rows))
    return out


# ================================================================== text: MarkItDown
def text_markitdown(pdf_path):
    """MarkItDown converts each page (the PDF is cut into one-page files first, so we know the page) and its
    paragraphs (blocks separated by a blank line) become text elements. MarkItDown returns no positions: the
    reading-order step locates each paragraph on the page (ordering.py)."""
    from markitdown import MarkItDown
    from markitdown.converters import _pdf_converter as pdfconv
    md = MarkItDown()
    # MarkItDown checks every page for "form-like" content (aligned words, e.g. figures and tables). Such pages are
    # read line by line with pdfplumber's extract_text, which runs straight across both columns and drops the spaces
    # in fonts like this ("ACMSustainabilityWeek"). Pages without it go to pdfminer, which keeps columns and spaces.
    # Default here: the form check is switched off, so every page uses MarkItDown's pdfminer reader.
    # MARKITDOWN_AS_IS=1 (run.py --markitdown-as-is) keeps MarkItDown's own behaviour for comparison.
    as_is = os.environ.get("MARKITDOWN_AS_IS") == "1"
    original = pdfconv._extract_form_content_from_words
    if not as_is:
        pdfconv._extract_form_content_from_words = lambda page: None
    out = []
    try:
        pages = [(pno, md.convert_stream(io.BytesIO(data), file_extension=".pdf").text_content or "")
                 for pno, data in split_pdf(pdf_path, 1)]
    finally:
        pdfconv._extract_form_content_from_words = original
    for pno, txt in pages:
        for block in re.split(r"\n\s*\n", txt):
            flat = re.sub(r"\s+", " ", block).strip()
            if len(flat) >= 2:
                out.append(_el("text", "markitdown", pno, None, flat, flat))
    return out


# ================================================================== formulas: Azure Document Intelligence
def azure_credentials():
    for e, k in (("AZURE_DI_ENDPOINT", "AZURE_DI_KEY"),
                 ("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", "AZURE_DOCUMENT_INTELLIGENCE_KEY"),
                 ("AZURE_FORM_RECOGNIZER_ENDPOINT", "AZURE_FORM_RECOGNIZER_KEY")):
        if os.environ.get(e) and os.environ.get(k):
            return os.environ[e].rstrip("/"), os.environ[k]
    raise LookupError("set AZURE_DI_ENDPOINT and AZURE_DI_KEY (see README)")


def _azure_analyze(data, feature):
    """One prebuilt-layout call with an add-on feature; returns analyzeResult."""
    import requests
    endpoint, key = azure_credentials()
    url = f"{endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze?api-version=2024-11-30&features={feature}"
    r = requests.post(url, data=data, timeout=120,
                      headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/pdf"})
    if r.status_code != 202:
        raise RuntimeError(f"Azure HTTP {r.status_code}: {r.text[:300]}")
    op = r.headers["Operation-Location"]
    j = {}
    for _ in range(240):
        time.sleep(1.0)
        j = requests.get(op, headers={"Ocp-Apim-Subscription-Key": key}, timeout=60).json()
        if j.get("status") in ("succeeded", "failed"):
            break
    if j.get("status") != "succeeded":
        raise RuntimeError(f"Azure analysis {j.get('status')}: {str(j.get('error', ''))[:300]}")
    return j["analyzeResult"]


def keyvalues_azure(pdf_path):
    """prebuilt-layout with the keyValuePairs add-on (free add-on; it replaced the old general-document model).
    The free tier (F0) reads 2 pages per request, so the PDF is sent in 2-page pieces (AZURE_CHUNK_PAGES=0: whole)."""
    size = int(os.environ.get("AZURE_CHUNK_PAGES", "2")) or 10_000
    out = []
    for first, data in split_pdf(pdf_path, size):
        res = _azure_analyze(data, "keyValuePairs")
        dims = {pg["pageNumber"]: (pg.get("width") or 1, pg.get("height") or 1) for pg in res.get("pages", [])}
        for kv in res.get("keyValuePairs", []):
            k = (kv.get("key") or {}).get("content", "").strip().rstrip(":").strip()
            v = (kv.get("value") or {}).get("content", "").strip()
            if not k and not v:
                continue
            regions = (kv.get("key") or {}).get("boundingRegions", []) + (kv.get("value") or {}).get("boundingRegions", [])
            pno = regions[0]["pageNumber"] if regions else 1
            pw, ph = dims.get(pno, (1, 1))
            pts = [p for r in regions if r["pageNumber"] == pno for p in [r.get("polygon") or []]]
            xs = [x for poly in pts for x in poly[0::2]]
            ys = [y for poly in pts for y in poly[1::2]]
            bbox = [min(xs) / pw, min(ys) / ph, max(xs) / pw, max(ys) / ph] if xs else None
            out.append(_el("key_value", "azure", pno + first - 1, bbox, f"**{k}:** {v}", f"{k}: {v}",
                           key=k, value=v, tool_confidence=kv.get("confidence")))
    return out


# ================================================================== formulas: Amazon Textract
EQNUM = re.compile(r"\(\s*\d{1,3}\s*\)\s*$")
MATHSIGN = re.compile(r"[=≤≥<>∑Σ∫←→∈∝≈∥±×·]")


def formulas_textract(pdf_path):
    """Textract has NO formula feature (its Layout elements are title, header, footer, section header, page number,
    list, figure, table, key-value and text). So Textract only READS the page (DetectDocumentText on a 150 dpi page
    image) and a rule picks the formulas out of its lines: a line ending with an equation number "(3)" that holds a
    math sign is a display formula; short lines right above or below it (fraction parts, sum limits) are joined.
    The result is plain text, not LaTeX. Credentials: your AWS login; region TEXTRACT_REGION or AWS_REGION."""
    import boto3
    import pdfplumber
    client = boto3.client("textract", region_name=os.environ.get("TEXTRACT_REGION", os.environ.get("AWS_REGION", "us-east-1")))
    out = []
    with pdfplumber.open(pdf_path) as pdf:                      # only used to render the page image
        images = []
        for page in pdf.pages:
            buf = io.BytesIO()
            page.to_image(resolution=150).original.convert("RGB").save(buf, format="PNG")
            images.append(buf.getvalue())
    for pno, png in enumerate(images, start=1):
        r = client.detect_document_text(Document={"Bytes": png})
        lines = []
        for b in r["Blocks"]:
            if b["BlockType"] == "LINE":
                g = b["Geometry"]["BoundingBox"]
                lines.append({"text": b.get("Text", ""), "x0": g["Left"], "y0": g["Top"],
                              "x1": g["Left"] + g["Width"], "y1": g["Top"] + g["Height"], "conf": b.get("Confidence", 0)})
        used = set()
        for i, ln in enumerate(lines):
            if i in used or not (EQNUM.search(ln["text"]) and MATHSIGN.search(ln["text"])):
                continue
            h = ln["y1"] - ln["y0"]
            group = [i]
            for j, o in enumerate(lines):                       # fraction parts / limits just above or below
                if j == i or j in used or len(o["text"]) > 15:
                    continue
                near = ln["y0"] - 1.3 * h <= (o["y0"] + o["y1"]) / 2 <= ln["y1"] + 1.3 * h
                inside = o["x0"] >= ln["x0"] - 0.01 and o["x1"] <= ln["x1"] + 0.01
                if near and inside:
                    group.append(j)
            used.update(group)
            parts = sorted((lines[k] for k in group), key=lambda o: (round(o["y0"], 3), o["x0"]))
            text = " ".join(EQNUM.sub("", p["text"]).strip() if p is ln else p["text"] for p in parts).strip()
            bbox = [min(p["x0"] for p in parts), min(p["y0"] for p in parts),
                    max(p["x1"] for p in parts), max(p["y1"] for p in parts)]
            out.append(_el("formula", "textract", pno, bbox, text, text, latex=text, plain_text=True,
                           tool_confidence=round(ln["conf"] / 100, 3)))
    return out


# ================================================================== images: Docling
def images_docling(pdf_path):
    """Docling's layout model finds pictures; caption_text() gives the caption Docling linked to each picture.
    OCR and table structure are switched off (only pictures are needed, and the PDFs have a text layer)."""
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = False
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    doc = conv.convert(pdf_path).document
    out = []
    for n, pic in enumerate(doc.pictures, start=1):
        if not pic.prov:
            continue
        prov = pic.prov[0]
        size = doc.pages[prov.page_no].size
        bb = prov.bbox.to_top_left_origin(page_height=size.height)
        caption = (pic.caption_text(doc) or "").strip()
        bbox = [bb.l / size.width, bb.t / size.height, bb.r / size.width, bb.b / size.height]
        out.append(_el("image", "docling", prov.page_no, bbox, f"![figure {n}](crop)\n\n{caption}".strip(), caption,
                       caption=caption))
    return out


# ================================================================== web pages
def fetch_html(src):
    """URL -> requests GET; a saved .html file -> read from disk."""
    if src.startswith("http"):
        import requests
        r = requests.get(src, timeout=60, headers={"User-Agent": "tool-per-element-experiment/1.0"})
        r.raise_for_status()
        return r.text
    with open(src, encoding="utf-8", errors="replace") as f:
        return f.read()


def _web_root(html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer"]):
        t.decompose()
    root = soup.select_one("main") or soup.select_one("#mw-content-text") or soup.body or soup
    return soup, root


def headings_bs4(html):
    soup, root = _web_root(html)
    out = []
    h1 = soup.find("h1")
    tags = ([h1] if h1 else []) + root.find_all(["h2", "h3", "h4"])
    for n, t in enumerate(tags):
        txt = re.sub(r"\[\s*edit\s*\]", "", t.get_text(" ", strip=True)).strip()
        if txt:
            lvl = int(t.name[1])
            out.append(_el("heading", "beautifulsoup", 1, None, "#" * lvl + " " + txt, txt, level=lvl, order_hint=n))
    return out


def tables_bs4(html):
    _, root = _web_root(html)
    out = []
    for t in root.find_all("table"):
        if "infobox" in " ".join(t.get("class", [])) or t.find_parent("table") is not None:
            continue                                    # the infobox is key-value data, not a data table
        rows = [[re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
                for tr in t.find_all("tr")]
        rows = [r for r in rows if any(r)]
        if len(rows) < 2:
            continue
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        md = "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width] +
                       ["| " + " | ".join(r) + " |" for r in rows[1:]])
        out.append(_el("table", "beautifulsoup", 1, None, md, " ".join(c for r in rows for c in r), cells=rows))
    return out


def text_markitdown_html(html):
    from markitdown import MarkItDown
    txt = MarkItDown().convert_stream(io.BytesIO(html.encode("utf-8")), file_extension=".html").text_content or ""
    out = []
    for block in re.split(r"\n\s*\n", txt):
        b = block.strip()
        if len(b) < 40 or b.startswith(("#", "|", "!", "*", "-", "[")):   # MarkItDown's own headings/tables/lists/links
            continue
        flat = re.sub(r"\s+", " ", b)
        out.append(_el("text", "markitdown", 1, None, flat, flat))
    return out


def images_docling_html(html):
    import tempfile
    from docling.document_converter import DocumentConverter
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
    doc = DocumentConverter().convert(f.name).document
    out = []
    for n, pic in enumerate(doc.pictures, start=1):
        cap = (pic.caption_text(doc) or "").strip()
        out.append(_el("image", "docling", 1, None, f"![figure {n}](web)\n\n{cap}".strip(), cap, caption=cap, order_hint=n))
    return out


PDF_PLAN = [("heading", "pypdf", headings_pypdf), ("table", "pdfplumber", tables_pdfplumber),
            ("text", "markitdown", text_markitdown), ("formula", "textract", formulas_textract),
            ("key_value", "azure", keyvalues_azure), ("image", "docling", images_docling)]
WEB_PLAN = [("heading", "beautifulsoup", headings_bs4), ("table", "beautifulsoup", tables_bs4),
            ("text", "markitdown", text_markitdown_html), ("formula", "textract", None),
            ("key_value", "azure", None), ("image", "docling", images_docling_html)]

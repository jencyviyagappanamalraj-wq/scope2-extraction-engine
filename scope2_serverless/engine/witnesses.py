"""Witness tools: independent extractors that read the same document, so the engine can compare and vote.

Every witness returns the same shape:
    {"tool": "azure", "status": "ok" | "unavailable" | "error", "error": "...", "seconds": 3.2,
     "pages": {1: {"text": "...", "tables": [[["cell", ...], ...], ...], "formulas": ["latex", ...],
                   "paragraphs": [{"text": "...", "role": "title"}], "ocr_confidence": 0.98}, ...}}
A witness that is not installed, has no key or is denied returns status "unavailable"/"error" and the job goes on
without it. Which witnesses run (env WITNESSES_STANDARD / WITNESSES_ACCURATE, or WITNESSES, default per level):
    standard: pypdf, markitdown, docling            (local, free)
    accurate: pypdf, markitdown, docling, azure, textract
"""
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

LOCAL = ["pypdf", "markitdown", "docling"]
CLOUD = ["azure", "textract"]


def default_tools(level):
    """WITNESSES_STANDARD / WITNESSES_ACCURATE (set by the deployment) or WITNESSES (both levels, local runs)."""
    env = os.environ.get(f"WITNESSES_{level.upper()}") or os.environ.get("WITNESSES")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    return LOCAL + (CLOUD if level == "accurate" else [])


def _pages_of(pdf_path):
    from pypdf import PdfReader
    return len(PdfReader(pdf_path).pages)


def _split(pdf_path, size):
    """PDF -> list of (first_page_number, pdf_bytes) chunks of `size` pages."""
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


# ------------------------------------------------------------------ pypdf
def w_pypdf(pdf_path):
    from pypdf import PdfReader
    reader = PdfReader(pdf_path)
    return {i: {"text": p.extract_text() or ""} for i, p in enumerate(reader.pages, start=1)}


# ------------------------------------------------------------------ MarkItDown (one page at a time: it has no page breaks)
def w_markitdown(pdf_path):
    from markitdown import MarkItDown
    md = MarkItDown()
    pages = {}
    for first, data in _split(pdf_path, 1):
        pages[first] = {"text": md.convert_stream(io.BytesIO(data), file_extension=".pdf").text_content or ""}
    return pages


# ------------------------------------------------------------------ Docling (layout model + TableFormer + formula model)
_docling = None


def _docling_converter():
    global _docling
    if _docling is None:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        opts = PdfPipelineOptions()
        if os.environ.get("DOCLING_ARTIFACTS_PATH"):            # models baked into the container image (no download)
            opts.artifacts_path = os.environ["DOCLING_ARTIFACTS_PATH"]
        opts.do_formula_enrichment = os.environ.get("DOCLING_FORMULAS", "1") == "1"   # formulas -> LaTeX
        opts.do_table_structure = True
        _docling = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _docling


def w_docling(pdf_path):
    doc = _docling_converter().convert(pdf_path).document
    pages = {}
    for item, _level in doc.iterate_items():
        prov = getattr(item, "prov", None)
        if not prov:
            continue
        p = pages.setdefault(prov[0].page_no, {"text": "", "tables": [], "formulas": [], "paragraphs": []})
        label = str(getattr(item, "label", "")).lower()
        if hasattr(item, "data") and hasattr(item.data, "grid"):                      # TableItem
            grid = [[(c.text or "").strip() for c in row] for row in item.data.grid]
            p["tables"].append(grid)
            p["text"] += "\n" + "\n".join(" ".join(r) for r in grid)
        elif "formula" in label:
            p["formulas"].append(getattr(item, "text", "") or "")
        else:
            t = getattr(item, "text", "") or ""
            p["text"] += "\n" + t
            p["paragraphs"].append({"text": t, "role": label})
    return pages


# ------------------------------------------------------------------ Azure AI Document Intelligence (REST, prebuilt-layout)
def w_azure(pdf_path):
    endpoint, key = os.environ.get("AZURE_DI_ENDPOINT", "").rstrip("/"), os.environ.get("AZURE_DI_KEY", "")
    if not endpoint or not key:
        raise LookupError("AZURE_DI_ENDPOINT / AZURE_DI_KEY not set")
    # free tier (F0) reads only 2 pages per request -> send 2-page pieces; paid tier: AZURE_CHUNK_PAGES=0 (whole file)
    size = int(os.environ.get("AZURE_CHUNK_PAGES", "2")) or 10_000
    url = (f"{endpoint}/documentintelligence/documentModels/prebuilt-layout:analyze"
           "?api-version=2024-11-30&features=formulas")
    pages = {}
    for first, data in _split(pdf_path, size):
        r = requests.post(url, data=data, timeout=60,
                          headers={"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/pdf"})
        if r.status_code != 202:
            raise RuntimeError(f"Azure HTTP {r.status_code}: {r.text[:200]}")
        op = r.headers["Operation-Location"]
        for _ in range(120):
            time.sleep(1.5)
            j = requests.get(op, headers={"Ocp-Apim-Subscription-Key": key}, timeout=30).json()
            if j.get("status") in ("succeeded", "failed"):
                break
        if j.get("status") != "succeeded":
            raise RuntimeError(f"Azure analysis {j.get('status')}: {json.dumps(j.get('error', ''))[:200]}")
        res = j["analyzeResult"]
        off = first - 1
        for pg in res.get("pages", []):
            n = pg["pageNumber"] + off
            words = pg.get("words", [])
            pages[n] = {"text": "\n".join(l["content"] for l in pg.get("lines", [])), "tables": [], "paragraphs": [],
                        "formulas": [f.get("value", "") for f in pg.get("formulas", []) if f.get("kind") == "display"],
                        "inline_formulas": [f.get("value", "") for f in pg.get("formulas", []) if f.get("kind") == "inline"],
                        "ocr_confidence": round(sum(w.get("confidence", 1) for w in words) / len(words), 3) if words else None}
        for para in res.get("paragraphs", []):
            br = (para.get("boundingRegions") or [{}])[0]
            if br.get("pageNumber"):
                pages[br["pageNumber"] + off]["paragraphs"].append({"text": para["content"], "role": para.get("role", "text")})
        for t in res.get("tables", []):
            br = (t.get("boundingRegions") or [{}])[0]
            grid = [["" for _ in range(t["columnCount"])] for _ in range(t["rowCount"])]
            for c in t.get("cells", []):
                grid[c["rowIndex"]][c["columnIndex"]] = c.get("content", "")
            if br.get("pageNumber"):
                pages[br["pageNumber"] + off]["tables"].append(grid)
    return pages


# ------------------------------------------------------------------ Amazon Textract (page images, LAYOUT + TABLES)
def w_textract(pdf_path):
    import boto3
    import pdfplumber
    client = boto3.client("textract", region_name=os.environ.get("TEXTRACT_REGION", os.environ.get("AWS_REGION", "us-east-1")))
    pages = {}
    with pdfplumber.open(pdf_path) as pdf:
        for n, page in enumerate(pdf.pages, start=1):
            buf = io.BytesIO()
            page.to_image(resolution=150).original.convert("RGB").save(buf, format="PNG")
            r = client.analyze_document(Document={"Bytes": buf.getvalue()}, FeatureTypes=["TABLES", "LAYOUT"])
            blocks = {b["Id"]: b for b in r["Blocks"]}
            lines = [b for b in r["Blocks"] if b["BlockType"] == "LINE"]
            words = [b for b in r["Blocks"] if b["BlockType"] == "WORD"]
            tables = []
            for t in (b for b in r["Blocks"] if b["BlockType"] == "TABLE"):
                cells = [blocks[i] for rel in t.get("Relationships", []) if rel["Type"] == "CHILD" for i in rel["Ids"]]
                cells = [c for c in cells if c["BlockType"] == "CELL"]
                if not cells:
                    continue
                grid = [["" for _ in range(max(c["ColumnIndex"] for c in cells))] for _ in range(max(c["RowIndex"] for c in cells))]
                for c in cells:
                    txt = " ".join(blocks[i].get("Text", "") for rel in c.get("Relationships", []) if rel["Type"] == "CHILD"
                                   for i in rel["Ids"] if blocks[i]["BlockType"] == "WORD")
                    grid[c["RowIndex"] - 1][c["ColumnIndex"] - 1] = txt
                tables.append(grid)
            pages[n] = {"text": "\n".join(l.get("Text", "") for l in lines), "tables": tables,
                        "ocr_confidence": round(sum(w.get("Confidence", 100) for w in words) / len(words) / 100, 3) if words else None}
    return pages


# ------------------------------------------------------------------ web pages
def w_markitdown_html(html):
    from markitdown import MarkItDown
    return {1: {"text": MarkItDown().convert_stream(io.BytesIO(html.encode("utf-8")), file_extension=".html").text_content or ""}}


def w_docling_html(html):
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
    from docling.document_converter import DocumentConverter
    md = DocumentConverter().convert(f.name).document.export_to_markdown()
    return {1: {"text": md}}


PDF_TOOLS = {"pypdf": w_pypdf, "markitdown": w_markitdown, "docling": w_docling, "azure": w_azure, "textract": w_textract}
HTML_TOOLS = {"markitdown": w_markitdown_html, "docling": w_docling_html}


def _one(name, fn, arg):
    t0 = time.time()
    try:
        pages = fn(arg)
        return {"tool": name, "status": "ok", "seconds": round(time.time() - t0, 2), "pages": pages}
    except (ImportError, LookupError) as e:                       # not installed / no key
        return {"tool": name, "status": "unavailable", "error": f"{type(e).__name__}: {str(e)[:200]}",
                "seconds": round(time.time() - t0, 2), "pages": {}}
    except Exception as e:                                         # denied, timeout, bad answer
        return {"tool": name, "status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}",
                "seconds": round(time.time() - t0, 2), "pages": {}}


def run_witnesses(src, level, html=None):
    """Run the witnesses in parallel. src = PDF path, or a URL with html = the page's HTML."""
    tools = default_tools(level)
    table = HTML_TOOLS if html is not None else PDF_TOOLS
    arg = html if html is not None else src
    names = [t for t in tools if t in table]
    with ThreadPoolExecutor(max(1, len(names))) as ex:
        results = list(ex.map(lambda n: _one(n, table[n], arg), names))
    for r in results:
        r["pages"] = {int(k): v for k, v in r["pages"].items()}
        print(json.dumps({"witness": r["tool"], "status": r["status"], "seconds": r["seconds"],
                          "pages": len(r["pages"]), "error": r.get("error", "")}))
    return results

"""Amazon Textract on its own: the Markdown it produces for a PDF, scored against the answer key.

    python scripts\\textract_markdown.py ..\\data\\test_docs\\Paper1.pdf
    -> scripts\\textract_out\\Paper1_textract.md      (the full Markdown)
    -> scripts\\textract_out\\Paper1_textract_score.md (score per element type against eval\\ground_truth)

Textract AnalyzeDocument with LAYOUT + TABLES, one call per page image (150 dpi). Cost: TABLES $0.015 + LAYOUT $0.004
per page: Paper1 has 4 pages = about $0.08. Free tier for new accounts: 100 pages a
month for 3 months. Needs AWS credentials in this window (aws sso login, or the PowerShell lines from the AWS access
portal) and AWS_REGION (default us-east-1).

Markdown built from Textract's layout blocks, in the order Textract returns them (its reading order):
  LAYOUT_TITLE -> "# ", LAYOUT_SECTION_HEADER -> "## ", LAYOUT_TEXT / LAYOUT_LIST / LAYOUT_KEY_VALUE -> paragraph,
  LAYOUT_TABLE -> Markdown table from the TABLE cells, LAYOUT_FIGURE -> "![figure]" (its caption is the next text block
  starting with "Figure"), LAYOUT_HEADER / LAYOUT_FOOTER / LAYOUT_PAGE_NUMBER -> left out.
Textract has no formula and no heading-level output: formulas come out as plain text lines inside paragraphs, and
every section header gets the same level.

Scoring uses the answer key only (tool_per_element/scoring.py): 1.0 = exactly as printed.
"""
import io
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent                                  # scope2-extraction-engine
sys.path.insert(0, str(ROOT / "tool_per_element"))


def page_pngs(pdf):
    import pdfplumber
    out = []
    with pdfplumber.open(pdf) as doc:
        for page in doc.pages:
            buf = io.BytesIO()
            page.to_image(resolution=150).original.convert("RGB").save(buf, format="PNG")
            out.append(buf.getvalue())
    return out


def textract_pages(pdf):
    import boto3
    client = boto3.client("textract", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return [client.analyze_document(Document={"Bytes": png}, FeatureTypes=["LAYOUT", "TABLES"]) for png in page_pngs(pdf)]


def to_elements(responses):
    """Textract blocks -> elements (type, page, text, md) in Textract's reading order."""
    els = []
    for pno, r in enumerate(responses, start=1):
        blocks = {b["Id"]: b for b in r["Blocks"]}

        def kids(b, typ=None):
            ids = [i for rel in b.get("Relationships", []) if rel["Type"] == "CHILD" for i in rel["Ids"]]
            return [blocks[i] for i in ids if i in blocks and (typ is None or blocks[i]["BlockType"] == typ)]

        def text_of(b):
            lines = kids(b, "LINE")
            if lines:
                return " ".join(l.get("Text", "") for l in lines).strip()
            words = kids(b, "WORD")
            return " ".join(w.get("Text", "") for w in words).strip()

        def table_rows(t):
            cells = kids(t, "CELL") + [c for m in kids(t, "MERGED_CELL") for c in []]
            if not cells:
                return []
            nr = max(c["RowIndex"] for c in cells)
            nc = max(c["ColumnIndex"] for c in cells)
            grid = [[""] * nc for _ in range(nr)]
            for c in cells:
                grid[c["RowIndex"] - 1][c["ColumnIndex"] - 1] = text_of(c)
            return grid

        tables = [b for b in r["Blocks"] if b["BlockType"] == "TABLE"]
        used_tables = set()
        for b in r["Blocks"]:
            bt = b["BlockType"]
            if not bt.startswith("LAYOUT_") or bt in ("LAYOUT_HEADER", "LAYOUT_FOOTER", "LAYOUT_PAGE_NUMBER"):
                continue
            g = b["Geometry"]["BoundingBox"]
            if bt == "LAYOUT_TABLE":
                # the TABLE block whose box overlaps this layout box most
                best = max(tables, default=None, key=lambda t: _overlap(t["Geometry"]["BoundingBox"], g))
                rows = table_rows(best) if best is not None and _overlap(best["Geometry"]["BoundingBox"], g) > 0 else []
                if best is not None:
                    used_tables.add(best["Id"])
                if len(rows) >= 2:
                    md = "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])] +
                                   ["| " + " | ".join(x) + " |" for x in rows[1:]])
                    els.append({"type": "table", "page": pno, "cells": rows, "text": " ".join(c for x in rows for c in x), "md": md})
                continue
            if bt == "LAYOUT_FIGURE":
                els.append({"type": "image", "page": pno, "caption": "", "text": "", "md": f"![figure on page {pno}]()"})
                continue
            # LAYOUT_TEXT / LIST / KEY_VALUE / TITLE / SECTION_HEADER: their children are LINEs or other LAYOUT blocks
            parts = []
            for c in kids(b):
                parts.append(c.get("Text", "") if c["BlockType"] == "LINE" else text_of(c))
            txt = re.sub(r"\s+", " ", " ".join(parts)).strip()
            if not txt:
                continue
            if bt == "LAYOUT_TITLE":
                els.append({"type": "heading", "page": pno, "text": txt, "md": "# " + txt})
            elif bt == "LAYOUT_SECTION_HEADER":
                els.append({"type": "heading", "page": pno, "text": txt, "md": "## " + txt})
            else:
                els.append({"type": "text", "page": pno, "text": txt, "md": txt})
    # a figure's caption = the next text block on the same page starting with "Figure"
    for i, e in enumerate(els):
        if e["type"] == "image":
            for nxt in els[i + 1:i + 4]:
                if nxt["page"] == e["page"] and nxt["type"] == "text" and re.match(r"(Figure|Fig\.)\s*\d+", nxt["text"]):
                    e["caption"] = e["text"] = nxt["text"]
                    break
    return els


def _overlap(a, b):
    x = max(0, min(a["Left"] + a["Width"], b["Left"] + b["Width"]) - max(a["Left"], b["Left"]))
    y = max(0, min(a["Top"] + a["Height"], b["Top"] + b["Height"]) - max(a["Top"], b["Top"]))
    return x * y


def find_key(pdf):
    for f in sorted((ROOT / "eval" / "ground_truth").glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("doc") == Path(pdf).name:
            return d["elements"]
    return []


def score(els, key):
    import scoring
    keyc = [dict(k) for k in key]
    _, best = scoring.score(els, keyc)
    # formulas and key-values: Textract has no element for them; look for them inside its text paragraphs
    text_all = {}
    for e in els:
        if e["type"] == "text":
            text_all.setdefault(e["page"], []).append(e["text"])
    rows = []
    for typ in ("heading", "text", "table", "formula", "key_value", "image"):
        ks = [(i, k) for i, k in enumerate(keyc) if k["type"] == typ]
        if not ks:
            continue
        if typ in ("formula", "key_value"):
            vals = []
            for i, k in ks:
                target = (k.get("alternatives") or [k.get("latex", "")])[0] if typ == "formula" else f'{k["key"]}: {k["value"]}'
                vals.append(max((scoring.window_sim(target, t) for t in text_all.get(k.get("page", 1), [])), default=0.0))
            note = "no formula element; best match inside paragraphs" if typ == "formula" else "searched inside paragraphs"
        else:
            vals = [best[i] for i, _ in ks]
            note = ""
        got = [e for e in els if e["type"] == typ]
        rows.append({"type": typ, "in_key": len(ks), "textract_elements": len(got),
                     "found": sum(v >= (0.6 if typ in ("text", "formula", "key_value") else 0.5) for v in vals),
                     "exact": sum(v >= 0.999 for v in vals), "coverage": round(sum(vals) / len(vals), 3),
                     "false_extra": sum(1 for e in got if e.get("confidence") == 0.0), "note": note,
                     "below_exact": [(round(v, 3), scoring.key_text(k)[:80]) for v, (i, k) in zip(vals, ks) if v < 0.999]})
    return rows


def main():
    pdf = sys.argv[1]
    out = HERE / "textract_out"
    out.mkdir(exist_ok=True)
    name = Path(pdf).stem
    t0 = time.perf_counter()
    responses = textract_pages(pdf)
    sec = time.perf_counter() - t0
    (out / f"{name}_textract_raw.json").write_text(json.dumps(responses, default=str), encoding="utf-8")
    els = to_elements(responses)
    md = "\n\n".join(e["md"] + (("\n\n" + e["caption"]) if e["type"] == "image" and e.get("caption") else "")
                     for e in els if not (e["type"] == "text" and any(x["type"] == "image" and x.get("caption") == e["text"] for x in els)))
    (out / f"{name}_textract.md").write_text(md + "\n", encoding="utf-8")
    key = find_key(pdf)
    rows = score(els, key) if key else []
    lines = [f"# Textract on {Path(pdf).name}", "", f"Pages: {len(responses)} · Textract time: {sec:.1f} s · "
             f"cost ≈ ${0.019 * len(responses):.3f} (TABLES + LAYOUT)", "",
             "| Element | In answer key | Textract elements | Found | Exact | Coverage | Not in key (score 0) | Note |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['type']} | {r['in_key']} | {r['textract_elements']} | {r['found']} | {r['exact']} | "
                     f"{r['coverage']} | {r['false_extra']} | {r['note']} |")
    lines.append("")
    for r in rows:
        if r["below_exact"]:
            lines.append(f"**{r['type']} below 1.0:**")
            lines += [f"- {v}: {t}" for v, t in r["below_exact"]]
            lines.append("")
    (out / f"{name}_textract_score.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[:4 + len(rows) + 2]))
    print(f"\nMarkdown: {out / (name + '_textract.md')}\nScore:    {out / (name + '_textract_score.md')}")


if __name__ == "__main__":
    main()

"""Sanity-check the ground-truth JSON files against the real source documents.

A wrong ground truth silently lowers every tool's score, so check it before you trust any number.

    python eval/check_ground_truth.py

For every ground-truth element it asks: can I find this text in the source document itself?
  * PDF   -> text of the page given in "page" (pdfplumber, whitespace/hyphen/case-insensitive)
  * web   -> text of the cached HTML (data/web_cache/<name>.html)
Result per element: OK, WRONG_PAGE (found on another page), or NOT_FOUND (typo, paraphrase or invented).
Formulas and images cannot be matched as text, so they are listed under "LOOK AT THESE YOURSELF".
Nothing here replaces your eyes; it only tells you where to look first.
"""
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE if (HERE / "eval").exists() else HERE.parent   # works from eval/ or scripts/
GT_DIR = ROOT / "eval" / "ground_truth"
PDF_KEYS = {"protac_permeability": "protac", "hvac_nomad_rl": "hvac"}
FUZZY = 0.9


sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "eval"))
from run_baselines import find_pdfs      # same Paper1/Paper2 detection as the baseline runner


from make_ground_truth import norm, pages_text      # same spacing-proof lookup as the builder


def found(needle, haystack):
    n = norm(needle)
    if not n:
        return True
    if n in haystack:
        return True
    if len(n) < 25:
        return False
    # sliding fuzzy match for longer sentences (OCR/line-break differences)
    step = max(10, len(n) // 4)
    best = 0.0
    for i in range(0, max(1, len(haystack) - len(n) + 1), step):
        r = difflib.SequenceMatcher(None, n, haystack[i:i + len(n) + 20]).ratio()
        best = max(best, r)
        if best >= FUZZY:
            return True
    return False


def element_text(el):
    t = el["type"]
    if t in ("heading", "text"):
        return [el["text"]]
    if t == "key_value":
        return [el["key"], el["value"]]
    if t == "table":
        return [c for row in el["cells"] for c in row if str(c).strip()]
    return []


def html_text(path):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    for e in soup(["script", "style"]):
        e.decompose()
    for sup in soup.find_all("sup"):                     # citation / footnote markers: [14], [a], [citation needed]
        if re.fullmatch(r"\[\s*[\w\s.]{1,30}\s*\]", sup.get_text(strip=True)):
            sup.decompose()
    for e in soup.select(".mw-editsection, .navbox, .reflist, .refbegin, .mw-references-wrap, .noprint, .metadata, .bday, [style*='display:none']"):
        e.decompose()
    return norm(soup.get_text(" "))


def main():
    problems = manual = total = 0
    for gt_path in sorted(GT_DIR.glob("*.json")):
        name = gt_path.stem
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        pages, whole = None, None
        if name in PDF_KEYS:
            pdf = find_pdfs().get(name)
            if not pdf:
                print(f"\n{name}: PDF not found under data/  (skipped)")
                continue
            pages = pages_text(pdf)
            whole = " ".join(pages)
        else:
            html = ROOT / "data" / "web_cache" / f"{name}.html"
            if not html.exists():
                print(f"\n{name}: no PDF and no {html.name} for this file. If it is an OLD ground-truth file "
                      f"(e.g. Paper1.json), move it out of eval/ground_truth/ so evaluate.py does not score it. (skipped)")
                continue
            whole = html_text(html)
        print(f"\n== {name}  ({len(gt['elements'])} elements)")
        for el in gt["elements"]:
            total += 1
            parts = element_text(el)
            label = (el.get("text") or el.get("key") or el.get("latex") or " ".join(el.get("caption_keywords", [])) or el["type"])[:60]
            if not parts:
                manual += 1
                print(f"  LOOK    p{el.get('page', '?')} {el['type']:<9} {label}")
                continue
            page = el.get("page", 1)
            if pages and 1 <= page <= len(pages):
                ok_page = all(found(p, pages[page - 1]) for p in parts)
            else:
                ok_page = None
            ok_any = all(found(p, whole) for p in parts)
            if pages and ok_page:
                continue
            if not pages and ok_any:
                continue
            problems += 1
            missing = [p for p in parts if not found(p, whole)]
            if missing:
                print(f"      not found in the source: {missing[0][:90]!r}")
            if ok_any:
                print(f"  WRONG_PAGE p{page} {el['type']:<9} {label}")
            else:
                print(f"  NOT_FOUND  p{page} {el['type']:<9} {label}")
    print(f"\n{total} elements checked, {problems} problems, {manual} formulas/images to check by eye.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

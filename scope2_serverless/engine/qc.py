"""Task 3 - AI quality check. Re-uses ai.py (same provider, same BEDROCK_MODEL_ID / BEDROCK_REGION, same prices).

A second model call (same model, different task) looks at the PDF crop AND the extracted output and answers
pass/fail with a score. Only elements that the AI repaired (formulas, tables, algorithms, re-read paragraphs) are checked,
so the cost stays small.

Confidence after the check (the repair itself set 0.8 = "well-formed, not yet checked"):
  pass         -> the checker's own score, kept between 0.8 and 0.95  (not 1.0: writer and checker are the same
                  model and can share a blind spot; 1.0 is kept for results proven by independent means)
  fail         -> 0.5, flags qc_fail + needs_review, the checker's issues are stored on the element
  not run      -> stays 0.8 and keeps only the ai_repaired flag; the reason is stored in the element's "qc_error"
                  field and printed in the log (time budget used up, or the AI call failed 3 times)
"""
import io
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pdfplumber

try:
    from . import ai
except ImportError:
    import ai

PROMPT = (
    "You are a strict quality checker. The image is a crop of a PDF page. Below is the {kind} that an extraction "
    "engine produced from it ({fmt}). Compare them symbol by symbol / cell by cell.\n"
    "Fail if anything is wrong, missing or invented (a number, symbol, sub/superscript, row, column, cell).\n"
    "Do NOT fail for spacing, line breaks, hyphenation or LaTeX notation that shows the same content (for example "
    "'log PAMPA' vs 'logPAMPA', \\text{...} vs plain letters): pass, give a score of about 0.8 and list it as an issue.\n"
    'Reply with ONLY this JSON: {"verdict": "pass" or "fail", "score": 0.0-1.0, "issues": ["short issue", ...]}\n\n'
    "EXTRACTED OUTPUT:\n{candidate}"
)


def _parse(text):
    """Model may add prose or LaTeX braces around the JSON, so find the verdict instead of trusting the first '{'."""
    for m in re.finditer(r"\{[^{}]*\}", text):
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and "verdict" in d:
                return d
        except json.JSONDecodeError:
            pass
    m = re.search(r'"?verdict"?\s*[:=]\s*"?(pass|fail)', text, re.I)
    return {"verdict": m.group(1).lower(), "issues": []} if m else {}


def qc_elements(pdf_path, els, stats, check_all_low=False, workers=4, budget=60.0):
    """Adds flags qc_pass / qc_fail / needs_review. Costs go into `stats` (kind 'qc'). Returns a small dict."""
    out = {"qc_checked": 0, "qc_pass": 0, "qc_fail": 0, "qc_errors": 0}
    provider = ai.get_provider()
    if provider is None:
        return out
    todo = [e for e in els if e["type"] in ("formula", "table", "text", "algorithm") and e.get("bbox") and
            ("ai_repaired" in e["flags"] or (check_all_low and e["confidence"] < 0.85))]
    pngs = []
    with pdfplumber.open(pdf_path) as pdf:                      # render one by one (pdfium is not thread-safe)
        for e in todo:
            res = 200 if e["type"] in ("text", "algorithm") else 170          # paragraphs: small sub/superscripts
            img = pdf.pages[e["page"] - 1].crop(tuple(e["bbox"])).to_image(resolution=res).original
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            pngs.append(buf.getvalue())
    deadline = time.time() + budget

    def work(args):
        e, png = args
        if time.time() > deadline:
            return e, None, "time budget used up before the check could start"
        kind, fmt = {"formula": ("formula", "LaTeX"), "table": ("table", "Markdown table"),
                     "text": ("paragraph", "Markdown text with inline LaTeX between $ signs" if "inline_math" in e["flags"]
                              else "plain text"),
                     "algorithm": ("algorithm (pseudocode)", "Markdown lines with inline LaTeX between $ signs")}[e["type"]]
        cand = e["content_markdown"] if e["type"] in ("text", "algorithm") else e["content_markdown"].strip("$ \n")
        prompt = PROMPT.replace("{kind}", kind).replace("{fmt}", fmt).replace("{candidate}", cand)
        err = None
        for attempt in range(3):                                 # same retry policy as the repair calls
            if attempt and time.time() + 2 ** attempt > deadline:
                break
            if attempt:
                time.sleep(2 ** attempt)                         # 2 s, then 4 s (rate limits / throttling)
            try:
                text, tin, tout = provider.ask(png, prompt)
                stats.add("qc", tin, tout, cached=False)
                return e, _parse(text), None
            except Exception as ex:                              # QC must never break the job
                err = str(ex)[:200]
        return e, None, f"AI check failed: {err}" if err else "time budget used up during retries"

    with ThreadPoolExecutor(workers) as pool:
        for e, verdict, why in pool.map(work, zip(todo, pngs)):
            if verdict is None:                                  # not checked: score stays 0.8, no extra flag
                out["qc_errors"] += 1
                e["qc_error"] = why
                out.setdefault("qc_not_run_reasons", [])
                if why not in out["qc_not_run_reasons"]:
                    out["qc_not_run_reasons"].append(why)
                print(f"QC not run for {e['type']} on page {e['page']}: {why}")
            elif verdict.get("verdict") == "pass":
                out["qc_checked"] += 1
                out["qc_pass"] += 1
                e["flags"].append("qc_pass")
                try:                                             # the checker's own score, if it gave a sane one
                    score = float(verdict.get("score"))
                except (TypeError, ValueError):
                    score = 0.95
                score = score if 0.0 <= score <= 1.0 else 0.95
                e["confidence"] = round(min(0.95, max(e["confidence"], score)), 2)
                e["qc_score"] = score
            else:                                                # fail or unparseable -> be conservative
                out["qc_checked"] += 1
                out["qc_fail"] += 1
                e["flags"] += ["qc_fail", "needs_review"]
                e["confidence"] = min(e["confidence"], 0.5)
                e["qc_issues"] = verdict.get("issues", ["unparseable QC answer"])
                if verdict.get("score") is not None:
                    e["qc_score"] = verdict.get("score")
    return out

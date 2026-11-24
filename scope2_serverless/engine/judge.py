"""AI judge (accurate level): scores EVERY extracted element against the page image, one AI call per page.

The judge sees the rendered page and the numbered list of elements we extracted from it, and returns for each
element a score (rubric below) and the problem it found. It also reports content visible on the page that is
missing from the list. Its score is one of the signals of the final confidence (ensemble.final_confidence),
never the only one: the judge is the same model family as the repair step and can share its blind spots.
"""
import io
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import pdfplumber

PROMPT = """You are a strict judge of a document-extraction system. The image is one page of a document.
Below is the list of elements the system extracted from THIS page (Markdown; formulas in LaTeX; tables as Markdown).
Score each element against what is printed on the page:
  1.0 = exactly right; 0.8 = only spacing/punctuation/hyphenation differences; 0.5 = a real error (a wrong or
  missing word, number, symbol, sub/superscript or table cell); 0.2 = mostly wrong or garbled; 0.0 = not on the page.
Images: score 1.0 if the page shows a figure there.
An element ending with "[...shortened for this check...]" was cut only for this check: judge the part shown and do
not report it as truncated.
Reply with ONLY this JSON:
{"elements": [{"id": <id>, "score": <0..1>, "issue": "<short, empty if none>"}], "missing": "<content on the page that is missing from the list, empty if none>"}

ELEMENTS:
"""
CHUNK = 25
CLIP = 4000          # characters of one element sent to the judge; longer ones are marked as shortened


def _clip(text):
    return text if len(text) <= CLIP else text[:CLIP] + " [...shortened for this check; judge only the part shown]"


def _png(pdf, n, res=130):
    buf = io.BytesIO()
    pdf.pages[n - 1].to_image(resolution=res).original.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _parse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        return None


def judge_pages(pdf_path, els, provider, stats, budget=120.0):
    deadline = time.time() + budget
    workers = int(os.environ.get("AI_PARALLEL", "4"))
    for i, e in enumerate(els):
        e["id"] = i
    jobs = []
    with pdfplumber.open(pdf_path) as pdf:
        for p in sorted({e["page"] for e in els}):
            on_page = [e for e in els if e["page"] == p]
            png = _png(pdf, p)
            for k in range(0, len(on_page), CHUNK):
                jobs.append((p, png, on_page[k:k + CHUNK]))

    def work(job):
        p, png, part = job
        listing = "\n".join(json.dumps({"id": e["id"], "type": e["type"],
                                        "content": _clip(e["content_markdown"])},
                                       ensure_ascii=False) for e in part)
        err = None
        for attempt in range(3):
            if time.time() > deadline:
                return p, part, None, err or "time budget used up"
            if attempt:
                time.sleep(2 ** attempt)
            try:
                text, tin, tout = provider.ask(png, PROMPT + listing)
                stats.add("judge", tin, tout, cached=False)
                with stats._lock:                                # count judge calls in the job's AI totals
                    stats.calls += 1
                    stats.input_tokens += tin
                    stats.output_tokens += tout
                return p, part, _parse(text), None
            except Exception as ex:
                err = str(ex)[:200]
        return p, part, None, f"AI judge failed: {err}"

    summary = {"judge_pages": 0, "judge_elements": 0, "judge_errors": [], "judge_missing": []}
    by_id = {e["id"]: e for e in els}
    with ThreadPoolExecutor(workers) as ex:
        for p, part, verdict, why in ex.map(work, jobs):
            if verdict is None:
                summary["judge_errors"].append({"page": p, "why": why})
                continue
            summary["judge_pages"] += 1
            for item in verdict.get("elements", []):
                e = by_id.get(item.get("id"))
                try:
                    score = max(0.0, min(1.0, float(item.get("score"))))
                except (TypeError, ValueError):
                    continue
                if e is not None and e["page"] == p:
                    e.setdefault("evidence", {})["judge"] = {"score": score, "issue": str(item.get("issue", ""))[:300]}
                    summary["judge_elements"] += 1
            if verdict.get("missing"):
                summary["judge_missing"].append({"page": p, "missing": str(verdict["missing"])[:300]})
    scores = [e["evidence"]["judge"]["score"] for e in els if e.get("evidence", {}).get("judge")]
    summary["judge_mean_score"] = round(sum(scores) / len(scores), 3) if scores else None
    return summary

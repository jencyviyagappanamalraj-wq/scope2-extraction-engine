"""The full extraction pipeline (v4, ensemble). Used by the worker Lambda and for local runs:

    python engine/pipeline.py data/test_docs/Paper1.pdf out/Paper1.md --level accurate

Order of work
 1. rules engine (extract.py) reads the PDF / web page          } in parallel
 2. witness tools read it too (witnesses.py)                     }
    standard: pypdf, MarkItDown, Docling      accurate: + Azure Document Intelligence, Amazon Textract
 3. pages with no text layer (scans) are filled from the best OCR witness
 4. tables: the version most tools agree with is kept (engine, Docling, Azure or Textract)
 5. every element gets tool_support / agreement; paragraphs the tools disagree on are flagged low_agreement
 6. accurate: Bedrock repairs formulas, weak tables, algorithms and low-agreement paragraphs (ai.py),
    a second call checks each repair (qc.py), and the judge scores every element on the page image (judge.py)
 7. structure checks, then the final confidence = weighted evidence (ensemble.final_confidence)
 8. report: per-tool leaderboard, per-page coverage and possibly-missing lines, judge summary, cost
No ground truth is used anywhere in this process.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import ensemble                      # noqa: E402
import extract                       # noqa: E402
import witnesses as W                # noqa: E402

UA = {"User-Agent": "scope2-extractor/0.4"}


def _fetch_html(url):
    """Fetch a web page; every redirect hop is checked, so a public URL cannot bounce us to an internal address."""
    import requests
    import common
    for _ in range(5):
        if os.environ.get("ALLOW_LOCAL_URLS") != "1":
            common.check_url(url)
        r = requests.get(url, timeout=30, headers=UA, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            url = requests.compat.urljoin(url, r.headers.get("location", ""))
            continue
        r.raise_for_status()
        return r.text
    raise ValueError("too many redirects")


def run(src, out_md, level="standard", budget=None):
    t0 = time.time()
    budget = budget or float(os.environ.get("JOB_BUDGET_SECONDS", "780"))
    is_web = src.startswith("http")
    info, html = {}, None
    if is_web:
        html = _fetch_html(src)
    with ThreadPoolExecutor(1) as pool:                                  # witnesses run while the rules work
        wit_future = pool.submit(W.run_witnesses, src, level, html)
        if is_web:
            els = extract.extract_web(html)
        else:
            els = extract.extract_pdf(src, deadline=t0 + budget * 0.4, info=info)
        flagged_text = 0 if is_web else extract.crosscheck_text(src, els)
        t_rules = time.time() - t0
        wit = wit_future.result()
    t_witness = time.time() - t0

    filled = [] if is_web else ensemble.fill_scanned_pages(els, wit, extract.table_to_md, extract.new_el)
    tables_replaced = ensemble.choose_tables(els, wit, extract.table_to_md)
    ensemble.score_agreement(els, wit)

    stats_d, qc_d, judge_d = {}, {}, {}
    if level == "accurate" and not is_web:
        import ai
        provider = ai.get_provider()
        if provider is None:
            print("AI_PROVIDER not set -> no AI repair / judge")
        else:
            stats = ai.Stats()
            vocab = {}
            for w in ensemble.ok_witnesses(wit):
                for p, pg in w["pages"].items():
                    vocab[p] = vocab.get(p, "") + "\n" + pg.get("text", "")
            for e in els:
                if "low_agreement" in e["flags"]:
                    e["_vocab"] = vocab.get(e["page"], "")
            left = budget - (time.time() - t0)
            extract.ai_repair(src, els, provider, stats, budget=max(10.0, left * 0.45))
            from qc import qc_elements
            qc_d = qc_elements(src, els, stats, budget=max(30.0, (budget - (time.time() - t0)) * 0.4))
            from judge import judge_pages
            judge_d = judge_pages(src, els, provider, stats, budget=max(30.0, budget - (time.time() - t0) - 20))
            stats_d = stats.as_dict()
            for e in els:
                e.pop("_vocab", None)

    ensemble.score_agreement(els, wit, only_repaired=True)        # repaired text is compared again
    ensemble.run_checks(els, wit, extract.balanced)
    ensemble.final_confidence(els)
    cov = ensemble.coverage(els, wit)
    leaderboard = ensemble.tool_scores(els, wit)
    for i, e in enumerate(els):
        e["id"] = i

    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    # pictures: each PDF figure is saved as images/<name>.png next to the Markdown and linked from it
    images = [] if is_web else extract.save_figures(src, els, str(Path(out_md).parent / "images"))
    total = time.time() - t0
    Path(out_md).write_text(extract.to_markdown(els), encoding="utf-8")
    covs = [c["coverage"] for c in cov if c["coverage"] is not None]
    report = {
        "source": src, "level": level, "engine_version": "v4-ensemble", "elements": len(els),
        "images": images,
        "inline_math_paragraphs": sum(1 for e in els if "inline_math" in e["flags"]),
        "witnesses": [{"tool": w["tool"], "status": w["status"], "seconds": w["seconds"],
                       "pages": len(w["pages"]), "error": w.get("error", "")} for w in wit],
        "tool_scores": leaderboard,
        "coverage_mean": round(sum(covs) / len(covs), 3) if covs else None,
        "coverage_by_page": cov,
        "scanned_pages_filled": filled, "tables_replaced_by_consensus": tables_replaced,
        "low_agreement_elements": sum(1 for e in els if "low_agreement" in e["flags"]),
        "needs_review": sum(1 for e in els if "needs_review" in e["flags"]),
        "needs_ai_left": sum(1 for e in els if "needs_ai" in e["flags"] and "ai_repaired" not in e["flags"]),
        "text_disagree": flagged_text,
        "seconds_rules": round(t_rules, 2), "seconds_witnesses": round(t_witness, 2), "seconds_total": round(total, 2),
        "budget_seconds": budget, "scoring_weights": ensemble.WEIGHTS,
        **info, **stats_d, **qc_d, **judge_d,
    }
    Path(out_md).with_suffix(".json").write_text(
        json.dumps({"report": report, "elements": els}, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("elements", "coverage_mean", "low_agreement_elements", "needs_review",
                                             "seconds_total")}))
    return els, report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out_md")
    ap.add_argument("--level", choices=["standard", "accurate"], default="standard")
    a = ap.parse_args()
    run(a.src, a.out_md, a.level)

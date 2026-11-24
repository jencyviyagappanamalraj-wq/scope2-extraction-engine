"""Task 6 - measure the numbers for the business case.

    python engine\\business_case.py data\\test_docs                      (a folder of PDFs, and/or URLs)
    python engine\\business_case.py data\\test_docs --all-ai-pages 2     (also measures the all-AI approach on the first 2 pages of each PDF)
    python engine\\business_case.py data\\test_docs --all-ai-pages 2 --server api_benchmark.csv
        (use the times and costs measured on the DEPLOYED server by scripts/benchmark_api.py instead of this laptop)

For every document it runs Standard and Accurate, times them, and records AI cost. With --all-ai-pages it sends whole
page images to the same Bedrock model and asks for Markdown (the "all-AI" baseline) and records tokens, time and cost.
Writes business_case.csv and business_case.md (cost per 1,000 pages, typical and worst-case time, competitor prices).
Needs the same environment variables as the engine: AI_PROVIDER=gemini, GEMINI_API_KEY, GEMINI_MODEL,
AI_PRICE_IN_PER_1K, AI_PRICE_OUT_PER_1K (see settings.env).

Costs are split in two:
  variable  = compute seconds x price per job-second + AI tokens x price   (grows with every page)
  fixed     = the always-on server: EC2 instance + Elastic IP + disk        (the same every month)
so the fully loaded cost per 1,000 pages depends on monthly volume. The report shows it at several volumes.
"""
import argparse
import csv
import io
import os
import shutil
import statistics
import sys
import time
from pathlib import Path

import pdfplumber

import extract  # noqa: F401
import pipeline

try:
    import ai
except ImportError:
    ai = None

# --- deployment prices (us-east-1 on-demand, checked Nov 2026; check the AWS pricing pages again before you submit) ---
# Serverless: the worker Lambda (3008 MB) costs $0.0000166667 per GB-second = $0.000049 per second of work;
# nothing runs (and nothing is paid) when there are no jobs.
WORKERS = int(os.environ.get("WORKERS", "3"))                 # SQS -> Lambda MaximumConcurrency (jobs at once)
COMPUTE_USD_PER_SECOND = float(os.environ.get("COMPUTE_USD_PER_SECOND", "0.000049"))
HOURS_PER_MONTH = 730
FIXED_PER_MONTH = {"Lambda, API Gateway, SQS, DynamoDB on-demand (paid per request; $0 when idle)": 0.0,
                   "S3 storage (files kept 7 days, about 1 GB)": 0.023}
VOLUMES = [10_000, 100_000, 1_000_000]          # pages per month for the fully loaded cost table
ALL_AI_PROMPT = ("Transcribe this page to GitHub-flavoured Markdown, in reading order. Headings as #, tables as Markdown "
                 "tables, formulas as LaTeX between $$ and $$. Output ONLY the Markdown.")
# list prices per 1,000 pages (USD) - check the vendor pages again before you submit
COMPETITORS = [("Amazon Textract - text only (DetectDocumentText)", 1.50), ("Amazon Textract - tables", 15.0),
               ("Amazon Textract - forms", 50.0), ("Amazon Textract - forms + tables (sum of the two)", 65.0),
               ("Azure Document Intelligence - Read", 1.50), ("Azure Document Intelligence - Layout / prebuilt", 10.0)]


def collect(paths):
    out = []
    for p in paths:
        if p.startswith("http"):
            out.append(p)
        elif Path(p).is_dir():
            out += [str(f) for f in sorted(Path(p).glob("*.pdf"))]
        else:
            out.append(p)
    return out


def timed_run(src, level, out_dir):
    name = (Path(src).stem if not src.startswith("http") else "web_" + str(abs(hash(src)) % 10**6))
    t = time.time()
    _, report = pipeline.run(src, str(out_dir / f"{name}_{level}.md"), level)
    # cloud witness tools are paid per page (list prices): add them to the AI cost of the job
    pages = int(report.get("pages", 0) or 0) or len(report.get("coverage_by_page", [])) or 1
    tools = sum(pages * float(os.environ.get(f"{w['tool'].upper()}_USD_PER_PAGE", {"azure": "0.01", "textract": "0.019"}[w["tool"]]))
                for w in report.get("witnesses", []) if w["status"] == "ok" and w["tool"] in ("azure", "textract"))
    report["ai_cost_usd"] = float(report.get("ai_cost_usd", 0) or 0) + tools
    return time.time() - t, report


def all_ai(pdf, n_pages):
    provider = ai.get_provider() if ai else None
    if provider is None or pdf.startswith("http"):
        return None
    tin = tout = 0
    t = time.time()
    with pdfplumber.open(pdf) as doc:
        pages = doc.pages[:n_pages]
        for page in pages:
            buf = io.BytesIO()
            page.to_image(resolution=150).original.convert("RGB").save(buf, format="PNG")
            _, i, o = provider.ask(buf.getvalue(), ALL_AI_PROMPT)[:3]
            tin, tout = tin + i, tout + o
    return {"pages": len(pages), "in": tin, "out": tout, "sec": time.time() - t, "usd": ai.Stats.price(tin, tout)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--all-ai-pages", type=int, default=0)
    ap.add_argument("--server", help="api_benchmark.csv from scripts/benchmark_api.py (times measured on the deployed server)")
    ap.add_argument("--margin", type=float, default=0.6, help="gross margin used for the suggested price (0.6 = 60%%)")
    a = ap.parse_args()
    shutil.rmtree(os.environ.get("AI_CACHE_DIR", ".cache/ai"), ignore_errors=True)    # cached answers would fake the timings
    out_dir = Path("business_case_out")
    out_dir.mkdir(exist_ok=True)
    rows = []
    for src in collect(a.paths):
        print("==", src)
        s_sec, s_rep = timed_run(src, "standard", out_dir)
        a_sec, a_rep = timed_run(src, "accurate", out_dir)
        pages = int(a_rep.get("pages_processed") or s_rep.get("pages_processed") or 1)
        full = all_ai(src, a.all_ai_pages) if a.all_ai_pages else None
        rows.append({"doc": Path(src).name if not src.startswith("http") else src, "pages": pages,
                     "standard_sec": round(s_sec, 2), "accurate_sec": round(a_sec, 2),
                     "accurate_ai_usd": float(a_rep.get("ai_cost_usd", 0)),
                     "allai_pages": full["pages"] if full else 0, "allai_in": full["in"] if full else 0,
                     "allai_out": full["out"] if full else 0, "allai_sec": round(full["sec"], 2) if full else 0,
                     "allai_usd": full["usd"] if full else 0})
    with open("business_case.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    tot_pages = sum(r["pages"] for r in rows)
    c = COMPUTE_USD_PER_SECOND
    where = "this machine"
    var = {"standard": sum(r["standard_sec"] * c for r in rows) / tot_pages * 1000,
           "accurate": sum(r["accurate_sec"] * c + r["accurate_ai_usd"] for r in rows) / tot_pages * 1000}
    sec_per_page = {"standard": [r["standard_sec"] / r["pages"] for r in rows],
                    "accurate": [r["accurate_sec"] / r["pages"] for r in rows]}
    worst = {lvl: max(rows, key=lambda r: r[f"{lvl}_sec"]) for lvl in ("standard", "accurate")}
    worst_txt = {lvl: f"{worst[lvl][f'{lvl}_sec']:.1f} ({worst[lvl]['doc']}, {worst[lvl]['pages']} pages)" for lvl in worst}
    if a.server:                                   # numbers measured on the deployed server
        where = "the deployed server (scripts/benchmark_api.py)"
        srv = [r for r in csv.DictReader(open(a.server, encoding="utf-8")) if r["status"] == "done"]
        for lvl in ("standard", "accurate"):
            rs = [r for r in srv if r["quality"] == lvl]
            if not rs:
                continue
            pages = sum(int(r["pages"]) for r in rs)
            var[lvl] = sum(float(r["cost_usd"]) for r in rs) / pages * 1000
            sec_per_page[lvl] = [float(r["wall_sec"]) / int(r["pages"]) for r in rs]
            w = max(rs, key=lambda r: float(r["wall_sec"]))
            worst_txt[lvl] = f"{float(w['wall_sec']):.1f} ({w['doc']}, {w['pages']} pages)"
    fixed = sum(FIXED_PER_MONTH.values())
    # capacity: how many pages one server can do per month if the workers are always busy (Standard level)
    capacity = WORKERS * HOURS_PER_MONTH * 3600 / max(statistics.median(sec_per_page["standard"]), 1e-6)

    md = ["# Business case (measured)\n",
          f"Test set: {len(rows)} documents, {tot_pages} pages. Times measured on {where}. "
          f"Deployment: serverless (Lambda workers, up to {WORKERS} jobs at once); compute ${c:.8f} per job-second; "
          f"cloud tools Azure $0.01 and Textract $0.019 per page (accurate level). "
          f"AI: {os.environ.get('BEDROCK_MODEL_ID') if os.environ.get('AI_PROVIDER') == 'bedrock' else os.environ.get('GEMINI_MODEL', os.environ.get('AI_PROVIDER', 'none'))} at ${os.environ.get('AI_PRICE_IN_PER_1K', '?')} / "
          f"${os.environ.get('AI_PRICE_OUT_PER_1K', '?')} per 1K input/output tokens.\n",
          "## 1. Variable cost per 1,000 pages (what each extra page costs us)\n",
          "| Approach | USD per 1,000 pages | How |", "|---|---|---|",
          f"| Our engine - Standard | {var['standard']:.4f} | measured seconds x compute price, no AI |",
          f"| Our engine - Accurate | {var['accurate']:.3f} | measured seconds x compute price + measured AI cost |"]
    sampled = [r for r in rows if r["allai_pages"]]
    if sampled:
        n = sum(r["allai_pages"] for r in sampled)
        allai = (sum(r["allai_usd"] for r in sampled) + sum(r["allai_sec"] for r in sampled) * c) / n * 1000
        md.append(f"| All-AI (every page image to the same model) | {allai:.2f} | measured on {n} sampled pages: "
                  f"{sum(r['allai_in'] for r in sampled) / n:.0f} input + {sum(r['allai_out'] for r in sampled) / n:.0f} output tokens per page, "
                  f"{sum(r['allai_sec'] for r in sampled) / n:.1f} s per page |")
    md += [f"| {name} | {price:.2f} | vendor list price |" for name, price in COMPETITORS]

    md += ["\n## 2. Fixed cost (nothing is always on)\n", "| Item | USD per month |", "|---|---|"]
    md += [f"| {k} | {v:.2f} |" for k, v in FIXED_PER_MONTH.items()]
    md += [f"| **Total** | **{fixed:.2f}** |",
           f"\nWith {WORKERS} jobs at once the service handles about {capacity:,.0f} Standard pages per month; raise the "
           "SQS MaximumConcurrency for more (Lambda scales out, there is no server to add).\n",
           "## 3. Fully loaded cost per 1,000 pages by monthly volume\n",
           "| Pages per month | Standard | Accurate | Suggested price Standard | Suggested price Accurate |", "|---|---|---|---|---|"]
    for v in VOLUMES:
        servers = 1
        f1k = fixed / v * 1000
        std, acc = var["standard"] + f1k, var["accurate"] + f1k
        md.append(f"| {v:,} | {std:.2f} | {acc:.2f} | "
                  f"{std / (1 - a.margin):.2f} | {acc / (1 - a.margin):.2f} |")
    md.append(f"\nSuggested price = fully loaded cost / (1 - {a.margin:.0%} gross margin). Compare it with the vendor prices in section 1.")

    md += ["\n## 4. Processing time\n", "| Level | typical (median sec/page) | worst document (total sec) | worst sec/page |", "|---|---|---|---|"]
    for lvl in ("standard", "accurate"):
        md.append(f"| {lvl} | {statistics.median(sec_per_page[lvl]):.2f} | {worst_txt[lvl]} | {max(sec_per_page[lvl]):.2f} |")
    md.append("\nWorst case is bounded by JOB_BUDGET_SECONDS: after it the job returns what it finished and is marked `truncated`.")
    md.append("Times from this machine exclude queue wait; times from the server (--server) include upload, queue and polling.")

    md += ["\n## 5. When a customer should use something else\n",
           "- **Scanned documents, photos of paper, handwriting:** the engine reads the PDF text layer and has no OCR. "
           "Use Textract DetectDocumentText or Azure Read ($1.50 per 1,000 pages).",
           "- **Fixed business forms (invoices, receipts, IDs, tax forms) with named fields:** Azure prebuilt models or "
           "Textract AnalyzeExpense/AnalyzeID return those fields directly; our key-value rules target papers and web pages.",
           "- **Very low, irregular volume:** the fixed server cost dominates (see section 3); a pay-per-page service is cheaper "
           "below a few thousand pages per month.",
           "- **Data that may not leave the customer's own cloud account or region:** run the open-source engine in their "
           "account (Standard needs no external AI) instead of our hosted API.",
           "- **Strict real-time needs (well under a second per page):** jobs are asynchronous; Accurate adds AI latency."]
    Path("business_case.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()

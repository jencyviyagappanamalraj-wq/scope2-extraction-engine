"""Task 6 - measure the DEPLOYED service: time and cost per page at each quality level, as a customer sees them.

    python scripts/benchmark_api.py https://<your-domain> <api key> data/test_docs --repeat 2
    python scripts/benchmark_api.py https://<your-domain> <api key> data/test_docs https://en.wikipedia.org/wiki/India

Submits every document at Standard and Accurate, waits for each, and records:
  wall_sec   submit -> result ready (what the customer waits, includes queue wait)
  job_sec    the engine time on the server
  pages, ai_calls, ai_cost_usd, compute_cost_usd, cost_usd, document_confidence, low_confidence_elements
Also submits all documents at once (--burst) to show several jobs running in parallel.
Writes api_benchmark.csv and api_benchmark.md. business_case.py reads api_benchmark.csv.
"""
import argparse
import csv
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests


def submit(api, key, src, quality):
    h = {"x-api-key": key, "content-type": "application/json"}
    if src.startswith("http"):
        r = requests.post(f"{api}/v1/jobs", headers=h, json={"url": src, "quality": quality}, timeout=60)
        r.raise_for_status()
        return r.json()["job_id"]
    j = requests.post(f"{api}/v1/jobs", headers=h, json={"filename": Path(src).name, "quality": quality}, timeout=60)
    j.raise_for_status()
    j = j.json()
    with open(src, "rb") as f:                                   # presigned S3 upload, then start
        requests.put(j["upload_url"], data=f, headers=j["upload_headers"], timeout=300).raise_for_status()
    requests.post(f"{api}/v1/jobs/{j['job_id']}/start", headers=h, timeout=60).raise_for_status()
    return j["job_id"]


def wait(api, key, job_id, timeout=1800):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = requests.get(f"{api}/v1/jobs/{job_id}", headers={"x-api-key": key}, timeout=60).json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(1)
    raise TimeoutError(job_id)


def one(api, key, src, quality):
    t0 = time.time()
    jid = submit(api, key, src, quality)
    j = wait(api, key, jid)
    r = j.get("report") or {}
    return {"doc": Path(src).name if not src.startswith("http") else src, "quality": quality, "status": j["status"],
            "pages": int(r.get("pages") or 1), "wall_sec": round(time.time() - t0, 2), "job_sec": j.get("seconds"),
            "ai_calls": r.get("ai_calls", 0), "ai_cost_usd": r.get("ai_cost_usd", 0),
            "compute_cost_usd": r.get("compute_cost_usd", 0), "cost_usd": r.get("cost_usd", 0),
            "document_confidence": r.get("document_confidence"), "low_confidence_elements": r.get("low_confidence_elements"),
            "truncated": bool(r.get("truncated")), "error": j.get("error", "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("api"); ap.add_argument("key"); ap.add_argument("sources", nargs="+")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--burst", action="store_true", help="also submit everything at once to measure parallel throughput")
    a = ap.parse_args()
    api = a.api.rstrip("/")
    srcs = []
    for s in a.sources:
        srcs += [str(p) for p in sorted(Path(s).glob("*.pdf"))] if not s.startswith("http") and Path(s).is_dir() else [s]
    rows = []
    for _ in range(a.repeat):                                # one at a time: clean per-job timings
        for src in srcs:
            for q in ("standard", "accurate"):
                row = one(api, a.key, src, q)
                print(row, flush=True)
                rows.append(row)
    burst = None
    if a.burst:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=3) as ex:      # = the worker MaximumConcurrency (3 jobs at once)
            res = list(ex.map(lambda s: one(api, a.key, s, "standard"), srcs * 2))
        burst = {"jobs": len(res), "pages": sum(r["pages"] for r in res), "wall_sec": round(time.time() - t0, 1),
                 "sum_job_sec": round(sum(r["job_sec"] or 0 for r in res), 1)}
        print("burst:", burst)
    with open("api_benchmark.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)

    md = ["# Deployed service benchmark\n", f"API: {api}. Documents: {len(srcs)}, runs per level: {a.repeat}.\n",
          "| Level | jobs | pages | typical sec/page (median) | worst job (wall sec) | worst sec/page | cost per 1,000 pages (variable) | mean confidence |",
          "|---|---|---|---|---|---|---|---|"]
    for q in ("standard", "accurate"):
        rs = [r for r in rows if r["quality"] == q and r["status"] == "done"]
        if not rs:
            continue
        pages = sum(r["pages"] for r in rs)
        per_page = [r["wall_sec"] / r["pages"] for r in rs]
        worst = max(rs, key=lambda r: r["wall_sec"])
        conf = statistics.mean(r["document_confidence"] or 0 for r in rs)
        md.append(f"| {q} | {len(rs)} | {pages} | {statistics.median(per_page):.2f} | {worst['wall_sec']:.1f} ({worst['doc']}, "
                  f"{worst['pages']} p.) | {max(per_page):.2f} | ${sum(r['cost_usd'] for r in rs) / pages * 1000:.3f} | {conf:.3f} |")
    failed = [r for r in rows if r["status"] != "done"]
    md.append(f"\nFailed jobs: {len(failed)}. Wall time includes upload, queue wait and polling (1 s).")
    if burst:
        md.append(f"\nParallel burst: {burst['jobs']} jobs / {burst['pages']} pages finished in {burst['wall_sec']} s "
                  f"(sum of job times {burst['sum_job_sec']} s, so about {burst['sum_job_sec'] / max(burst['wall_sec'], 0.1):.1f} jobs ran at once).")
    Path("api_benchmark.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    sys.exit(main())

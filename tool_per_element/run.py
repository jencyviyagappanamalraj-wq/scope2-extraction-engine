"""One tool per element type - extraction experiment.

    python run.py ..\\data\\test_docs\\Paper1.pdf
    python run.py ..\\data\\test_docs\\Paper1.pdf ..\\data\\test_docs\\Paper2.pdf --env path\\to\\.env
    python run.py ..\\data\\web_cache\\wikipedia_python.html
    python run.py https://en.wikipedia.org/wiki/Python_(programming_language) --key ..\\eval\\ground_truth\\wikipedia_python.json
    python run.py Paper1.pdf --skip azure,textract          (tools you cannot run yet: shown as "skipped")

Writes out/<name>/viewer.html (open it in a browser), elements.json, merged.md and summary.csv,
and out/index.html with one line per document.
"""
import argparse
import csv
import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import extractors as X        # noqa: E402
import ordering               # noqa: E402
import scoring                # noqa: E402
import viewer                 # noqa: E402


def load_env(path):
    """KEY=VALUE lines -> environment (values are never printed)."""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'"))


def find_key(src, explicit):
    if explicit:
        return Path(explicit)
    name = Path(src.split("?")[0]).name
    for d in (HERE.parent / "eval" / "ground_truth", Path.cwd() / "eval" / "ground_truth", HERE / "answer_keys"):
        for f in sorted(d.glob("*.json")) if d.exists() else []:
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("doc") == name:
                    return f
            except (OSError, ValueError):
                pass
    return None


def run_one(src, key_path, out_root, skip):
    is_web = src.startswith("http") or src.lower().endswith((".html", ".htm"))
    name = Path(src.split("?")[0]).stem if not src.startswith("http") else src.rstrip("/").split("/")[-1][:60]
    out_dir = out_root / "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== {src}")
    html_source = X.fetch_html(src) if is_web else None
    arg = html_source if is_web else src
    plan = X.WEB_PLAN if is_web else X.PDF_PLAN
    if not is_web and os.environ.get("MARKITDOWN_AS_IS") == "1":
        plan = [(t, "markitdown (as is)" if tool == "markitdown" else tool, fn) for t, tool, fn in plan]

    elements, timings = [], {}
    for typ, tool, fn in plan:
        if fn is None:
            timings[typ] = {"status": "not run", "error": f"{tool} reads PDFs and images, not web pages"}
            print(f"  {typ:<10} {tool:<14} not run (web page)")
            continue
        if tool in skip:
            timings[typ] = {"status": "skipped", "error": "--skip"}
            print(f"  {typ:<10} {tool:<14} skipped")
            continue
        t0 = time.perf_counter()
        try:
            els = fn(arg)
            sec = time.perf_counter() - t0
            timings[typ] = {"status": "ok", "seconds": round(sec, 3)}
            for e in els:
                e["tool_seconds"] = round(sec, 3)
            elements += els
            print(f"  {typ:<10} {tool:<14} {len(els):>4} elements  {sec:7.2f} s")
        except Exception as ex:                                           # one tool failing never stops the run
            sec = time.perf_counter() - t0
            msg = f"{type(ex).__name__}: {str(ex)[:300]}"
            timings[typ] = {"status": "error", "seconds": round(sec, 3), "error": msg}
            print(f"  {typ:<10} {tool:<14} ERROR {msg}")
            if os.environ.get("DEBUG"):
                traceback.print_exc()

    key_elements, key_name = [], None
    if key_path and key_path.exists():
        key_elements = json.loads(key_path.read_text(encoding="utf-8"))["elements"]
        key_name = key_path.name
    missing, key_best = scoring.score(elements, key_elements)
    for e in elements:
        if e.get("verdict") == "close" and e["type"] in ("heading", "key_value"):
            e["diff"] = scoring.diff_html(e["text"], e["key_text"])

    t0 = time.perf_counter()
    merged = ordering.merge_web(elements, html_source) if is_web else ordering.merge_pdf(elements, src)
    merge_sec = time.perf_counter() - t0
    order = ordering.order_score(merged, key_elements)
    summary = scoring.summary(elements, key_elements, key_best, timings, plan)

    (out_dir / "merged.md").write_text("\n\n".join(e.get("md_merged", e["md"]) for e in merged if not e.get("hidden")) + "\n",
                                       encoding="utf-8")
    (out_dir / "elements.json").write_text(json.dumps({"summary": summary, "order": order, "elements": merged},
                                                      indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    with open(out_dir / "summary.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)
    data = {"source_name": name, "kind": "web" if is_web else "pdf", "key_name": key_name,
            "n_pages": None if is_web else X.page_count(src), "summary": summary, "order": order,
            "merged": merged, "merge_seconds": merge_sec,
            "total_seconds": sum(t.get("seconds") or 0 for t in timings.values()),
            "missing": [{"type": k["type"], "page": k.get("page", 1), "key_text": scoring.key_text(k)} for k in missing]}
    viewer.build(data, out_dir / "viewer.html", pdf_path=None if is_web else src, html_source=html_source)
    print(f"  reading order: {order}")
    print(f"  -> {out_dir / 'viewer.html'}")
    return name, out_dir, summary, order


def write_index(out_root, results):
    rows = []
    for name, out_dir, summary, order in results:
        cells = "".join(f"<td>{r['tool']}<br>{r['extracted']} el · {('%.2f s' % r['seconds']) if r['seconds'] is not None else r['status']}"
                        f"<br>conf {r['mean_confidence'] if r['mean_confidence'] is not None else '–'} · key {r['key_found']}/{r['in_key']}</td>"
                        for r in summary)
        rows.append(f"<tr><td><a href='{out_dir.name}/viewer.html'>{name}</a><br>order {order['score']}</td>{cells}</tr>")
    head = "".join(f"<th>{r['type']}</th>" for r in results[0][2]) if results else ""
    (out_root / "index.html").write_text(
        "<!DOCTYPE html><meta charset='utf-8'><title>One tool per element</title><style>body{font:14px system-ui,Arial;margin:20px}"
        "td,th{border:1px solid #ddd;padding:6px;vertical-align:top;font-size:13px}table{border-collapse:collapse}</style>"
        f"<h2>One tool per element type</h2><table><tr><th>document</th>{head}</tr>{''.join(rows)}</table>", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--key", help="answer key JSON (default: eval/ground_truth/*.json whose \"doc\" is the file name)")
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--env", help="a .env file with AZURE_DI_ENDPOINT / AZURE_DI_KEY (and AWS settings)")
    ap.add_argument("--skip", default="", help="comma-separated tools to skip, e.g. azure,textract")
    ap.add_argument("--markitdown-as-is", action="store_true",
                    help="keep MarkItDown's form check (glues words / mixes columns on pages with figures)")
    a = ap.parse_args()
    if a.env:
        load_env(a.env)
    if a.markitdown_as_is:
        os.environ["MARKITDOWN_AS_IS"] = "1"
        a.out = str(Path(a.out).with_name(Path(a.out).name + "_markitdown_as_is"))
    skip = {s.strip() for s in a.skip.split(",") if s.strip()}
    out_root = Path(a.out)
    results = [run_one(src, find_key(src, a.key), out_root, skip) for src in a.sources]
    write_index(out_root, results)
    print(f"\nindex: {out_root / 'index.html'}")


if __name__ == "__main__":
    main()

"""Compare every tool on your documents WITHOUT ground truth (run on your laptop, from scope2_serverless):

    python scripts/compare_tools.py ../data/test_docs --level accurate
    python scripts/compare_tools.py ../data/test_docs https://en.wikipedia.org/wiki/Python_(programming_language) --level standard

For each document it runs the full pipeline and writes, into reports/:
  tool_comparison.md   per document: each tool's agreement with the majority of the others (F1, precision, recall),
                       coverage, elements to review, AI judge average, tables replaced by the tool vote
  tool_comparison.csv  the same numbers as a table
  <doc>.md / <doc>.json the extraction itself (open the JSON to see the evidence behind every score)
Needs for accurate: AWS login with Bedrock (AI_PROVIDER=bedrock, BEDROCK_MODEL_ID), AZURE_DI_ENDPOINT/AZURE_DI_KEY.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import pipeline  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--level", choices=["standard", "accurate"], default="accurate")
    ap.add_argument("--out", default="reports")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    docs = []
    for s in a.sources:
        docs += sorted(str(p) for p in Path(s).glob("*.pdf")) if not s.startswith("http") and Path(s).is_dir() else [s]
    rows, md = [], [f"# Tool comparison ({a.level} level, no ground truth)\n",
                    "Each tool is scored against what the MAJORITY OF THE OTHER tools read on the same page "
                    "(precision = how much of what it read the others confirm, recall = how much of what the others "
                    "agree on it also read). 'engine' is our final output after the vote, AI repair and AI judge.\n"]
    for d in docs:
        name = Path(d).stem if not d.startswith("http") else d.rstrip("/").split("/")[-1][:60]
        print(f"== {name}")
        try:
            els, r = pipeline.run(d, str(out / f"{name}.md"), a.level)
        except Exception as e:
            md.append(f"## {name}\n\nFAILED: {e}\n")
            continue
        md.append(f"## {name}\n")
        md.append("| tool | status | F1 vs others | precision | recall | seconds |\n|---|---|---|---|---|---|")
        status = {w["tool"]: w for w in r["witnesses"]}
        for tool in list(r["tool_scores"]) + [t for t in status if t not in r["tool_scores"]]:
            s = r["tool_scores"].get(tool, {})
            w = status.get(tool, {})
            label = "**our final output**" if tool == "engine" else tool
            st = "" if tool == "engine" else w.get("status", "") + (f" ({w.get('error', '')[:60]})" if w.get("error") else "")
            md.append(f"| {label} | {st} | {s.get('f1', '')} | {s.get('precision', '')} | {s.get('recall', '')} | {w.get('seconds', r['seconds_total'] if tool == 'engine' else '')} |")
            rows.append({"document": name, "tool": tool, "status": w.get("status", "ok"), **s})
        md.append(f"\nCoverage {r['coverage_mean']} · elements {r['elements']} · to review {r['needs_review']} · "
                  f"tools disagree {r['low_agreement_elements']} · tables replaced by vote {r['tables_replaced_by_consensus']} · "
                  f"AI judge average {r.get('judge_mean_score')} · AI cost ${r.get('ai_cost_usd', 0)}\n")
        miss = [f"p.{c['page']}: {m}" for c in r["coverage_by_page"] for m in c["possibly_missing"]]
        if miss:
            md.append("Possibly missing (seen by the tools, not in our output): " + "; ".join(miss[:5]) + "\n")
    (out / "tool_comparison.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    if rows:
        with open(out / "tool_comparison.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}, key=lambda k: ["document", "tool", "status"].index(k) if k in ("document", "tool", "status") else 9))
            w.writeheader()
            w.writerows(rows)
    print(f"\nwritten: {out / 'tool_comparison.md'}")


if __name__ == "__main__":
    main()

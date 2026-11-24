"""Show what a tool got wrong, so you know what to fix (run evaluate.py first).

    python eval/show_misses.py                      # engine_standard, every document
    python eval/show_misses.py engine_standard wikipedia_india
    python eval/show_misses.py docling              # see where a competitor fails

For each ground-truth element the tool missed (score below 1) it prints: document, element type, page, score, and
the ground-truth element. Group by type to see PATTERNS (for example "all unnumbered headings", "table cells").
"""
import csv
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main():
    tool = sys.argv[1] if len(sys.argv) > 1 else "engine_standard"
    doc = sys.argv[2] if len(sys.argv) > 2 else None
    rows = [r for r in csv.DictReader(open(HERE / "details.csv", encoding="utf-8"))
            if r["tool"] == tool and (doc is None or r["doc"] == doc)]
    if not rows:
        sys.exit("No rows. Run: python eval/evaluate.py   (tool names: " + ", ".join(
            sorted({r["tool"] for r in csv.DictReader(open(HERE / "details.csv", encoding="utf-8"))})) + ")")
    misses = [r for r in rows if float(r["score"]) < 1]
    print(f"{tool}: {len(misses)} of {len(rows)} elements not fully matched\n")
    print("by type:", dict(Counter(r["type"] for r in misses)), "\n")
    last = None
    for r in sorted(misses, key=lambda r: (r["doc"], r["type"], int(r["page"] or 0))):
        if (r["doc"], r["type"]) != last:
            print(f"--- {r['doc']} / {r['type']}")
            last = (r["doc"], r["type"])
        print(f"  p{r['page']:<2} score={float(r['score']):.2f}  {str(r['element'])[:100]}")


if __name__ == "__main__":
    main()

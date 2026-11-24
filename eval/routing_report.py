"""Evidence for the router: which method is best for each element type, and what it costs in time.

    python eval/evaluate.py            # first (writes eval/details.csv)
    python eval/routing_report.py      # prints the table and writes eval/routing_evidence.md

For every element type it shows each tool's accuracy (mean score on the ground truth), the best tool, and the
engine's margin over the best competitor. It also shows the mean seconds per document from run_baselines.py.
Use it in two ways:
  * to JUSTIFY the ROUTER in engine/extract.py (each choice backed by a number);
  * to DECIDE: if a competitor beats the engine on a type by 0.05 or more, that type is a candidate for integrating
    that tool (or improving the rules). If the engine already leads, say so and keep the faster, simpler choice.
"""
import csv
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TYPES = ["heading", "text", "table", "formula", "key_value", "image", "order"]


def main():
    f = HERE / "details.csv"
    if not f.exists():
        sys.exit("Run python eval/evaluate.py first.")
    acc = defaultdict(list)
    for r in csv.DictReader(open(f, encoding="utf-8")):
        acc[(r["tool"], r["type"])].append(float(r["score"]))
    tools = sorted({t for t, _ in acc})
    mean = {k: sum(v) / len(v) for k, v in acc.items()}

    secs = defaultdict(list)
    tf = HERE / "outputs" / "baseline_times.csv"
    if tf.exists():
        for r in csv.DictReader(open(tf, encoding="utf-8")):
            if r["status"] == "ok":
                secs[r["tool"]].append(float(r["seconds"]))
    sec = {t: sum(v) / len(v) for t, v in secs.items()}

    try:
        sys.path.insert(0, str(ROOT))
        from engine.extract import ROUTER
    except Exception:
        ROUTER = {}

    lines = ["| element | " + " | ".join(tools) + " | best tool | engine vs best competitor | router |",
             "|---|" + "---|" * (len(tools) + 3)]
    for ty in TYPES:
        scores = {t: mean.get((t, ty)) for t in tools}
        have = {t: s for t, s in scores.items() if s is not None}
        if not have:
            continue
        best = max(have, key=have.get)
        eng = max((s for t, s in have.items() if t.startswith("engine")), default=None)
        rival = {t: s for t, s in have.items() if not t.startswith("engine")}
        if eng is not None and rival:
            rb = max(rival, key=rival.get)
            gap = eng - rival[rb]
            verdict = (f"engine leads by {gap:+.2f} (vs {rb})" if gap >= 0
                       else f"{rb} leads by {-gap:.2f}  <- candidate to integrate or improve")
        else:
            verdict = "n/a"
        cells = [("-" if scores[t] is None else f"{scores[t]:.2f}") for t in tools]
        lines.append(f"| {ty} | " + " | ".join(cells) + f" | {best} | {verdict} | "
                     + ("+".join(ROUTER.get(ty, [])) or "-") + " |")
    lines.append("")
    lines.append("Mean seconds per document: " + ", ".join(f"{t} {s:.1f}s" for t, s in sorted(sec.items())) if sec
                 else "Mean seconds per document: run run_baselines.py to record times.")
    out = "\n".join(lines)
    print(out)
    (HERE / "routing_evidence.md").write_text(out + "\n", encoding="utf-8")
    print("\nWritten: eval/routing_evidence.md")


if __name__ == "__main__":
    main()

import csv, sys
from collections import defaultdict
from pathlib import Path

rows = list(csv.DictReader(open(Path(__file__).parent / "details.csv", encoding="utf-8")))
if len(sys.argv) > 2 and sys.argv[1] == "misses":
    tool = sys.argv[2]
    typ = sys.argv[3] if len(sys.argv) > 3 else None
    for r in rows:
        if r["tool"] == tool and float(r["score"]) < 1 and (typ is None or r["type"] == typ):
            print(f'{r["doc"]:<22}{r["type"]:<10}p{r["page"]:<3}{r["score"]:<6}{r["element"]}')
else:
    tools = sorted({r["tool"] for r in rows})
    acc = defaultdict(list)
    for r in rows:
        acc[(r["doc"], r["type"], r["tool"])].append(float(r["score"]))
    print(f'{"doc":<22}{"type":<11}' + "".join(f"{t:>16}" for t in tools))
    for doc, typ in sorted({(d, t) for d, t, _ in acc}):
        line = f"{doc:<22}{typ:<11}"
        for t in tools:
            v = acc.get((doc, typ, t))
            line += f"{(sum(v)/len(v) if v else float('nan')):>16.2f}"
        print(line)
# Deployed service benchmark

API: https://cdlslmrxb7.execute-api.us-east-1.amazonaws.com. Documents: 3, runs per level: 1.

| Level | jobs | pages | typical sec/page (median) | worst job (wall sec) | worst sec/page | cost per 1,000 pages (variable) | mean confidence |
|---|---|---|---|---|---|---|---|
| standard | 3 | 13 | 5.61 | 22.4 (Paper1.pdf, 4 p.) | 6.86 | $0.052 | 0.909 |
| accurate | 3 | 13 | 6.85 | 49.6 (Paper2.pdf, 8 p.) | 12.24 | $42.480 | 0.924 |

Failed jobs: 0. Wall time includes upload, queue wait and polling (1 s).

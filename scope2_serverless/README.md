# Scope 2 extraction service (v4: serverless + tool ensemble)

PDF or web page → Markdown + JSON, where every element has a score that is **explained by evidence**: agreement of
independent tools (pypdf, MarkItDown, Docling, Azure Document Intelligence, Amazon Textract), structure checks, an AI
judge (Bedrock Claude Sonnet 4.6) and the method used. No ground truth is needed to score a new document.

- **DEPLOY_STEPS.md**: step by step (CloudShell, one command up: `bash deploy.sh`, one down: `bash destroy.sh`)
- **DESIGN.md**: architecture diagram, how each tool is used, how the score works, figure images, inline math, highlighting, limitations
- `engine/`: `pipeline.py` (the full flow), `extract.py` (rules engine), `witnesses.py` (the 5 tools),
  `ensemble.py` (agreement, table vote, coverage, tool scores, final score), `judge.py` (AI judge),
  `ai.py` + `qc.py` (AI repair + check), `api_handler.py` / `worker_handler.py` (Lambdas), `index.html` (web app),
  `business_case.py`
- `template.yaml`: CloudFormation/SAM, no IAM resources; `Dockerfile.docling`: optional Docling worker
- `scripts/compare_tools.py` (tool comparison report), `benchmark_api.py` (live timing/cost), `test_client.py`

## Levels
| | standard | accurate |
|---|---|---|
| Tools | rules + pypdf + MarkItDown (+ Docling locally / container) | + Azure DI + Textract |
| AI | none | Bedrock repair (formulas, inline math, weak tables, algorithms, paragraphs the tools disagree on) + AI check + AI judge on every page |

## API
| Method and path | What it does |
|---|---|
| `POST /v1/keys` `{"email"}` | get an API key (shown once; only its SHA-256 is stored) |
| `POST /v1/jobs` `{"quality", "filename"}` | returns a presigned `upload_url`: PUT the PDF there, then `POST /v1/jobs/{id}/start` |
| `POST /v1/jobs` `{"quality", "url"}` | queue a web page or a PDF link |
| `GET /v1/jobs/{id}` | status, timings, pages, confidence, cost, tool comparison |
| `GET /v1/jobs/{id}/result?format=markdown\|json[&inline=1]` | the result (JSON includes the evidence for every element) |
| `GET /v1/jobs/{id}/result?format=zip` | `result.md` + `result.json` + `images/` (the figures the Markdown links to) |
| `GET /v1/jobs/{id}/images` | presigned links to each figure image (`images/page2_figure_1.png`, ...) |
| `GET /v1/usage` | jobs, pages, AI calls and cost for your key |
| `GET /` | the web app: PDF and output side by side, tool comparison, badges, evidence on click |

## Run locally (all tools, including Docling)
```
python engine/pipeline.py ../data/test_docs/Paper1.pdf out/Paper1.md --level accurate
python scripts/compare_tools.py ../data/test_docs --level accurate
```

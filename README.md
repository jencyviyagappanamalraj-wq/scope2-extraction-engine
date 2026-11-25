# Scope 2: Production AI Extraction Service on AWS

A serverless service that turns a **PDF or a web page into clean Markdown + JSON**. Every element (heading, paragraph,
table, formula, figure, key-value pair) comes with a **confidence score that is explained by evidence**, so a customer
knows which parts to trust without needing an answer key.

- Two quality levels: **standard** (rules + open-source witnesses, no AI, cheapest) and **accurate** (adds Amazon
  Textract, AI repair of the hard elements and an AI judge on every page).
- Runs on AWS: API Gateway, Lambda, SQS, DynamoDB, S3 and Bedrock (Claude Sonnet 4.6). No servers to manage.
- A web app shows the PDF and the result side by side; clicking an element highlights it on the page and shows why
  it got its score.

## Repository map

| Folder | What is in it |
|---|---|
| `scope2_serverless/` | **The product.** Engine, AWS template, deploy scripts, web app. Start here. |
| `scope2_serverless/engine/` | `pipeline.py` (full flow), `extract.py` (rules engine), `witnesses.py` (pypdf, MarkItDown, Textract, Azure DI, Docling), `ensemble.py` (agreement, table vote, final score), `ai.py` + `qc.py` (AI repair + AI check), `judge.py` (AI judge), `api_handler.py` / `worker_handler.py` (Lambdas), `index.html` (web app), `business_case.py` |
| `scope2_serverless/DESIGN.md` | Architecture, how each tool is used, how the score works, every extraction rule, limitations |
| `scope2_serverless/DEPLOY_STEPS.md` | Step-by-step deployment from AWS CloudShell |
| `scope2_serverless/BENCHMARK.md`, `BUSINESS_CASE.md` | Benchmark report and business case (measured on the deployed stack) |
| `scope2_serverless/scripts/` | `benchmark_api.py` (live timing and cost), `test_client.py`, `compare_tools.py` |
| `eval/` | `evaluate.py` (scorer), `ground_truth/` (human-checked answer keys: 2 papers, 2 Wikipedia pages), `results.csv`, `outputs/engine_accurate/` (live AI results) |

## Quickstart

### 1. Deploy (AWS CloudShell, about 5 minutes)
```bash
tar -xzf scope2-serverless.tgz
cd scope2_serverless
bash deploy.sh            # prints the API URL and the web app URL
```
Remove everything with `bash destroy.sh`. Details: `scope2_serverless/DEPLOY_STEPS.md`.

### 2. Get an API key (shown once; only its SHA-256 hash is stored)
```bash
curl -X POST "$API/v1/keys" -H "content-type: application/json" -d '{"email":"you@example.com"}'
```

### 3. Extract a document
```bash
# a PDF: ask for an upload link, upload, start
curl -X POST "$API/v1/jobs" -H "x-api-key: $KEY" -d '{"quality":"accurate","filename":"Paper1.pdf"}'
curl -X PUT "<upload_url>" --upload-file Paper1.pdf
curl -X POST "$API/v1/jobs/<job_id>/start" -H "x-api-key: $KEY"

# a web page or PDF link
curl -X POST "$API/v1/jobs" -H "x-api-key: $KEY" -d '{"quality":"standard","url":"https://en.wikipedia.org/wiki/India"}'

# status, then the result
curl "$API/v1/jobs/<job_id>" -H "x-api-key: $KEY"
curl "$API/v1/jobs/<job_id>/result?format=markdown" -H "x-api-key: $KEY"   # or json, or zip (md + json + images/)
```
Or open the web app at `$API/`, paste the key, upload a PDF and pick the level.

### 4. Run locally (no AWS needed for standard)
```bash
pip install -r scope2_serverless/requirements-worker.txt
python scope2_serverless/engine/pipeline.py data/test_docs/Paper1.pdf out/Paper1.md --level standard
```

## How it works

```
client -> API Gateway -> scope2-api (Lambda) -> SQS queue (+ dead-letter queue)
                                    |                     |
                              DynamoDB (jobs, keys)   scope2-worker (Lambda): rules engine + witnesses
                                                          + Textract + Bedrock (accurate) -> S3 (md, json, images)
```

| Element | Main producer | Checked by |
|---|---|---|
| Headings, hierarchy | pdfplumber rules (font size, numbering, bold; run-in headings kept in their paragraph) | witnesses, AI judge |
| Paragraphs | pdfplumber rules (baseline grouping, columns, reading order) | pypdf, MarkItDown, Textract agreement; AI re-read if they disagree |
| Inline math | rules flag it; accurate: AI rewrites with inline LaTeX | AI check + AI judge |
| Tables | rules (ruled tables, header row, grouped labels) | Textract table as a candidate in the vote; AI repair if weak |
| Formulas | rules find the block; accurate: AI writes LaTeX | AI check + AI judge |
| Figures | caption + drawing area, saved as PNG files linked from the Markdown | AI judge |
| Key-value pairs | rules (label + value); accurate: AI on page 1, only pairs printed as label then value | must be found in the page text |
| Web pages | BeautifulSoup (headings, text, tables) | - |

**Score per element** = 0.45 x AI judge + 0.35 x tool agreement + 0.20 x method (weights re-normalised when a signal
is missing, e.g. no judge at the standard level). A failed structure check caps the score at 0.79 (two: 0.5); below
0.6 the element is flagged **needs_review**. Full explanation in `DESIGN.md`.

## Test results (answer keys in `eval/ground_truth/`)

| Document | Headings | Text blocks | Key-values | Reading order |
|---|---|---|---|---|
| Paper1 (2 columns, formulas, algorithm) | 15 / 15 | 8 / 8 | 2 / 3 | 0.955 |
| Paper2 (1 column, charts, grouped table) | 25 / 25 | 6 / 6 | 1 / 1 | 0.967 |

## Benchmark and pricing

**Benchmark** (`scope2_serverless/BENCHMARK.md`, scored by `eval/evaluate.py` against the answer keys): the engine
beats every Scope 1 tool on headings (0.99), text (0.97), tables (0.93), key-values (1.00) and, at the accurate level,
formulas (1.00 vs 0.00–0.33), with three examples where AI fixed what the parsers broke and one where it made things
worse.

**Business case** (`scope2_serverless/BUSINESS_CASE.md`), measured on the deployed stack:

| Level | Cost to us / 1,000 pages | Price / 1,000 pages | Typical time per page | Worst job |
|---|---|---|---|---|
| standard | $0.05 | **$1.00** | 5.6 s | 22 s (4 pages) |
| accurate | $42.48 | **$100.00** | 6.9 s | 50 s (8 pages) |

Compared with Textract ($1.50 text, $15 tables, $65 forms + tables), Azure DI ($1.50 Read, $10 Layout) and an
all-AI approach (≈ $20, estimate), and when a customer should use one of those instead.

## API reference

| Method and path | What it does |
|---|---|
| `POST /v1/keys` `{"email"}` | get an API key (shown once; only its SHA-256 is stored) |
| `POST /v1/jobs` `{"quality", "filename"}` | returns a presigned `upload_url`: PUT the PDF there, then `POST /v1/jobs/{id}/start` |
| `POST /v1/jobs` `{"quality", "url"}` | queue a web page or a PDF link |
| `GET /v1/jobs/{id}` | status, timings, pages, confidence, cost, tool comparison |
| `GET /v1/jobs/{id}/result?format=markdown\|json\|zip` | the result (JSON includes the evidence for every element; zip adds `images/`) |
| `GET /v1/jobs/{id}/images` | presigned links to each figure image |
| `GET /v1/usage` | jobs, pages, AI calls and cost for your key |
| `GET /` | the web app (replaces the Scope 1 Streamlit app): PDF and output side by side, click to highlight, scores and evidence |

All `/v1` routes except `POST /v1/keys` need the header `x-api-key`.

## Known limitations
- Tested on 2 papers and 2 web pages with answer keys. The rules use layout only (positions, fonts, rules, captions),
  not words from these documents, but new layouts can still break them; low scores and **needs_review** flags
  show where.
- Markdown tables cannot merge cells: a label printed once for a group of rows is repeated on each row. Bold and
  underline inside tables are not kept.
- Scanned pages have no text layer: at the accurate level Textract reads them; at the standard level they come
  out empty (use accurate for scanned documents).
- Azure Document Intelligence and Docling are supported as witnesses but are off in the deployed stack (Azure
  credits expired; Docling needs a large container image).
- The AI writer and the AI checker are the same model, so their scores never go above 0.95.

## Security
- No secrets in the code or the repo: keys are typed at deploy time (CloudFormation `NoEcho`) or kept in a local
  `.env` that is git-ignored.
- API keys are stored only as SHA-256 hashes. S3 objects are encrypted (AES-256) and deleted after 7 days.
- No customer-managed KMS key is used.

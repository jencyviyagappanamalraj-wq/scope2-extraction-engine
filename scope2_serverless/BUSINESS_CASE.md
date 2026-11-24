# Business case and price per page

All our numbers are measured on the deployed AWS stack (`scripts/benchmark_api.py`, output in `api_benchmark.md`) and
priced with `engine/business_case.py` (60% gross margin). Test set: Paper1 (4 pages), Paper2 (8 pages) and the
Wikipedia "India" page; every job finished (0 failures).

## 1. Cost per 1,000 pages (what each extra page costs us)

| Approach | USD per 1,000 pages | Source |
|---|---|---|
| **Our engine, standard** (rules + pypdf + MarkItDown, no AI) | **0.05** | measured: Lambda seconds × price |
| **Our engine, accurate** (+ Textract witness, AI repair, AI check, AI judge) | **42.48** | measured: about $25 Bedrock + $18 Textract + compute |
| Amazon Textract, text only (DetectDocumentText) | 1.50 | AWS list price |
| Amazon Textract, tables | 15.00 | AWS list price |
| Amazon Textract, forms + tables | 65.00 | AWS list price |
| Azure Document Intelligence, Read | 1.50 | Azure list price |
| Azure Document Intelligence, Layout | 10.00 | Azure list price |
| All-AI (every page image to Claude Sonnet 4.6, Markdown out) | **≈ 20** (estimate) | ≈ 1,600 input tokens × $3/M + ≈ 1,000 output tokens × $15/M per page. Not measured live: `--all-ai-pages` needs Bedrock credentials on the benchmark machine |

Fixed cost: about **$0.02 per month** (S3 storage). Lambda, API Gateway, SQS and DynamoDB on demand cost nothing when
idle, so the cost per 1,000 pages is the same at 10,000 and at 1,000,000 pages a month.

## 2. Processing time (deployed service, including upload, queue and polling)

| Level | Typical (median s per page) | Worst document | Worst s per page |
|---|---|---|---|
| standard | 5.6 | 22.4 s (Paper1, 4 pages, includes a Lambda cold start) | 6.9 |
| accurate | 6.9 | 49.6 s (Paper2, 8 pages) | 12.2 |

Very long documents are bounded by the job budget (780 s): the job returns what it finished, marked `truncated`.
3 jobs run at once by default; raising the SQS `MaximumConcurrency` scales out (no server to add).

## 3. Price

| Level | Our cost / 1,000 pages | **Price / 1,000 pages** | Margin | Who it is for |
|---|---|---|---|---|
| standard | $0.05 | **$1.00** | 95% | digital PDFs and web pages where structure matters: headings, paragraphs, tables, figures, key-values, all with scores |
| accurate | $42.48 | **$100.00** ($0.10 per page) | 58% | technical and scientific documents (formulas, inline math, algorithms) where every element must be checked |

**Why these prices hold up:**
- **Standard at $1.00** is below the text-only services (Textract/Azure Read, $1.50), yet returns full structure in
  reading order, which those services do not: on our test set it beats every Scope 1 tool on headings (0.99), text
  (0.97), tables (0.93) and key-values (1.00) (`BENCHMARK.md`).
- **Accurate at $100** is the only option that gets formulas right (1.00 vs 0.00–0.33 for every tool) **and proves
  it**: every element carries an AI judge score, tool agreement and failed checks, and anything below 0.6 is flagged
  for review. Textract and Azure Layout are cheaper ($10–15) but return no formulas as LaTeX and no per-element
  evidence. All-AI is cheaper (≈ $20) but has no independent check: when the model drops or invents a number,
  nothing flags it.
- The cost is dominated by two items we control: the Textract witness (≈ $18) and the AI judge on every page. An
  **accurate-lite** option without the Textract witness would cost ≈ $25 and could sell at ≈ $60 (same AI repair
  and judge, one fewer independent reader).

## 4. When a customer should use something else

- **Scanned documents, photos, handwriting at high volume:** standard has no OCR (accurate fills scanned pages from
  Textract, at accurate prices). Textract DetectDocumentText or Azure Read at $1.50 is cheaper for plain OCR.
- **Fixed business forms (invoices, receipts, IDs, tax forms):** Azure prebuilt models or Textract
  AnalyzeExpense/AnalyzeID return named fields directly; our key-value rules target papers and web pages.
- **Plain text only, no structure needed:** a text-only service or a plain parser is enough.
- **Data that may not leave the customer's own account:** run the open-source engine in their account (standard
  needs no external AI).
- **Real-time needs (well under a second per page):** jobs are asynchronous, and accurate adds AI latency.

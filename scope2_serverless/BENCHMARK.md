# Benchmark report: baseline tools vs the Scope 2 engine

## How it was measured
- **Test set:** the Scope 1 documents. Paper1 (HVAC, 4 pages, two columns, formulas, algorithm), Paper2 (PROTAC,
  8 pages, one column, charts, grouped table), Wikipedia "India" and "Python" (saved HTML, so every tool reads the
  same page).
- **Answer keys:** `eval/ground_truth/*.json`, read by hand: 113 headings, 74 text sentences, 4 tables, 3 formulas,
  45 key-value pairs, 91 images.
- **Scorer:** `eval/evaluate.py` (standard library only). An element counts as found when the tool's Markdown has it
  at ≥ 0.85 similarity (formulas ≥ 0.8 against the LaTeX and its accepted alternatives). *Order* = share of
  consecutive answer-key headings/sentences that appear in the same order in the output.
  A run-in heading written as bold at the start of a paragraph (`**Original dataset** Current ...`) counts as a
  heading for every tool.
- **Run:** `python eval/evaluate.py` after writing each tool's output to `eval/outputs/<tool>/<doc>.md`.

## Scores per element type (1.00 = everything in the answer key found)

| Element | Scope 1 open source (pdfplumber + BeautifulSoup scripts) | MarkItDown | Docling | Azure DI (Scope 1, pages 1–2 only) | **Engine standard** (no AI) | **Engine accurate** (AI, live AWS run) |
|---|---|---|---|---|---|---|
| Headings (113) | 0.82 | 0.65 | 0.91 | 0.27 | **0.99** | **0.99** |
| Text (74) | 0.85 | 0.26 | 0.41 | 0.18 | **0.97** | **0.97** |
| Tables (4) | 0.69 | 0.43 | 0.44 | 0.00 | **0.93** | **0.93** |
| Formulas (3) | 0.00 | 0.00 | 0.00 | 0.33 | 0.33 | **1.00** |
| Key-value pairs (45) | 0.98 | 0.47 | 0.47 | 0.58 | **1.00** | **1.00** |
| Images (91) | 0.98 | 0.91 | **1.00** | 0.09 | **1.00** | **1.00** |
| Reading order | 0.88 | 0.95 | **0.97** | 0.91 | 0.89¹ | 0.89¹ |

Engine accurate = the Markdown downloaded from the deployed service (Bedrock Claude Sonnet 4.6) for the two papers.
Web pages use no AI at either level (the HTML already marks headings, paragraphs and tables), so their accurate
output is the standard output.

Reading order: **PDFs: 1.00 on both papers** (Docling 0.95 / 1.00). The drop is on the web pages (India 0.86, Python 0.82): short
headings such as "Economy" or "Typing" also appear as words in the infobox and tables that the engine keeps, and the
scorer locates the first match. Docling drops those boxes, so its order looks better while it finds fewer sentences
(text 0.41 vs 0.97). Skipping infobox tables before the first heading is listed as future work.

**Result:** the engine beats every single tool on headings, text, tables, key-value pairs and formulas (accurate),
ties Docling on images, and is the only approach that gets all four element families right at once.

Azure DI: the Scope 1 run read only pages 1–2 (free tier) and the subscription's credits have since expired, so its
row is the Scope 1 measurement and is not re-run.

## Speed (seconds per document, same machine, warm)

| Document | plain | MarkItDown | Engine standard | Docling (Scope 1 run) |
|---|---|---|---|---|
| Paper1 (4 pages) | 0.63 | 0.76 | 0.95 | 206 |
| Paper2 (8 pages) | 0.63 | 0.75 | 0.87 | 159 |
| Wikipedia India | 0.84 | 1.61 | 0.88 | 21 |
| Wikipedia Python | 0.24 | 0.64 | 0.39 | 51 |

The engine costs ~0.2–0.3 s more than the plain readers and does all six element types in that pass; Docling is about
200× slower on CPU, which is why it is a witness only (optional container), not the producer.

## Where AI fixed what the parsers broke (live accurate run, `eval/outputs/engine_accurate/*.json`)

| # | Element | Without AI (rules / parsers) | With AI (accurate) | Evidence |
|---|---|---|---|---|
| 1 | **Display formula** (Paper1, eq. 3) | `N ∑︁ w βD p p min L(φ) = − i logp φ (ξ i ) + KL φ ∥ φ old (3) φ i=1`: the text layer loses every sub/superscript; every parser scores 0 on formulas | `\min_{\phi} L(\phi) = -\sum_{i=1}^{N} w_i \log p_{\phi}(\xi_i) + \beta D_{\mathrm{KL}}(p_{\phi} \| p_{\phi_{\mathrm{old}}})` | AI check pass, judge 1.0, confidence 0.98; formulas 0.33 → **1.00** |
| 2 | **Inline math** (Paper1, Introduction) | "26% of energy-related 𝐶𝑂 2 emissions" | "26% of energy-related $CO_2$ emissions" (rendered as CO₂) | judge 1.0, confidence 0.97; words checked against the rules' text (≥ 85%) |
| 3 | **Algorithm 1** (Paper1) | `Require: policy parameters θ, flow parameters φ 1: for n = 1 to N do 2: Train Policy(θ,p φ ) 3: D ← Sample And Refine(X,U)` (one run of text, symbols flattened) | one step per line, `$p_\phi$`, `$\mathcal{D} \leftarrow$`, keywords in bold | ≥ 70% of the numbered steps kept, judge 1.0 |

AI calls on the live run: Paper1 5 repair calls ($0.147 AI cost for the whole job incl. check and judge), Paper2 8 ($0.176).

## Where AI made things worse

| Element | What happened | What we did |
|---|---|---|
| **Key-value pairs** on page 1 (Paper1) | The AI was asked for key-value pairs on page 1 and returned the title, the author names, e-mails and affiliations as pairs ("Author: Pablo Boitel"): labels that are not printed on the page. The rules alone had found the right 2 pairs. | Each AI pair is now accepted only if the **label is printed directly before the value** in the page's own text and the label is not a heading. The invented pairs disappeared; the AI can still add a pair the rules missed. |

Also measured and rejected: the AI checker failed a correct inline-math paragraph only because "logPAMPA" was written
"log PAMPA" (spacing). The checker now passes spacing-only and notation-only differences at ~0.8 and still fails wrong
numbers, symbols, sub/superscripts or cells, the same rule the AI judge uses.

## Cost of the AI (accurate level)
Measured per job in the report (`ai_calls`, tokens, `cost_usd`) and summarised per 1,000 pages in the business case
(`engine/business_case.py`, using `scripts/benchmark_api.py` timings from the deployed stack).

# One tool per element type (experiment)

An experiment, separate from the Scope 2 engine: every element type is extracted by a different tool, each tool is
timed on its own, every element is scored against the answer key only, and one more step merges everything back
into reading order. One HTML viewer per document shows the source and the result side by side.

| Element type | Tool (PDF) | Tool (web page) | Why this tool |
|---|---|---|---|
| Headings | pypdf | BeautifulSoup + requests | pypdf exposes each text piece's font and size, which is what marks a heading |
| Tables | pdfplumber | BeautifulSoup + requests | pdfplumber has a table finder that works on the PDF's lines and word positions |
| Text | MarkItDown | MarkItDown | A plain text converter: shows what a simple reader gets |
| Formulas | Amazon Textract | not run (reads PDFs/images only) | Textract has no formula feature: it reads the page text (DetectDocumentText) and a rule picks the formula lines (a line ending in an equation number like "(3)" with a math sign, plus the short lines just above/below). Plain text, not LaTeX |
| Key-value pairs | Azure Document Intelligence | not run (reads PDFs/images only) | prebuilt-layout with the free `keyValuePairs` add-on |

Earlier version: Textract FORMS did the key-values (about $0.05 a page, and on research papers almost every pair was
wrong) and Azure did the formulas. Textract's text reading (DetectDocumentText) is about $0.0015 a page.
| Images | Docling | Docling | Its layout model finds pictures and links their captions |

## Run it (Windows PowerShell)

```powershell
cd "C:\Users\win 10\scope2-extraction-engine\tool_per_element"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt          # Docling is large (PyTorch); the first run also downloads its models

# Azure Document Intelligence (key-value pairs): set the two values for this window, or pass --env <your .env file>
$env:AZURE_DI_ENDPOINT = "https://<your-resource>.cognitiveservices.azure.com"
$env:AZURE_DI_KEY = "<key>"
# (AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT/_KEY or AZURE_FORM_RECOGNIZER_ENDPOINT/_KEY are accepted too)

# Amazon Textract (formulas): your normal AWS login
aws sso login --profile <your-profile>
$env:AWS_PROFILE = "<your-profile>"

python run.py ..\data\test_docs\Paper1.pdf ..\data\test_docs\Paper2.pdf ..\data\web_cache\wikipedia_python.html ..\data\web_cache\wikipedia_india.html
start out\index.html
```

MarkItDown: by default its "form-like page" check is switched off (one line in `extractors.py`), so every page is
read with its pdfminer reader. With the check on, pages with figures are read line by line across both columns and
words are glued together ("ACMSustainabilityWeek"). Measured on the two papers: glued words 186 -> 9 and 104 -> 7,
answer-key sentences found 5/8 -> 8/8 and 4/6 -> 6/6, about 0.25 s slower per paper. To see MarkItDown as shipped:
`python run.py ... --markitdown-as-is` (results go to `out_markitdown_as_is\`).

A tool you cannot run yet: add `--skip azure` / `--skip textract,docling`. A failing tool never stops the run; its
error is shown in the viewer. The answer key is found automatically in `..\eval\ground_truth\` (the file whose
`"doc"` is the document's name); a live URL needs `--key ..\eval\ground_truth\wikipedia_python.json`.

Docling on Python 3.14: if `pip install docling` fails, make the venv with Python 3.12 (`py -3.12 -m venv .venv`).

## What the viewer shows

- **Table per element type**: tool, status, number extracted, **time in seconds** for that tool on the whole
  document, ms per element, mean confidence, exact matches, answer-key elements found, coverage of the key.
- **Reading order score**: answer-key headings and sentences are listed in printed order; it counts the
  neighbouring pairs that keep that order in the merged output.
- **Left**: the PDF pages with a coloured box for every element (dashed = estimated box; pypdf gives no line width).
  Web pages: the saved page itself. **Right**: the merged Markdown in reading order. Click either side to find the
  other.
- Each element: type, tool, **confidence badge**, and when it is not exact, a character diff against the answer key
  (red = written by the tool but not printed, green = printed but missing).
- At the bottom: answer-key elements nothing matched.

## How the confidence is computed (scoring.py)

Only the answer key (`eval/ground_truth/*.json`, elements read by a person from the rendered pages) is used.

- 1.00 = exactly as printed (line breaks, repeated spaces, letter case and Markdown symbols ignored).
- Otherwise character similarity (difflib ratio): "Abstrac" vs "Abstract" = 0.93.
- Formulas: LaTeX compared without spaces, braces and `\left`/`\right`. Tables: exactly matching cells over the
  larger cell count (missing and extra cells both count). Images: 1 point for a figure on the right page + 1 per
  caption keyword found. Key-values: "key: value" text.
- An extracted element with no counterpart (similarity < 0.5) scores 0 ("not in the answer key").
- Text: the key holds sample sentences, not every paragraph. A paragraph is scored on the key sentences it contains;
  other paragraphs are "not checked" and never counted.
- Matching is one-to-one, page by page.

## Reading order (ordering.py)

Each tool's positions are converted to page fractions (pypdf and Docling count from the bottom in points, pdfplumber
from the top, Azure in inches, Textract in fractions). MarkItDown gives no positions: its paragraphs are located by
their first and last words in the PDF's word list (used only as a ruler). Two-column pages (almost no word crosses
the middle) are read left column, then right column, between full-width elements; one-column pages top to bottom.
MarkItDown paragraphs that repeat a heading, table, key-value or caption found by another tool are hidden in the
merged view (tick "show paragraphs hidden as duplicates"); they still count in the text scores.

## Files

| File | What it does |
|---|---|
| `run.py` | Runs the six tools one after another (timed), scores, merges, writes the viewer |
| `extractors.py` | One function per tool and element type, PDF and web |
| `scoring.py` | Confidence against the answer key, per element and per type |
| `ordering.py` | Reading-order merge, duplicate hiding, reading-order score |
| `viewer.py` | The HTML viewer |
| `out/<doc>/` | `viewer.html`, `elements.json` (every element with box, score, time), `merged.md`, `summary.csv` |

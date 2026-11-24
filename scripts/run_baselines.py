"""Run every Scope 1 tool (and the Scope 2 engine) on the test documents, so eval/evaluate.py can score them.

Run from the PROJECT ROOT:

    python scripts/run_baselines.py                          # all tools that are available
    python scripts/run_baselines.py --tools opensource_md,markitdown
    python scripts/run_baselines.py --tools engine_standard,engine_accurate
    python scripts/run_baselines.py --force                  # redo outputs that already exist

SETUP (once): copy your Scope 1 programs into  baselines/  (they are imported, not rewritten):
    azure_di.py  azure_web.py  docling_markitdown.py  opensource_markdown.py  md_utils.py  md_pdf.py
    webscraper.py  extractor.py

TOOLS (each one writes eval/outputs/<tool>/<name>.md)
    pypdf_text        YOUR extractor.extract_text_with_pypdf (plain text, the weakest baseline)
    opensource_md     YOUR Scope 1 open-source reassembly: opensource_markdown.pdf_to_markdown (PDF)
                      and webscraper.html_to_markdown (web)  = pdfplumber + requests/BeautifulSoup
    markitdown        YOUR docling_markitdown.convert_pdf_with_markitdown (PDF); MarkItDown directly (web)
    docling           YOUR docling_markitdown.convert_pdf_with_docling (PDF) and get_docling_converter (web),
                      same options as Scope 1; web pages get the cleaned <name>_main.html
    azure             YOUR azure_di.pdf_to_markdown (PDF, 2-page chunks) and azure_web (HTML slice)
                      -> skipped automatically when AZURE_DI_ENDPOINT / AZURE_DI_KEY are not set
    engine_standard   the Scope 2 engine, rules only
    engine_accurate   the Scope 2 engine + AI (needs AI_PROVIDER=gemini|bedrock and its key)

Every run happens in its own process with a timeout (BASELINE_TIMEOUT seconds, default 900), so one slow
tool cannot block the others. A timeout is recorded as "timeout": that is itself a result.
Times go to eval/outputs/baseline_times.csv (tool, document, status, seconds).
"""
import argparse
import csv
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINES = ROOT / "baselines"
TIMEOUT = int(os.environ.get("BASELINE_TIMEOUT", "900"))
UA = "Mozilla/5.0 (compatible; scope2-benchmark/0.2; student project)"

URLS = {
    "wikipedia_python": "https://en.wikipedia.org/wiki/Python_(programming_language)",
    "wikipedia_india": "https://en.wikipedia.org/wiki/India",
}
# ground-truth name -> word that identifies the paper. It is looked for in the FILE NAME first and then in the
# TEXT OF PAGE 1, so Paper1.pdf / Paper2.pdf work. Change a word here if your paper uses a different one.
PDF_KEYS = {"protac_permeability": "protac", "hvac_nomad_rl": "hvac"}
PDF_DIRS = ["data/test_docs", "data"]

PDF_ONLY = {"pypdf_text"}


def _first_page_text(pdf):
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf)) as d:
            return (d.pages[0].extract_text() or "").lower()
    except Exception:
        return ""


def find_pdfs():
    """{ground-truth name: pdf path}. Looks in data/test_docs first, then anywhere under data/."""
    pdfs = []
    for d in PDF_DIRS:
        pdfs = sorted((ROOT / d).rglob("*.pdf")) if (ROOT / d).exists() else []
        if pdfs:
            break
    found = {}
    for name, key in PDF_KEYS.items():
        hit = next((p for p in pdfs if key in p.name.lower()), None)
        if hit is None:
            hit = next((p for p in pdfs if p not in found.values() and key in _first_page_text(p)), None)
        if hit:
            found[name] = hit
    return found


def find_docs():
    pdfs = find_pdfs()
    docs = {name: pdfs.get(name) for name in PDF_KEYS}
    docs.update(URLS)
    return docs


def main_html(name, url, full_html):
    """Scope 1 (web_converters.py) strips menus/headers/footers with webscraper.parse_html and gives Docling and
    MarkItDown the cleaned file <name>_main.html. Do exactly the same, so the comparison is the same."""
    f = full_html.with_name(f"{name}_main.html")
    if not f.exists() or f.stat().st_size == 0:
        sys.path.insert(0, str(BASELINES))
        import webscraper
        content = webscraper.parse_html(full_html.read_text(encoding="utf-8"), url)[0]
        f.write_text(f"<html><body>{content}</body></html>", encoding="utf-8")
    return f


def fetch_html(name, url):
    """Wikipedia blocks the default Python user-agent. Download once, then give every tool the SAME file."""
    f = ROOT / "data" / "web_cache" / f"{name}.html"
    if f.exists() and f.stat().st_size > 0:
        return f
    import requests
    r = requests.get(url, timeout=30, headers={"User-Agent": UA})
    r.raise_for_status()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(r.text, encoding="utf-8")
    return f


# Each snippet runs in a fresh process. Variables set before it: SRC (file path), URL (None for PDFs),
# NAME, OUT (markdown path).
HEAD = ("import sys\n"
        f"sys.path.insert(0, {str(BASELINES)!r})\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n")

CODE = {
    "pypdf_text": (                      # YOUR extractor.extract_text_with_pypdf (plain text, one file per PDF)
        "import extractor, tempfile, shutil\n"
        "from pathlib import Path\n"
        "tmp = Path(tempfile.mkdtemp())\n"
        "extractor.extract_text_with_pypdf(SRC, tmp)\n"
        "shutil.copy(tmp / (Path(SRC).stem + '.txt'), OUT)\n"),

    "opensource_md": (
        "if URL:\n"
        "    import webscraper\n"
        "    t = webscraper.html_to_markdown(open(SRC, encoding='utf-8').read(), URL)\n"
        "    open(OUT, 'w', encoding='utf-8').write(t)\n"
        "else:\n"
        "    import opensource_markdown\n"
        "    opensource_markdown.pdf_to_markdown(SRC, OUT)\n"),

    "markitdown": (                      # PDF: YOUR docling_markitdown.convert_pdf_with_markitdown; HTML: MarkItDown
        "if URL:\n"
        "    from markitdown import MarkItDown\n"
        "    t = MarkItDown().convert(SRC).text_content\n"
        "    open(OUT, 'w', encoding='utf-8').write(t)\n"
        "else:\n"
        "    import docling_markitdown\n"
        "    docling_markitdown.convert_pdf_with_markitdown(SRC, OUT)\n"),

    "docling": (
        "if URL:\n"
        "    import docling_markitdown\n"
        "    t = docling_markitdown.get_docling_converter().convert(SRC).document.export_to_markdown()\n"
        "    open(OUT, 'w', encoding='utf-8').write(t)\n"
        "else:\n"
        "    import docling_markitdown\n"
        "    docling_markitdown.convert_pdf_with_docling(SRC, OUT)\n"),

    "azure": (
        "if URL:\n"
        "    import azure_web, webscraper\n"
        "    from pathlib import Path\n"
        "    html = open(SRC, encoding='utf-8').read()\n"
        "    content = webscraper.parse_html(html, URL)[0]\n"
        "    slice_text, n = azure_web.slice_html(content)\n"
        "    sp = Path(SRC).with_name(NAME + '_slice.html')\n"
        "    sp.write_text(slice_text, encoding='utf-8')\n"
        "    res, method = azure_web.analyze(sp, NAME)\n"
        "    open(OUT, 'w', encoding='utf-8').write(res.content)\n"
        "else:\n"
        "    import azure_di\n"
        "    md, info = azure_di.pdf_to_markdown(SRC)\n"
        "    open(OUT, 'w', encoding='utf-8').write(md)\n"),

    "engine_standard": (
        "from engine import extract\n"
        "extract.run(URL or SRC, OUT, 'standard')\n"),

    "engine_accurate": (
        "from engine import extract\n"
        "extract.run(URL or SRC, OUT, 'accurate')\n"),
}


def tool_available(tool):
    if tool == "azure":
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        return bool(os.getenv("AZURE_DI_ENDPOINT") and os.getenv("AZURE_DI_KEY"))
    if tool == "engine_accurate":
        return os.environ.get("AI_PROVIDER", "none").lower() in ("gemini", "bedrock", "mock")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", default=",".join(CODE))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if not BASELINES.exists():
        print(f"NOTE: {BASELINES} not found. Copy your Scope 1 programs there (opensource_markdown.py, "
              "md_utils.py, webscraper.py, docling_markitdown.py, azure_di.py, azure_web.py).")

    docs = find_docs()
    rows = []
    for tool in a.tools.split(","):
        tool = tool.strip()
        if tool not in CODE:
            print(f"[skip] unknown tool {tool}")
            continue
        if not tool_available(tool):
            print(f"[skip] {tool}: credentials / AI_PROVIDER not set")
            continue
        for name, src in docs.items():
            is_url = name in URLS
            if tool in PDF_ONLY and is_url:
                continue
            if src is None:
                print(f"[skip] {tool}/{name}: PDF not found: no PDF in data/test_docs mentions '{PDF_KEYS[name]}' (file name or page 1)")
                continue
            out = ROOT / "eval" / "outputs" / tool / f"{name}.md"
            if out.exists() and out.stat().st_size > 0 and not a.force:
                print(f"[have] {tool}/{name} (use --force to redo)")
                continue
            url = None
            if is_url:
                url = src
                try:
                    src = fetch_html(name, url)
                except Exception as e:
                    print(f"[error] {tool}/{name}: could not download page: {e}")
                    continue
            if is_url and tool in ("markitdown", "docling"):
                src = main_html(name, url, src)               # same cleaned input as Scope 1 web_converters.py
            out.parent.mkdir(parents=True, exist_ok=True)
            prog = (HEAD + f"SRC = {str(src)!r}\nURL = {url!r}\nNAME = {name!r}\nOUT = {str(out)!r}\n"
                    + CODE[tool])
            t0 = time.time()
            try:
                r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True,
                                   timeout=TIMEOUT, cwd=str(ROOT),
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                secs = round(time.time() - t0, 1)
                if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
                    status = "ok"
                else:
                    status = "error"
                    tail = (r.stderr.strip().splitlines() or ["(no message)"])[-1]
                    print(f"[error] {tool}/{name}: {tail}")
            except subprocess.TimeoutExpired:
                secs, status = TIMEOUT, "timeout"
            if status != "ok":
                try:
                    out.unlink(missing_ok=True)
                    out.parent.rmdir()          # drop an empty tool folder so it is not scored as a 0 tool
                except OSError:
                    pass
            print(f"[{status}] {tool}/{name}  {secs}s")
            rows.append([tool, name, status, secs])

    if rows:
        f = ROOT / "eval" / "outputs" / "baseline_times.csv"
        f.parent.mkdir(parents=True, exist_ok=True)
        new = not f.exists()
        with open(f, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["tool", "document", "status", "seconds"])
            w.writerows(rows)
        print("\nTimes saved to eval/outputs/baseline_times.csv")
        print("Next:  python eval/evaluate.py")


if __name__ == "__main__":
    main()

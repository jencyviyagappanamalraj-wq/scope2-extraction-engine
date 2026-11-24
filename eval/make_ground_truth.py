"""Build eval/ground_truth/*.json for the two papers.

HOW THIS GROUND TRUTH WAS MADE (so you can defend it):
  1. The elements below were READ BY A PERSON from the rendered pages of Paper1.pdf and Paper2.pdf.
     No extraction tool's output was used as the source of any element, so no tool gets an unfair advantage.
  2. This script only does two mechanical things: it finds the PAGE of each heading / sentence / caption in the
     PDF's own text layer, and it STOPS with an error if an element cannot be found in the PDF (typo, invented text).
  3. Formulas and table cells were typed from the page image (the text layer is garbled for equations).

    python eval/make_ground_truth.py          # writes eval/ground_truth/<name>.json
    python eval/check_ground_truth.py         # independent re-check

Add or change elements in SPEC, run again. LIST ELEMENTS IN READING ORDER (left column before right column): the scorer checks order.
Keep: every heading, every key-value, every table, every display
formula, every figure, and a spread of sentences from different pages (and columns).
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from run_baselines import find_pdfs          # Paper1 / Paper2 are found by their page-1 text

OUT = ROOT / "eval" / "ground_truth"


def norm(s):
    """Letters and digits only, lower case: immune to spacing, hyphenation, quotes and ligatures."""
    s = unicodedata.normalize("NFKC", s).lower().replace("\u00ad", "")
    return re.sub(r"[^\w]", "", s)


def pages_text(pdf_path):
    """Per page, ONE searchable string made of several independent readings of the page text layer:
    whole-row text, left half, right half (two-column papers), and a second library (pypdf).
    A real element appears in at least one of them; an invented one appears in none."""
    from pypdf import PdfReader
    out = []
    reader = PdfReader(str(pdf_path))
    with pdfplumber.open(str(pdf_path)) as pdf:
        for i, page in enumerate(pdf.pages):
            w, h = page.width, page.height
            parts = [page.extract_text(x_tolerance=1.5) or "",
                     page.crop((0, 0, w / 2, h)).extract_text(x_tolerance=1.5) or "",
                     page.crop((w / 2, 0, w, h)).extract_text(x_tolerance=1.5) or ""]
            try:
                parts.append(reader.pages[i].extract_text() or "")
            except Exception:
                pass
            out.append(" ".join(norm(p) for p in parts))
    return out


SPEC = {
    # ------------------------------------------------------------------ Paper1: HVAC (two-column ACM paper)
    "hvac_nomad_rl": {
        "headings": [
            "Generalizing HVAC Control With Domain Randomized Reinforcement Learning",
            "Abstract", "CCS Concepts", "Keywords", "1 Introduction", "2 Method", "2.1 Thermal zone modeling",
            "2.2 Reinforcement learning controller", "2.3 Domain randomization for RL training",
            "2.4 Training configuration", "3 Results and evaluation of NOMAD-RL", "3.1 Single-zone setting",
            "3.2 Multi-zone setting", "4 Conclusion", "References"],
        "key_values": [
            ("ACM ISBN", "979-8-4007-2199-1/2026/06"),
            ("Keywords", "HVAC, Reinforcement learning, Control, Domain randomization"),
            ("This work is licensed under a", "Creative Commons Attribution 4.0 International License"),
        ],
        "texts": [
            "Model Predictive Control (MPC) remains the reference approach because it can explicitly balance comfort and energy use under operational constraints.",
            "Experiments in both single-zone and multi-zone settings show that NOMAD-RL consistently outperforms constant-setpoint and non-randomized RL baselines, while approaching the performance of a well-tuned MPC.",
            "The model is implemented in JAX to support efficient simulation and automatic differentiation.",
            "We train the controller with Recurrent PPO, using the standard actor\u2013critic implementation from the SB3-Contrib library.",
            "This utility yields the Boltzmann weights, i.e., the analytical solution of the associated expectation-maximization problem:",
            "NOMAD-RL reduces cost by 70.4% relative to PID, but is 8.7% more costly than recurrent PPO on the nominal zone.",
            "NOMAD-RL remains effective in this more challenging setting: it reduces cost by 67.9% relative to constant-setpoint PID and is 14.5% more costly than MPC.",
            "Future work will focus on improving performance and real-world readiness through further training, hyperparameter tuning, expansion of the simulator and dataset pipeline, and validation on real buildings.",
        ],
        # typed from the page image (page 3), with alternative spellings the text layer may give
        "formulas": [
            (3, r"U(\xi) = J_\theta(\xi) - \lambda_{phys} C_P(\xi) + \lambda_{sur}(-\log p_\phi(\xi))",
             ["U(ξ) = Jθ(ξ) − λphys CP(ξ) + λsur(− log pϕ(ξ))"]),
            (3, r"w_i = \mathrm{softmax}\left(\frac{U(\xi_i)}{\tau}\right)_i",
             ["wi = softmax(U(ξi)/τ)i", "w_i = softmax(U(xi_i)/tau)_i"]),
            (3, r"\min_\phi L(\phi) = -\sum_{i=1}^{N} w_i \log p_\phi(\xi_i) + \beta D_{KL}(p_\phi \| p_{\phi_{old}})",
             ["min L(ϕ) = − Σ wi log pϕ(ξi) + βDKL(pϕ ∥ pϕold)"]),
        ],
        "tables": [],
        "figures": [("Figure 1", ["architecture", "NOMAD-RL"]), ("Figure 2", ["parameter-flow", "distribution"]),
                    ("Figure 3", ["behavior", "training"]), ("Figure 4", ["recurrent", "nominal"]),
                    ("Figure 5", ["MPC", "sampled"])],
    },
    # ------------------------------------------------------------------ Paper2: PROTAC (single-column NeurIPS paper)
    "protac_permeability": {
        "headings": [
            "The Challenges of PROTAC Permeability Prediction", "Abstract", "1 Introduction", "2 Background",
            "Permeability prediction for degraders", "Agentic extraction of assay data",      # run-in sub-headings
            "3 Methods", "3.1 Data", "Original dataset", "New dataset",
            "3.2 Model training and evaluation", "4 Results", "Agentic data extraction", "Permeability modeling",
            "5 Discussion", "Performance on PROTAC-DB 3.0 does not transfer",
            "The public record is organized by publication, not by chemistry", "Ranking vs. absolute prediction",
            "Transferring curation workflows across endpoint classes",
            "5.1 Limitations and future work", "6 Conclusion", "Author contributions",
            "Acknowledgments and disclosure of funding", "Software and data availability", "References"],
        "key_values": [
            ("Data and code are available at", "https://github.com/xhudec2/protac-permeability"),
        ],
        "texts": [
            "Cell permeability is a key bottleneck for PROTAC development, and public data available to model it is scarce and inconsistent.",
            "The largest public resource, PROTAC-DB 3.0 [3], contains just 31 PROTACs with PAMPA measurements.",
            "That workflow targets the pharmacodynamic axis of degradation, however, and doesn\u2019t cover ADME properties.",
            "Alpha is set to 1.0 for all models.",
            "We believe a model trained on PROTAC-DB data does not generalize to the newly-extracted data because PROTAC-DB spans a narrow region of degrader chemistry that the extracted set includes.",
            "Predictive models for new modalities depend on high-quality published data, and for PROTAC permeability we are not there yet.",
        ],
        "formulas": [],
        # Table 1, typed from the page image. The merged "Evaluation data" cells are written on every row they span.
        "tables": [("Table 1", [
            ["Method", "Evaluation data", "Spearman \u03c1 \u2191", "RMSE \u2193", "R\u00b2 \u2191"],
            ["Ridge (PROTAC-DB 3.0)", "PROTAC-DB 3.0", "0.82 (0.06)", "0.67 (0.08)", "0.67 (0.08)"],
            ["Ridge (New)", "PROTAC-DB 3.0", "0.80 (0.02)", "1.07 (0.04)", "0.18 (0.06)"],
            ["Ridge (All)", "PROTAC-DB 3.0", "0.82 (0.03)", "0.82 (0.07)", "0.51 (0.08)"],
            ["Ridge (PROTAC-DB 3.0)", "New data", "0.12 (0.03)", "1.96 (0.03)", "-0.88 (0.06)"],
            ["Ridge (New)", "New data", "0.30 (0.09)", "1.47 (0.05)", "-0.07 (0.08)"],
            ["Ridge (All)", "New data", "0.33 (0.06)", "1.40 (0.04)", "0.04 (0.05)"],
            ["Ridge (PROTAC-DB 3.0)", "All data", "0.36 (0.03)", "1.64 (0.03)", "-0.34 (0.05)"],
            ["Ridge (New)", "All data", "0.50 (0.05)", "1.35 (0.04)", "0.09 (0.05)"],
            ["Ridge (All)", "All data", "0.53 (0.04)", "1.23 (0.04)", "0.24 (0.05)"]])],
        "figures": [("Figure 1", ["extraction", "pipeline"]), ("Figure 2", ["distribution", "comparison"]),
                    ("Figure 3", ["parity", "plots"])],
    },
}


def page_of(pages, needle, start=1):
    """First page (>= start) whose text layer contains `needle`; None if absent."""
    n = norm(needle)
    for i in range(start - 1, len(pages)):
        if n in pages[i]:
            return i + 1
    return None


def main():
    pdfs = find_pdfs()
    OUT.mkdir(parents=True, exist_ok=True)
    errors = 0
    for name, spec in SPEC.items():
        if name not in pdfs:
            print(f"{name}: PDF not found under data/test_docs (skipped)")
            continue
        pages = pages_text(pdfs[name])
        els = []

        def need(kind, label, page):
            nonlocal errors
            if page is None:
                errors += 1
                print(f"  NOT FOUND in the PDF ({name}, {kind}): {label[:70]}")
            return page or 1

        for h in spec["headings"]:
            # a heading line must not be matched inside the running header, so search from page 1 forward
            els.append({"type": "heading", "page": need("heading", h, page_of(pages, h)), "text": h})
        for k, v in spec["key_values"]:
            els.append({"type": "key_value", "page": need("key_value", v, page_of(pages, v)), "key": k, "value": v})
        for t in spec["texts"]:
            els.append({"type": "text", "page": need("text", t, page_of(pages, t[:80])), "text": t})
        for page, latex, alts in spec["formulas"]:
            els.append({"type": "formula", "page": page, "latex": latex, "alternatives": alts})
        for label, cells in spec["tables"]:
            els.append({"type": "table", "page": need("table", label, page_of(pages, label + ":")), "cells": cells})
        for label, kw in spec["figures"]:
            els.append({"type": "image", "page": need("figure", label, page_of(pages, label + ":")),
                        "caption_keywords": kw})
        (OUT / f"{name}.json").write_text(
            json.dumps({"doc": pdfs[name].name, "elements": els}, indent=1, ensure_ascii=False), encoding="utf-8")
        counts = {}
        for e in els:
            counts[e["type"]] = counts.get(e["type"], 0) + 1
        print(f"{name}: {len(els)} elements {counts}")
    if errors:
        print(f"\n{errors} element(s) were not found in the PDF text. Fix them in SPEC.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

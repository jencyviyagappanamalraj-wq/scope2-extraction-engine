"""AI helper for the ACCURATE level. One small interface, three providers:

    AI_PROVIDER=bedrock   Amazon Bedrock (uses your AWS credentials; set BEDROCK_MODEL_ID, e.g. the
                          inference-profile ID shown in the Bedrock playground; region us-east-1)
    AI_PROVIDER=gemini    Google AI Studio free tier (set GEMINI_API_KEY; optional GEMINI_MODEL)
    AI_PROVIDER=none      no AI (Standard behaviour)
    AI_PROVIDER=mock      fake answers, for testing the plumbing offline

Speed features: results are cached on disk by (image, prompt) hash, calls run in parallel, and the
engine only sends the few elements it is unsure about.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROMPTS = {
    "formula": ("This image shows mathematical equations from a scientific paper. Transcribe them as LaTeX. "
                "Output ONLY the LaTeX (no $ signs, no explanation). Put each equation on its own line and "
                "drop equation numbers."),
    "inline_math": ("This image shows one paragraph from a document. Transcribe it exactly as printed, as plain "
                    "Markdown text. Write every mathematical symbol, variable and expression as inline LaTeX between "
                    "single $ signs, keeping all subscripts and superscripts (for example $J_\\theta(\\xi)$, "
                    "$\\log p_\\phi(\\xi)$, $CO_2$, $R^2$). Join words split by a hyphen at a line break. "
                    "Do not add, drop, reorder or explain anything. Output ONLY the paragraph."),
    "paragraph": ("This image shows one paragraph from a document. Transcribe it exactly as printed, as plain text. "
                  "Keep symbols as the characters printed (for example CO₂, R², θ). Join words split by a hyphen at a "
                  "line break. Do not add, drop, reorder or explain anything. Output ONLY the paragraph."),
    "algorithm": ("This image shows an algorithm (pseudocode) box from a document. Transcribe it exactly, one output "
                  "line per printed line, in order. First line: **Algorithm N** and its title. Keep every line number "
                  "(1:, 2:, ...), write keywords in bold (**Require:**, **Ensure:**, **for**, **do**, **while**, "
                  "**if**, **then**, **else**, **end for**, **return**), write function names in small capitals as normal "
                  "words, and write every mathematical symbol, variable and expression as inline LaTeX between single "
                  "$ signs, keeping all subscripts and superscripts (e.g. $p_\\phi$, $\\mathcal{D}$). "
                  "Do not add, drop or explain anything. Output ONLY the lines."),
    "table": ("This image shows a table from a scientific paper. Transcribe it as a GitHub-flavoured Markdown "
              "table, one row per line. Keep every number exactly as printed, including values in brackets. "
              "If a label spans several rows, repeat it on every row it spans. Output ONLY the table."),
    "kv": ("List the key-value pairs printed on this page: a LABEL that is printed on the page directly followed by "
           "its value, such as \"Keywords: ...\", \"ACM ISBN 979-...\", \"DOI: ...\", \"This work is licensed under "
           "a ...\" or \"Data and code are available at ...\". Copy label and value exactly as printed. Do NOT invent "
           "labels, and do not list the title, author names, e-mails, affiliations, addresses, page headers or "
           "section headings. One pair per line in the form label: value. Output ONLY these lines."),
    "image": "Describe this figure in one or two factual sentences (what is drawn or plotted). Output ONLY the description.",
}


class Stats:
    def __init__(self):
        self.calls = self.cache_hits = self.input_tokens = self.output_tokens = self.errors = self.skipped = 0
        self.seconds = 0.0
        self.by_kind = {}                      # per AI use: formula / table / kv / image / qc ...
        self._lock = threading.Lock()

    def add(self, kind, tin, tout, cached):
        with self._lock:
            d = self.by_kind.setdefault(kind, {"calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0})
            d["cache_hits" if cached else "calls"] += 1
            d["input_tokens"] += tin           # tokens are counted even for cache hits = cost as if not cached
            d["output_tokens"] += tout

    @staticmethod
    def price(tin, tout):
        """USD. Set AI_PRICE_IN_PER_1K / AI_PRICE_OUT_PER_1K from the Bedrock pricing page for your model."""
        return (tin / 1000 * float(os.environ.get("AI_PRICE_IN_PER_1K", "0"))
                + tout / 1000 * float(os.environ.get("AI_PRICE_OUT_PER_1K", "0")))

    def as_dict(self):
        by_kind = {k: {**v, "cost_usd": round(self.price(v["input_tokens"], v["output_tokens"]), 6)}
                   for k, v in self.by_kind.items()}
        return {"ai_calls": self.calls, "cache_hits": self.cache_hits, "errors": self.errors, "ai_skipped_time_budget": self.skipped, "skipped_over_budget": self.skipped,
                "input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "ai_seconds_total": round(self.seconds, 2),
                "ai_cost_usd": round(sum(v["cost_usd"] for v in by_kind.values()), 6),
                "ai_cost_by_kind": by_kind}


class Provider:
    name = "none"

    def ask(self, png: bytes, prompt: str):
        raise NotImplementedError


class Bedrock(Provider):
    name = "bedrock"

    def __init__(self):
        import boto3
        self.model = os.environ["BEDROCK_MODEL_ID"]
        from botocore.config import Config
        self.client = boto3.client(
            "bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "us-east-1"),
            config=Config(read_timeout=45, connect_timeout=10, retries={"max_attempts": 2, "mode": "standard"}))

    def ask(self, png, prompt):
        r = self.client.converse(
            modelId=self.model,
            messages=[{"role": "user", "content": [
                {"image": {"format": "png", "source": {"bytes": png}}}, {"text": prompt}]}],
            inferenceConfig={"maxTokens": 1500, "temperature": 0},
        )
        text = "".join(b.get("text", "") for b in r["output"]["message"]["content"])
        u = r.get("usage", {})
        return text.strip(), u.get("inputTokens", 0), u.get("outputTokens", 0)


class Gemini(Provider):
    name = "gemini"

    def __init__(self):
        self.key = os.environ["GEMINI_API_KEY"]
        self.model = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

    def ask(self, png, prompt):
        import requests
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {"contents": [{"parts": [
            {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(png).decode()}},
            {"text": prompt}]}], "generationConfig": {"temperature": 0}}
        # key in a header, not the URL: an HTTP error message contains the URL and would leak the key into logs
        r = requests.post(url, headers={"x-goog-api-key": self.key}, json=body, timeout=90)
        if r.status_code >= 400:
            raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        text = "".join(p.get("text", "") for p in j["candidates"][0]["content"].get("parts", []))
        u = j.get("usageMetadata", {})
        # thinking tokens are billed as output tokens, so count them in the cost
        return text.strip(), u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)


class Mock(Provider):
    name = "mock"

    def ask(self, png, prompt):
        time.sleep(0.05)
        if "strict quality checker" in prompt:
            return '{"verdict": "pass", "score": 0.95, "issues": []}', 150, 20
        if "key-value" in prompt:
            return "ACM ISBN: 979-8-4007-2199-1/2026/06\nInvented label: a value that is not on the page", 100, 20
        if "strict judge" in prompt:                # mock judge: every listed element scores 0.9
            ids = re.findall(r'"id": (\d+)', prompt)
            return json.dumps({"elements": [{"id": int(i), "score": 0.9, "issue": ""} for i in ids], "missing": ""}), 300, 50
        if "one paragraph from a document" in prompt or "inline LaTeX" in prompt:
            return "", 100, 0                       # mock cannot read the image: empty answer -> element kept as is
        if "LaTeX" in prompt:
            return "x^{2} + y^{2} = z^{2}", 100, 10
        if "Markdown" in prompt:
            return "| A | B |\n|---|---|\n| 1 | 2 |", 100, 20
        return "A mock description.", 100, 10


def get_provider():
    name = os.environ.get("AI_PROVIDER", "none").lower()
    return {"bedrock": Bedrock, "gemini": Gemini, "mock": Mock}.get(name, lambda: None)()


def ask_cached(provider, stats, png, kind, cache_dir=None, tries=3, deadline=None):
    cache_dir = cache_dir or os.environ.get("AI_CACHE_DIR", ".cache/ai")
    prompt = PROMPTS[kind]
    key = hashlib.sha1(png + prompt.encode() + provider.name.encode()).hexdigest()
    f = Path(cache_dir) / f"{key}.json"
    if f.exists():
        stats.cache_hits += 1
        j = json.loads(f.read_text(encoding="utf-8"))
        stats.add(kind, j.get("tin", 0), j.get("tout", 0), cached=True)
        return j["text"]
    for attempt in range(tries):
        if deadline and time.time() > deadline:       # time budget used up: skip, keep the rule-based result
            stats.skipped += 1
            return None
        t = time.time()
        try:
            text, tin, tout = provider.ask(png, prompt)
            stats.calls += 1
            stats.input_tokens += tin
            stats.output_tokens += tout
            stats.seconds += time.time() - t
            stats.add(kind, tin, tout, cached=False)
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"text": text, "tin": tin, "tout": tout}), encoding="utf-8")
            return text
        except Exception as e:                      # rate limit, network, access error ...
            stats.errors += 1
            last = e
            time.sleep(2 ** attempt)
    print("AI call failed:", last)
    return None


def run_parallel(provider, stats, jobs, workers=4, budget=None):
    """jobs = list of (png_bytes, kind). Returns texts (None = failed or ran out of time), same order.
    `budget` = max seconds to wait for all AI answers (default env AI_BUDGET_SECONDS, 60)."""
    from concurrent.futures import wait
    budget = budget if budget is not None else float(os.environ.get("AI_BUDGET_SECONDS", "60"))
    workers = int(os.environ.get("AI_PARALLEL", workers))   # lower it for rate-limited keys (free Gemini tier)
    ex = ThreadPoolExecutor(max_workers=workers)
    futures = [ex.submit(ask_cached, provider, stats, png, kind) for png, kind in jobs]
    wait(futures, timeout=budget)
    ex.shutdown(wait=False, cancel_futures=True)
    return [f.result() if f.done() and not f.cancelled() else None for f in futures]

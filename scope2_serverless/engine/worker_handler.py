"""Worker Lambda (scope2-worker): triggered by SQS, one job per message. Runs the ensemble pipeline
(rules + witness tools + Bedrock repair/check/judge) and stores Markdown + JSON in S3, status/report in DynamoDB."""
import json
import os
import shutil
import tempfile
import zipfile
import time

import boto3
import pdfplumber
import requests

import common
import pipeline

ddb = boto3.resource("dynamodb")
s3 = boto3.client("s3")
JOBS = ddb.Table(os.environ["JOBS_TABLE"])
BUCKET = os.environ["BUCKET_NAME"]
MAX_BYTES = 50 * 1024 * 1024
UA = {"User-Agent": "scope2-extractor/0.1 (student project)"}


def set_job(job_id, **fields):
    names = {f"#{k}": k for k in fields}
    values = common.to_ddb({f":{k}": v for k, v in fields.items()})
    expr = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    JOBS.update_item(Key={"job_id": job_id}, UpdateExpression=expr,
                     ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def download_pdf(url, dest):
    """Returns (True, url) if the URL was a PDF (saved to dest), (False, final_url) for a normal web page.
    Every redirect hop is checked, so a public URL cannot bounce the worker to an internal address."""
    for _ in range(4):
        common.check_url(url)
        with requests.get(url, stream=True, timeout=30, headers=UA, allow_redirects=False) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                url = requests.compat.urljoin(url, r.headers.get("location", ""))
                continue
            r.raise_for_status()
            is_pdf = "pdf" in r.headers.get("content-type", "").lower() or url.lower().split("?")[0].endswith(".pdf")
            if not is_pdf:
                return False, url
            size = 0
            with open(dest, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("file is larger than 50 MB")
                    f.write(chunk)
            return True, url
    raise ValueError("too many redirects")


def process(job_id):
    job = JOBS.get_item(Key={"job_id": job_id}).get("Item")
    if not job or job.get("status") not in ("queued", "running"):
        return
    t0 = time.time()
    set_job(job_id, status="running", started_at=common.now())
    work = tempfile.mkdtemp(dir="/tmp")
    try:
        pdf_path = os.path.join(work, "in.pdf")
        if job.get("source"):
            is_pdf, final_url = download_pdf(job["source"], pdf_path)
            src = pdf_path if is_pdf else final_url
        else:
            s3.download_file(BUCKET, f"inputs/{job_id}.pdf", pdf_path)
            src = pdf_path
        out_md = os.path.join(work, "out.md")
        els, report = pipeline.run(src, out_md, job["quality"])
        if src == pdf_path:
            with pdfplumber.open(pdf_path) as pdf:
                report["pages"] = len(pdf.pages)
        seconds = time.time() - t0
        # AI cost incl. the quality-check calls (ai.py tracks it); fall back to plain token cost
        ai_cost = report["ai_cost_usd"] if report.get("ai_cost_usd") else common.cost_usd(
            report.get("input_tokens", 0), report.get("output_tokens", 0))
        compute_cost = round(seconds * float(os.environ.get("COMPUTE_USD_PER_SECOND", "0.000049")), 6)
        # cloud witness tools are paid per page (list prices; set in the template)
        pages = int(report.get("pages", 1) or 1)
        tool_cost = 0.0
        for w in report.get("witnesses", []):
            if w["status"] == "ok" and w["tool"] in ("azure", "textract"):
                tool_cost += pages * float(os.environ.get(f"{w['tool'].upper()}_USD_PER_PAGE", "0"))
        report["tool_cost_usd"] = round(tool_cost, 6)
        total_len = sum(len(e["content_markdown"]) for e in els) or 1
        report["document_confidence"] = round(sum(e["confidence"] * len(e["content_markdown"]) for e in els) / total_len, 3) if els else 0
        report["low_confidence_elements"] = sum(1 for e in els if e["confidence"] < 0.8)
        report["ai_applied"] = bool(report.get("ai_calls", 0) + report.get("cache_hits", 0))
        report["ai_cost_usd"], report["compute_cost_usd"] = round(ai_cost, 6), compute_cost
        report["cost_usd"] = round(ai_cost + compute_cost + tool_cost, 6)
        report["source"] = job.get("source") or "uploaded.pdf"
        s3.upload_file(out_md, BUCKET, f"outputs/{job_id}.md")
        s3.upload_file(out_md[:-3] + ".json", BUCKET, f"outputs/{job_id}.json")
        # pictures: images/<name>.png next to the Markdown (its links point there), uploaded one by one for the web app,
        # and one zip with result.md + result.json + images/ for the customer (the links work after unzipping)
        img_dir = os.path.join(work, "images")
        names = sorted(os.listdir(img_dir)) if os.path.isdir(img_dir) else []
        for n in names:
            s3.upload_file(os.path.join(img_dir, n), BUCKET, f"outputs/{job_id}/images/{n}",
                           ExtraArgs={"ContentType": "image/png"})
        zip_path = os.path.join(work, "result.zip")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(out_md, "result.md")
            z.write(out_md[:-3] + ".json", "result.json")
            for n in names:
                z.write(os.path.join(img_dir, n), f"images/{n}")
        s3.upload_file(zip_path, BUCKET, f"outputs/{job_id}.zip", ExtraArgs={"ContentType": "application/zip"})
        report["images_saved"] = len(names)
        set_job(job_id, status="done", finished_at=common.now(), seconds=round(time.time() - t0, 2), report=report)
    except Exception as e:                      # mark failed instead of retrying forever
        set_job(job_id, status="failed", finished_at=common.now(), error=str(e)[:500])
    finally:
        shutil.rmtree(work, ignore_errors=True)


def handler(event, context):
    for rec in event["Records"]:
        process(json.loads(rec["body"])["job_id"])

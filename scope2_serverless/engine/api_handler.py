"""API Lambda (behind a Lambda Function URL). Routes:

  GET  /health
  POST /v1/jobs                {"quality": "standard|accurate", "url": "..."}  or  {"quality": ..., "filename": "x.pdf"}
  POST /v1/jobs/{id}/start     after you PUT the PDF to the presigned upload_url
  GET  /v1/jobs/{id}           status, timings, cost
  GET  /v1/jobs/{id}/result?format=markdown|json|zip presigned download link (zip = result.md + result.json + images/)
  GET  /v1/jobs/{id}/images                         presigned links of the figure images (images/<name>.png)
  GET  /v1/usage               jobs, pages, AI calls and cost for your key
  POST /v1/keys                {"email": "..."}  -> new API key (public, shown once)
  GET  /                       the web app (source and output side by side)
All other /v1 routes need the header  x-api-key.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import uuid
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

import common

ddb = boto3.resource("dynamodb")
from botocore.config import Config
# presigned links MUST use Signature Version 4: buckets created after June 2020 reject the old v2 signature
s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                  config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
sqs = boto3.client("sqs")
JOBS = ddb.Table(os.environ["JOBS_TABLE"])
KEYS = ddb.Table(os.environ["KEYS_TABLE"])
BUCKET = os.environ["BUCKET_NAME"]
QUEUE = os.environ["QUEUE_URL"]
MAX_BYTES = 50 * 1024 * 1024
QUALITIES = {"standard", "accurate"}


def reply(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, cls=common.Enc)}


def text_reply(code, text, ctype):
    return {"statusCode": code, "headers": {"Content-Type": ctype, "Cache-Control": "no-store"}, "body": text}


def create_key(body):
    """Self-service key: anyone can get one. Only the SHA-256 of the key is stored."""
    email = str(body.get("email", "")).strip().lower()
    if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}", email):
        raise ValueError('send {"email": "you@example.com"}')
    key = "ek_" + secrets.token_urlsafe(32)
    KEYS.put_item(Item={"key_hash": hashlib.sha256(key.encode()).hexdigest(), "email": email,
                        "active": True, "created_at": common.now()})
    return reply(201, {"api_key": key, "note": "Store this key now. It cannot be shown again. Send it as header x-api-key."})


def authenticate(headers):
    key = headers.get("x-api-key", "")
    if not key:
        return None
    h = hashlib.sha256(key.encode()).hexdigest()
    item = KEYS.get_item(Key={"key_hash": h}).get("Item")
    return h if item and item.get("active") else None


def parse_body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("body must be JSON")
    if not isinstance(data, dict):
        raise ValueError("body must be a JSON object")
    return data


def enqueue(job_id):
    sqs.send_message(QueueUrl=QUEUE, MessageBody=json.dumps({"job_id": job_id}))


def create_job(owner, body):
    quality = body.get("quality", "standard")
    if quality not in QUALITIES:
        raise ValueError("quality must be standard or accurate")
    url, filename = body.get("url"), body.get("filename")
    if bool(url) == bool(filename):
        raise ValueError("send exactly one of: url, filename")
    job_id = uuid.uuid4().hex[:12]
    item = {"job_id": job_id, "owner": owner, "quality": quality, "created_at": common.now()}
    if url:
        common.check_url(url)
        item.update(source=url, status="queued")
        JOBS.put_item(Item=item)
        enqueue(job_id)
        return reply(202, {"job_id": job_id, "status": "queued"})
    if not filename.lower().endswith(".pdf"):
        raise ValueError("only .pdf uploads are supported")
    item.update(status="awaiting_upload")
    JOBS.put_item(Item=item)
    upload_url = s3.generate_presigned_url(
        "put_object", ExpiresIn=600,
        Params={"Bucket": BUCKET, "Key": f"inputs/{job_id}.pdf", "ContentType": "application/pdf"})
    return reply(201, {"job_id": job_id, "status": "awaiting_upload", "upload_url": upload_url,
                       "upload_headers": {"Content-Type": "application/pdf"},
                       "next": f"PUT the file to upload_url, then POST /v1/jobs/{job_id}/start"})


def get_job(job_id, owner):
    item = JOBS.get_item(Key={"job_id": job_id}).get("Item")
    if not item or item.get("owner") != owner:
        return None
    return item


def start_job(job):
    if job["status"] != "awaiting_upload":
        raise ValueError(f"job is {job['status']}, nothing to start")
    try:
        head = s3.head_object(Bucket=BUCKET, Key=f"inputs/{job['job_id']}.pdf")
    except Exception:
        raise ValueError("file not uploaded yet")
    if head["ContentLength"] > MAX_BYTES:
        raise ValueError("file is larger than 50 MB")
    JOBS.update_item(Key={"job_id": job["job_id"]}, UpdateExpression="SET #s = :s",
                     ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "queued"})
    enqueue(job["job_id"])
    return reply(202, {"job_id": job["job_id"], "status": "queued"})


def job_view(job):
    keep = ("job_id", "status", "quality", "created_at", "started_at", "finished_at", "seconds", "report", "error")
    out = {k: job[k] for k in keep if k in job}
    if not job.get("source") and job["status"] != "awaiting_upload":      # uploaded PDF: link for the side-by-side viewer
        out["source_url"] = s3.generate_presigned_url(
            "get_object", ExpiresIn=3600,
            Params={"Bucket": BUCKET, "Key": f"inputs/{job['job_id']}.pdf",
                    "ResponseContentType": "application/pdf", "ResponseContentDisposition": "inline"})
    return out


def result_link(job, fmt, inline=False):
    if job["status"] != "done":
        return reply(409, {"error": f"job is {job['status']}"})
    ext = {"markdown": "md", "json": "json", "zip": "zip"}.get(fmt)
    if not ext:
        raise ValueError("format must be markdown, json or zip")
    if ext == "zip":
        url = s3.generate_presigned_url("get_object", ExpiresIn=600, Params={
            "Bucket": BUCKET, "Key": f"outputs/{job['job_id']}.zip",
            "ResponseContentDisposition": f"attachment; filename=extraction_{job['job_id']}.zip"})
        return reply(200, {"url": url, "expires_in": 600})
    if inline:                                   # the body itself (web app); results over 5 MB use the link
        obj = s3.get_object(Bucket=BUCKET, Key=f"outputs/{job['job_id']}.{ext}")
        if obj["ContentLength"] <= 5_000_000:
            return text_reply(200, obj["Body"].read().decode("utf-8"),
                              "application/json" if ext == "json" else "text/markdown; charset=utf-8")
    url = s3.generate_presigned_url(
        "get_object", ExpiresIn=600, Params={"Bucket": BUCKET, "Key": f"outputs/{job['job_id']}.{ext}"})
    return reply(200, {"url": url, "expires_in": 600})


def image_links(job):
    """{"images/page2_figure_1.png": presigned link, ...}: the names used in the Markdown links."""
    if job["status"] != "done":
        return reply(409, {"error": f"job is {job['status']}"})
    names = (job.get("report") or {}).get("images") or []        # saved by the worker; no bucket listing needed
    return reply(200, {"images": {"images/" + n: s3.generate_presigned_url(
        "get_object", ExpiresIn=3600, Params={"Bucket": BUCKET, "Key": f"outputs/{job['job_id']}/images/{n}"})
        for n in names}})


def usage(owner):
    try:
        items = JOBS.query(IndexName="OwnerIndex", KeyConditionExpression=Key("owner").eq(owner)).get("Items", [])
    except Exception:                       # role without access to the index: slower scan of the table instead
        from boto3.dynamodb.conditions import Attr
        items, kw = [], {"FilterExpression": Attr("owner").eq(owner)}
        while True:
            page = JOBS.scan(**kw)
            items += page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                break
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    total = {"jobs": len(items), "done": 0, "pages": 0, "ai_calls": 0, "cost_usd": 0.0}
    for it in items:
        if it.get("status") == "done":
            r = it.get("report", {})
            total["done"] += 1
            total["pages"] += int(r.get("pages", 0))
            total["ai_calls"] += int(r.get("ai_calls", 0))
            total["cost_usd"] += float(r.get("cost_usd", 0))
    total["cost_usd"] = round(total["cost_usd"], 5)
    recent = sorted(items, key=lambda i: i.get("created_at", 0), reverse=True)[:20]
    total["recent_jobs"] = [{"job_id": i["job_id"], "quality": i.get("quality"), "status": i.get("status"),
                             "pages": i.get("report", {}).get("pages"),
                             "cost_usd": i.get("report", {}).get("cost_usd")} for i in recent]
    return reply(200, total)


def handler(event, context):
    http = event["requestContext"]["http"]
    method, path = http["method"], event["rawPath"]
    if path == "/health":
        return reply(200, {"ok": True})
    if method == "GET" and path == "/":
        return text_reply(200, (Path(__file__).parent / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
    if method == "POST" and path == "/v1/keys":
        try:
            return create_key(parse_body(event))
        except ValueError as e:
            return reply(400, {"error": str(e)})
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    owner = authenticate(headers)
    if not owner:
        return reply(401, {"error": "missing or invalid x-api-key"})
    try:
        if method == "POST" and path == "/v1/jobs":
            return create_job(owner, parse_body(event))
        if method == "GET" and path == "/v1/usage":
            return usage(owner)
        m = re.fullmatch(r"/v1/jobs/([0-9a-f]{12})(/start|/result|/images)?", path)
        if m:
            job = get_job(m.group(1), owner)
            if not job:
                return reply(404, {"error": "job not found"})
            if m.group(2) == "/start" and method == "POST":
                return start_job(job)
            if m.group(2) == "/result" and method == "GET":
                qs = event.get("queryStringParameters") or {}
                return result_link(job, qs.get("format", "markdown"), qs.get("inline") == "1")
            if m.group(2) == "/images" and method == "GET":
                return image_links(job)
            if m.group(2) is None and method == "GET":
                return reply(200, job_view(job))
    except ValueError as e:
        return reply(400, {"error": str(e)})
    return reply(404, {"error": "not found"})

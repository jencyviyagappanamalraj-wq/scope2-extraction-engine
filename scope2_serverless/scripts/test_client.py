"""Step 3 of the 7 pm test: use the deployed API end to end.

    pip install requests
    python scripts/test_client.py <ApiUrl> <api key> path/to/file.pdf [standard|accurate]
    python scripts/test_client.py <ApiUrl> <api key> https://en.wikipedia.org/wiki/India [standard]
"""
import sys
import time

import requests

api, key, src = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
quality = sys.argv[4] if len(sys.argv) > 4 else "standard"
H = {"x-api-key": key}

if src.startswith("http"):
    job = requests.post(f"{api}/v1/jobs", headers=H, json={"url": src, "quality": quality}).json()
else:
    job = requests.post(f"{api}/v1/jobs", headers=H,
                        json={"filename": src.split("/")[-1].split("\\")[-1], "quality": quality}).json()
    print("created:", job)
    with open(src, "rb") as f:
        requests.put(job["upload_url"], data=f, headers=job["upload_headers"]).raise_for_status()
    job = requests.post(f"{api}/v1/jobs/{job['job_id']}/start", headers=H).json()
print("queued:", job)
jid = job["job_id"]
while True:
    st = requests.get(f"{api}/v1/jobs/{jid}", headers=H).json()
    print(" status:", st["status"])
    if st["status"] in ("done", "failed"):
        break
    time.sleep(3)
print(st)
if st["status"] == "done":
    link = requests.get(f"{api}/v1/jobs/{jid}/result", headers=H, params={"format": "markdown"}).json()["url"]
    print("\n--- first 800 characters of the result ---\n" + requests.get(link).text[:800])
print("\nusage:", requests.get(f"{api}/v1/usage", headers=H).json())

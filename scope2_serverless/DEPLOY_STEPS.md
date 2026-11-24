# Deploy step by step (serverless: API Gateway, Lambda, SQS, DynamoDB, S3, Bedrock)

Two places: your **Windows laptop** (PowerShell) and **AWS CloudShell** (a terminal in the AWS console, already
logged in as you). Total time about 20 minutes, mostly waiting.

## Part A: before you start

**Step 1: sign out and back in to AWS** (your trainer's instruction: your login only gets the new "pass the Lambda
role" permission after a fresh sign-in). Open the SSO portal → account → **Scope2TrainingDeveloper** →
Management console. Region at the top right: **United States (N. Virginia) us-east-1**.

**Step 2: have your Azure details ready** (from the `.env` of your Scope 1 "unstructured data pipeline" folder):
`AZURE_DI_ENDPOINT` (https://….cognitiveservices.azure.com/) and `AZURE_DI_KEY`. Keep them in Notepad; never
put them in a file inside this folder.

**Step 3: pack this folder** (PowerShell on your laptop):
```powershell
cd "C:\Users\win 10\scope2-extraction-engine\scope2_serverless"
tar --exclude=__pycache__ --exclude=reports -czf ..\scope2-serverless.tgz .
```

## Part B: deploy in CloudShell

**Step 4: open CloudShell** (the `>_` icon in the top bar). If an old `scope2-serverless.tgz` is there, remove it first:
`rm -f ~/scope2-serverless.tgz` (CloudShell will not overwrite a file on upload).

**Step 5: upload and unpack**: Actions → Upload file → `scope2-serverless.tgz`, then:
```bash
rm -rf ~/s2 && mkdir -p ~/s2 && cd ~/s2 && tar xzf ~/scope2-serverless.tgz && sed -i 's/\r$//' *.sh && ls
```

**Step 6: deploy (one command)**
```bash
bash deploy.sh
```
It asks for the Azure endpoint and then the key (hidden while you paste). It then:
1. finds your Claude Sonnet 4.6 model ID in Bedrock;
2. builds the two Lambda packages (the worker is about 184 MB, under Lambda's 250 MB limit);
3. creates the stack `scope2-serverless`: 2 Lambda functions (`scope2-api`, `scope2-worker`) using your trainer's
   role, the API Gateway, tables `scope2-service-roles-jobs` / `-keys`, bucket `scope2-service-roles-files-<account>`,
   queues `scope2-service-roles-jobs` / `-jobs-dlq`, 2 CloudWatch alarms. **No IAM roles are created.**
4. prints the **API URL** and a **test API key**. Copy both into Notepad.

If it says `could not find the model ID`: in the Bedrock console open Claude Sonnet 4.6, copy its inference
profile ID, run `export BEDROCK_MODEL_ID="<that ID>"`, then `bash deploy.sh` again.

If the deploy fails, it prints a table of the **exact errors**. If one says "not authorized" / "AccessDenied",
send that table to your trainer: it is exactly what she asked for. Then run `bash deploy.sh` again; it is safe to repeat.

## Part C: check it works

**Step 7: in the browser**: open the API URL. Click **Get a free key**, choose `Paper1.pdf`, pick **accurate**,
click **Extract**. Wait 1–3 minutes (accurate runs Azure, the AI repair, the AI check and the AI judge on every page).
You see the PDF on the left and the elements on the right with scores, badges, and the three cards at the top:
"How each tool read this document", "Completeness and review", "What the colours and badges mean".
Click any element to see the evidence behind its score.

**Step 8: logs** (CloudShell):
```bash
aws logs tail /aws/lambda/scope2-worker --since 30m
```
Each witness tool logs one line like `{"witness": "azure", "status": "ok", ...}`. `textract` may show `error`
with `AccessDeniedException`: the trainer's role does not include Textract. That is fine: the job continues
with the other tools and the report says so.

**Step 9: from your laptop** (PowerShell, `pip install requests` once):
```powershell
python scripts\test_client.py <API URL> <test key> ..\data\test_docs\Paper1.pdf accurate
python scripts\benchmark_api.py <API URL> <test key> ..\data\test_docs --repeat 1 --burst
```

## Part D: compare the tools on your laptop (with Docling)

Docling needs PyTorch and AI models (several GB), which do not fit a normal Lambda. On your laptop it runs
(you used it in Scope 1). This gives the full comparison of all five tools plus our engine:
```powershell
cd "C:\Users\win 10\scope2-extraction-engine\scope2_serverless"
pip install pdfplumber pypdf "markitdown[pdf]" docling boto3 requests beautifulsoup4
$env:AI_PROVIDER="bedrock"; $env:BEDROCK_MODEL_ID="<your Sonnet 4.6 ID>"; $env:BEDROCK_REGION="us-east-1"
$env:AZURE_DI_ENDPOINT="<endpoint>"; $env:AZURE_DI_KEY="<key>"
$env:WITNESSES="pypdf,markitdown,docling,azure,textract"
python scripts\compare_tools.py ..\data\test_docs --level accurate
```
Open `reports\tool_comparison.md`. (Optional, cloud Docling: `WITH_DOCLING=1 bash deploy.sh` builds a container
worker. It needs Docker and permission to create the ECR repository `scope2-service-roles-worker`, and a few GB of
disk; CloudShell may be too small. If it fails, keep Docling for the laptop comparison.)

## Part E: business case
```powershell
python engine\business_case.py ..\data\test_docs --all-ai-pages 2 --server api_benchmark.csv
```

## Part F: tear down (one command)
```bash
cd ~/s2 && bash destroy.sh
```
Type `yes`. It empties the buckets, deletes the stack, the deployment bucket and (if created) the ECR repository.

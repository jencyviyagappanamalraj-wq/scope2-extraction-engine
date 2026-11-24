#!/usr/bin/env bash
# ONE COMMAND to deploy (or update) the serverless extraction service. Run in AWS CloudShell, region us-east-1,
# after signing out and in again (so your login can pass the Lambda role).
#
#   bash deploy.sh                       # asks for the Azure endpoint/key (Enter = skip / keep current)
#   WITH_DOCLING=1 bash deploy.sh        # OPTIONAL: container worker that also runs Docling (needs Docker + ECR)
#
# Optional settings: BEDROCK_MODEL_ID (found automatically), STACK (scope2-serverless), WORKER_MEMORY (3008),
#                    LAMBDA_ROLE_ARN (the trainer's role), AZURE_CHUNK_PAGES (2 = free tier, 0 = paid)
# Creates NO IAM resources. Uses the existing role arn:aws:iam::<account>:role/scope2-service-lambda-exec.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PAGER=""
REGION=${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}
export AWS_DEFAULT_REGION="$REGION"
STACK=${STACK:-scope2-serverless}
say() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

say "Account"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text) || die "not logged in (sign in again / aws sso login)"
ROLE=${LAMBDA_ROLE_ARN:-arn:aws:iam::${ACCOUNT}:role/scope2-service-lambda-exec}
DEPLOY_BUCKET="scope2-service-roles-deploy-${ACCOUNT}"
echo "account $ACCOUNT, region $REGION, stack $STACK"
echo "Lambda role: $ROLE"

say "Bedrock model (Claude Sonnet 4.6)"
if [ -z "${BEDROCK_MODEL_ID:-}" ]; then
  IDS=$(aws bedrock list-inference-profiles --query "inferenceProfileSummaries[?contains(inferenceProfileId,'sonnet-4-6')].inferenceProfileId" --output text 2>/dev/null || true)
  for pref in us. global.; do
    for id in $IDS; do case "$id" in "$pref"*) BEDROCK_MODEL_ID=$id; break 2;; esac; done
  done
fi
[ -n "${BEDROCK_MODEL_ID:-}" ] || die 'could not find the model ID. Set it: export BEDROCK_MODEL_ID="<ID of Claude Sonnet 4.6 from the Bedrock console>"'
case "$BEDROCK_MODEL_ID" in us.*) PIN=0.0033; POUT=0.0165;; *) PIN=0.003; POUT=0.015;; esac
echo "model $BEDROCK_MODEL_ID (price per 1K tokens: in $PIN, out $POUT)"

say "Azure Document Intelligence (witness tool for the accurate level)"
AZ_PARAMS=()
if [ -z "${AZURE_DI_ENDPOINT:-}" ] && [ -t 0 ]; then
  read -r -p "Azure endpoint (https://<name>.cognitiveservices.azure.com/), or Enter to skip/keep: " AZURE_DI_ENDPOINT || true
fi
if [ -n "${AZURE_DI_ENDPOINT:-}" ]; then
  if [ -z "${AZURE_DI_KEY:-}" ] && [ -t 0 ]; then read -rs -p "Azure key (hidden): " AZURE_DI_KEY; echo; fi
  AZ_PARAMS=(AzureEndpoint="$AZURE_DI_ENDPOINT" AzureKey="${AZURE_DI_KEY:-}" AzureChunkPages="${AZURE_CHUNK_PAGES:-2}")
  echo "Azure will be used (endpoint $AZURE_DI_ENDPOINT)"
else
  echo "Azure skipped (or the value from the previous deploy is kept)"
fi

say "Stack state"
STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo NONE)
echo "$STATUS"
if [ "$STATUS" = "ROLLBACK_COMPLETE" ] || [ "$STATUS" = "ROLLBACK_FAILED" ]; then
  echo "A previous first deploy failed; deleting its empty stack first"
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
fi

say "Deployment bucket for the code ($DEPLOY_BUCKET)"
if ! aws s3api head-bucket --bucket "$DEPLOY_BUCKET" 2>/dev/null; then
  aws s3 mb "s3://$DEPLOY_BUCKET" >/dev/null
  aws s3api put-public-access-block --bucket "$DEPLOY_BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

say "Building the Lambda packages (in /tmp: the worker is ~184 MB)"
B=/tmp/scope2-build
rm -rf "$B" && mkdir -p "$B/build/api" "$B/build/worker"
cp engine/api_handler.py engine/common.py engine/index.html "$B/build/api/"
python3 -m pip install -q --target "$B/build/worker" --implementation cp --python-version 3.13 --only-binary=:all: \
  --platform manylinux2014_x86_64 --platform manylinux_2_28_x86_64 --platform manylinux_2_34_x86_64 \
  -r requirements-worker.txt
rm -rf "$B/build/worker/bin"
find "$B/build/worker" -name "__pycache__" -prune -exec rm -rf {} +
cp engine/*.py engine/index.html "$B/build/worker/"
cp template.yaml "$B/"
echo "worker package: $(du -sh "$B/build/worker" | cut -f1) (limit 250 MB)"

DOCLING_PARAMS=(WithDocling=false)
if [ "${WITH_DOCLING:-0}" = "1" ]; then
  say "Docling container worker (optional)"
  REPO=scope2-service-roles-worker
  aws ecr describe-repositories --repository-names "$REPO" >/dev/null 2>&1 || \
    aws ecr create-repository --repository-name "$REPO" --image-scanning-configuration scanOnPush=true >/dev/null
  URI="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com/${REPO}:$(date +%Y%m%d%H%M%S)"
  aws ecr get-login-password | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"
  docker build --platform linux/amd64 --provenance=false -f Dockerfile.docling -t "$URI" .
  docker push "$URI"
  DOCLING_PARAMS=(WithDocling=true WorkerImageUri="$URI" "WitnessesStandard=pypdf,markitdown,docling" "WitnessesAccurate=pypdf,markitdown,docling,azure,textract")
fi

say "CloudFormation (about 3 minutes)"
cd "$B"
aws cloudformation package --template-file template.yaml --s3-bucket "$DEPLOY_BUCKET" --output-template-file packaged.yaml >/dev/null
if ! aws cloudformation deploy --stack-name "$STACK" --template-file packaged.yaml --no-fail-on-empty-changeset \
     --capabilities CAPABILITY_AUTO_EXPAND --tags Project=scope2 \
     --parameter-overrides LambdaRoleArn="$ROLE" BedrockModelId="$BEDROCK_MODEL_ID" BedrockRegion="$REGION" \
       PriceInPer1K="$PIN" PriceOutPer1K="$POUT" WorkerMemory="${WORKER_MEMORY:-3008}" \
       ${AZ_PARAMS[@]+"${AZ_PARAMS[@]}"} "${DOCLING_PARAMS[@]}"; then
  echo; echo "Deploy failed. The exact errors (send these to your trainer if a permission is denied):"
  aws cloudformation describe-stack-events --stack-name "$STACK" --max-items 40 \
    --query "StackEvents[?contains(ResourceStatus,'FAILED')].[LogicalResourceId,ResourceStatusReason]" --output table
  exit 1
fi
API=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='ApiUrl'].OutputValue" --output text)

say "Checking the API"
for _ in $(seq 1 10); do curl -fsS "$API/health" >/dev/null 2>&1 && break; sleep 3; done
curl -fsS "$API/health" >/dev/null || die "API did not answer at $API/health"
KEY=$(curl -fsS -X POST "$API/v1/keys" -H "content-type: application/json" -d '{"email":"deploy-test@example.com"}' \
      | python3 -c "import sys,json;print(json.load(sys.stdin)['api_key'])")

cat <<EOF

========================================================================
 DEPLOYED
 API base URL / web app : $API
 A test API key         : $KEY      (header x-api-key)
 Try it                 : python scripts/test_client.py $API $KEY data/test_docs/Paper1.pdf accurate
 Logs                   : aws logs tail /aws/lambda/scope2-worker --follow
 Tear everything down   : bash destroy.sh
========================================================================
EOF

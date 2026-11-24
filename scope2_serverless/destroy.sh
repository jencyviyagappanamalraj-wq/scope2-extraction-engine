#!/usr/bin/env bash
# ONE COMMAND to remove everything deploy.sh created (files bucket is emptied first: S3 cannot delete a full bucket).
#   bash destroy.sh          (asks for confirmation)      bash destroy.sh --yes
set -euo pipefail
export AWS_PAGER=""
export AWS_DEFAULT_REGION=${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}
STACK=${STACK:-scope2-serverless}
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
if [ "${1:-}" != "--yes" ]; then
  read -r -p "Delete stack $STACK and ALL its data (jobs, keys, files)? Type yes: " a
  [ "$a" = "yes" ] || { echo "Cancelled."; exit 1; }
fi
for b in "scope2-service-roles-files-${ACCOUNT}" "scope2-service-roles-deploy-${ACCOUNT}"; do
  if aws s3api head-bucket --bucket "$b" 2>/dev/null; then echo "emptying s3://$b"; aws s3 rm "s3://$b" --recursive --quiet; fi
done
if aws cloudformation describe-stacks --stack-name "$STACK" >/dev/null 2>&1; then
  echo "deleting stack $STACK (about 2 minutes)"
  aws cloudformation delete-stack --stack-name "$STACK"
  aws cloudformation wait stack-delete-complete --stack-name "$STACK"
fi
aws s3api head-bucket --bucket "scope2-service-roles-deploy-${ACCOUNT}" 2>/dev/null && aws s3 rb "s3://scope2-service-roles-deploy-${ACCOUNT}"
aws ecr delete-repository --repository-name scope2-service-roles-worker --force >/dev/null 2>&1 || true
echo "Done. Nothing from this project is left in the account."

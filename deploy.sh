#!/usr/bin/env bash
# Usage: ./deploy.sh [stack-name]   (needs AWS CLI, SAM CLI, Docker)
set -euo pipefail
STACK=${1:-find-my-photos}

sam build --use-container
sam deploy --stack-name "$STACK" --resolve-s3 --capabilities CAPABILITY_IAM \
  --no-confirm-changeset --no-fail-on-empty-changeset

out() { aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

echo "window.CONFIG = { API_URL: \"$(out ApiUrl)\" };" > frontend/config.js
aws s3 sync frontend/ "s3://$(out WebBucketName)/" --delete
echo "Live at: $(out SiteUrl)"

#!/usr/bin/env bash
# Deletes everything, including the Rekognition collection (all stored face data).
set -euo pipefail
STACK=${1:-find-my-photos}
out() { aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
for k in PhotosBucketName WebBucketName; do aws s3 rm "s3://$(out $k)" --recursive; done
sam delete --stack-name "$STACK" --no-prompts

#!/usr/bin/env bash
# upload-code-to-s3.sh — Package and upload batch job code to S3
#
# This is used instead of ECR when Docker push is not available.
# The Batch job will pull the code from S3 at runtime using a public Python image.
#
# Usage:
#   AWS_PROFILE=mon-sandbox-admin bash scripts/upload-code-to-s3.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SERVICE_NAME="sre-aws-batch-telemetry"
AWS_REGION="${AWS_REGION:-us-west-2}"
S3_BUCKET="${S3_BUCKET:-sre-batch-telemetry-code-723346695882}"

echo "==> Ensuring S3 bucket '${S3_BUCKET}' exists…"
aws s3 mb "s3://${S3_BUCKET}" --region "${AWS_REGION}" 2>/dev/null || true

echo "==> Uploading batch job code to S3…"
aws s3 cp "${REPO_ROOT}/src/batch_job.py" "s3://${S3_BUCKET}/code/batch_job.py" --region "${AWS_REGION}"
aws s3 cp "${REPO_ROOT}/docker/requirements.txt" "s3://${S3_BUCKET}/code/requirements.txt" --region "${AWS_REGION}"

echo ""
echo "✅ Code uploaded to S3:"
echo "   s3://${S3_BUCKET}/code/batch_job.py"
echo "   s3://${S3_BUCKET}/code/requirements.txt"
echo ""
echo "   The Batch job will download these at runtime using the public python:3.11-slim image."

#!/usr/bin/env bash
# submit-job.sh — Submit a test AWS Batch job
#
# Usage:
#   bash scripts/submit-job.sh [--env dev] [--region us-east-1]
#
# Reads job queue and job definition names from CloudFormation stack outputs.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SERVICE_NAME="sre-aws-batch-telemetry"
ENV="${ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
JOB_NAME="${SERVICE_NAME}-test-$(date +%Y%m%d%H%M%S)"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)    ENV="$2";        shift 2 ;;
    --region) AWS_REGION="$2"; shift 2 ;;
    --name)   JOB_NAME="$2";   shift 2 ;;
    *)        echo "Unknown option: $1"; exit 1 ;;
  esac
done

STACK_NAME="${SERVICE_NAME}-${ENV}"

# ---------------------------------------------------------------------------
# Resolve queue and job definition from CloudFormation outputs
# ---------------------------------------------------------------------------
echo "==> Reading CloudFormation outputs from stack '${STACK_NAME}'…"

get_output() {
  local key="$1"
  aws cloudformation describe-stacks \
      --stack-name "${STACK_NAME}" \
      --region "${AWS_REGION}" \
      --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
      --output text
}

JOB_QUEUE="$(get_output JobQueueName)"
JOB_DEFINITION="$(get_output JobDefinitionName)"

if [[ -z "${JOB_QUEUE}" || -z "${JOB_DEFINITION}" ]]; then
  echo "ERROR: Could not resolve job queue or job definition from stack outputs." >&2
  echo "       Make sure the stack '${STACK_NAME}' is deployed." >&2
  exit 1
fi

echo "    Job Queue      : ${JOB_QUEUE}"
echo "    Job Definition : ${JOB_DEFINITION}"
echo "    Job Name       : ${JOB_NAME}"
echo ""

# ---------------------------------------------------------------------------
# Submit the job
# ---------------------------------------------------------------------------
echo "==> Submitting Batch job…"
JOB_ID=$(aws batch submit-job \
    --job-name "${JOB_NAME}" \
    --job-queue "${JOB_QUEUE}" \
    --job-definition "${JOB_DEFINITION}" \
    --region "${AWS_REGION}" \
    --query "jobId" \
    --output text)

echo ""
echo "✅ Job submitted successfully!"
echo "   Job ID   : ${JOB_ID}"
echo "   Job Name : ${JOB_NAME}"
echo ""
echo "==> Monitor job status:"
echo "   aws batch describe-jobs --jobs ${JOB_ID} --region ${AWS_REGION}"
echo ""
echo "==> View CloudWatch Logs:"
echo "   aws logs tail /aws/batch/${SERVICE_NAME} --follow --region ${AWS_REGION}"

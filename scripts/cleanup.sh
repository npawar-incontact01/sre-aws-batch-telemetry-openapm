#!/usr/bin/env bash
# cleanup.sh — Tear down all sre-aws-batch-telemetry resources
#
# Usage:
#   bash scripts/cleanup.sh [--env dev] [--region us-east-1]
#
# WARNING: This will delete the CloudFormation stack AND all ECR images.
# Use with caution in non-dev environments.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SERVICE_NAME="sre-aws-batch-telemetry"
ENV="${ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
SKIP_ECR="${SKIP_ECR:-false}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)       ENV="$2";        shift 2 ;;
    --region)    AWS_REGION="$2"; shift 2 ;;
    --skip-ecr)  SKIP_ECR="true"; shift   ;;
    *)           echo "Unknown option: $1"; exit 1 ;;
  esac
done

STACK_NAME="${SERVICE_NAME}-${ENV}"

echo "========================================="
echo " WARNING: This will DELETE:"
echo "   - CloudFormation stack : ${STACK_NAME}"
[[ "${SKIP_ECR}" == "false" ]] && \
echo "   - ECR repository       : ${SERVICE_NAME} (ALL images)"
echo " Region: ${AWS_REGION}"
echo "========================================="
read -rp "Type 'yes' to confirm: " CONFIRM
[[ "${CONFIRM}" != "yes" ]] && { echo "Aborted."; exit 0; }

# ---------------------------------------------------------------------------
# Delete CloudFormation stack
# ---------------------------------------------------------------------------
echo ""
echo "==> Deleting CloudFormation stack '${STACK_NAME}'…"
aws cloudformation delete-stack \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}"

echo "    Waiting for stack deletion to complete…"
aws cloudformation wait stack-delete-complete \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}"
echo "    Stack deleted."

# ---------------------------------------------------------------------------
# Delete ECR images and repository
# ---------------------------------------------------------------------------
if [[ "${SKIP_ECR}" == "false" ]]; then
  echo ""
  echo "==> Deleting ECR repository '${SERVICE_NAME}'…"
  IMAGE_IDS=$(aws ecr list-images \
      --repository-name "${SERVICE_NAME}" \
      --region "${AWS_REGION}" \
      --query "imageIds[*]" \
      --output json 2>/dev/null || echo "[]")

  if [[ "${IMAGE_IDS}" != "[]" && -n "${IMAGE_IDS}" ]]; then
    aws ecr batch-delete-image \
        --repository-name "${SERVICE_NAME}" \
        --image-ids "${IMAGE_IDS}" \
        --region "${AWS_REGION}" \
        --output text
    echo "    Images deleted."
  fi

  aws ecr delete-repository \
      --repository-name "${SERVICE_NAME}" \
      --region "${AWS_REGION}" \
      --force \
      --output text || echo "    ECR repository not found (already deleted)."
  echo "    ECR repository deleted."
fi

echo ""
echo "✅ Cleanup complete."

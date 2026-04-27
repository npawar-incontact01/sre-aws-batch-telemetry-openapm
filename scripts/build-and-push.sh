#!/usr/bin/env bash
# build-and-push.sh — Build Docker image and push to Amazon ECR
#
# Usage:
#   bash scripts/build-and-push.sh [--tag <image-tag>] [--region <aws-region>]
#
# Environment variables (or AWS CLI defaults):
#   AWS_ACCOUNT_ID   — AWS account ID (default: resolved via STS)
#   AWS_REGION       — AWS region (default: us-east-1)
#   AWS_PROFILE      — AWS CLI named profile (optional)

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_NAME="sre-aws-batch-telemetry"
IMAGE_TAG="latest"
AWS_REGION="${AWS_REGION:-us-east-1}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)    IMAGE_TAG="$2";      shift 2 ;;
    --region) AWS_REGION="$2";     shift 2 ;;
    *)        echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve AWS account ID
# ---------------------------------------------------------------------------
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_REPO="${ECR_REGISTRY}/${SERVICE_NAME}"
IMAGE_URI="${ECR_REPO}:${IMAGE_TAG}"

echo "==> Building image: ${IMAGE_URI}"
echo "    Account : ${AWS_ACCOUNT_ID}"
echo "    Region  : ${AWS_REGION}"
echo ""

# ---------------------------------------------------------------------------
# Create ECR repository if it doesn't exist
# ---------------------------------------------------------------------------
echo "==> Ensuring ECR repository '${SERVICE_NAME}' exists…"
aws ecr describe-repositories \
    --repository-names "${SERVICE_NAME}" \
    --region "${AWS_REGION}" \
    --query "repositories[0].repositoryUri" \
    --output text 2>/dev/null || \
aws ecr create-repository \
    --repository-name "${SERVICE_NAME}" \
    --region "${AWS_REGION}" \
    --image-scanning-configuration scanOnPush=true \
    --tags Key=Service,Value="${SERVICE_NAME}" Key=ManagedBy,Value=scripts/build-and-push.sh \
    --output text --query "repository.repositoryUri"
echo "    ECR repository ready."

# ---------------------------------------------------------------------------
# Authenticate Docker to ECR
# ---------------------------------------------------------------------------
echo "==> Authenticating Docker to ECR…"
aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${ECR_REGISTRY}"

# ---------------------------------------------------------------------------
# Build Docker image
# ---------------------------------------------------------------------------
echo "==> Building Docker image…"
docker build \
    --file "${REPO_ROOT}/docker/Dockerfile" \
    --tag "${IMAGE_URI}" \
    --tag "${ECR_REPO}:latest" \
    "${REPO_ROOT}"

# ---------------------------------------------------------------------------
# Push to ECR
# ---------------------------------------------------------------------------
echo "==> Pushing image to ECR…"
docker push "${IMAGE_URI}"
[[ "${IMAGE_TAG}" != "latest" ]] && docker push "${ECR_REPO}:latest"

echo ""
echo "✅ Successfully pushed: ${IMAGE_URI}"
echo "   Use this image URI in CloudFormation:"
echo "   ContainerImage=${IMAGE_URI}"

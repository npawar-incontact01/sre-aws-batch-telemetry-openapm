#!/usr/bin/env bash
# deploy.sh — Deploy sre-aws-batch-telemetry CloudFormation stacks
#
# Usage:
#   bash scripts/deploy.sh \
#     --env dev \
#     --vpc-id vpc-xxxxxxxx \
#     --subnet-ids "subnet-aaa,subnet-bbb" \
#     --container-image "300813158921.dkr.ecr.us-east-1.amazonaws.com/sre-aws-batch-telemetry:latest" \
#     --templates-bucket my-cfn-templates-bucket
#
# All parameters can also be set as environment variables:
#   ENV, VPC_ID, SUBNET_IDS, CONTAINER_IMAGE, TEMPLATES_BUCKET

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
SERVICE_NAME="sre-aws-batch-telemetry"
ENV="${ENV:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
VPC_ID="${VPC_ID:-}"
SUBNET_IDS="${SUBNET_IDS:-}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:-}"
TEMPLATES_BUCKET="${TEMPLATES_BUCKET:-}"
OTEL_ENDPOINT="${OTEL_ENDPOINT:-https://apm-na1.service.nicecxone-dev.com:4317}"
OTEL_RESOURCE_ATTRS="${OTEL_RESOURCE_ATTRS:-environment=ic-dev,account.id=300813158921}"
TEAM="${TEAM:-sre}"
COST_CENTER="${COST_CENTER:-0000}"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)               ENV="$2";               shift 2 ;;
    --vpc-id)            VPC_ID="$2";            shift 2 ;;
    --subnet-ids)        SUBNET_IDS="$2";        shift 2 ;;
    --container-image)   CONTAINER_IMAGE="$2";   shift 2 ;;
    --templates-bucket)  TEMPLATES_BUCKET="$2";  shift 2 ;;
    --region)            AWS_REGION="$2";        shift 2 ;;
    --team)              TEAM="$2";              shift 2 ;;
    --cost-center)       COST_CENTER="$2";       shift 2 ;;
    *)                   echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
ERRORS=0
for var_name in VPC_ID SUBNET_IDS CONTAINER_IMAGE TEMPLATES_BUCKET; do
  val="${!var_name}"
  if [[ -z "${val}" ]]; then
    echo "ERROR: --${var_name//_/-} (or \$${var_name}) is required" >&2
    ERRORS=$((ERRORS + 1))
  fi
done
[[ ${ERRORS} -gt 0 ]] && exit 1

STACK_NAME="${SERVICE_NAME}-${ENV}"

echo "==> Uploading CloudFormation templates to S3…"
aws s3 sync "${REPO_ROOT}/cloudformation/" \
    "s3://${TEMPLATES_BUCKET}/cloudformation/" \
    --region "${AWS_REGION}" \
    --exclude "*" --include "*.yaml"

echo "==> Deploying master stack: ${STACK_NAME}…"
aws cloudformation deploy \
    --stack-name "${STACK_NAME}" \
    --template-file "${REPO_ROOT}/cloudformation/master-stack.yaml" \
    --parameter-overrides \
        "EnvironmentName=${ENV}" \
        "ServiceName=${SERVICE_NAME}" \
        "VpcId=${VPC_ID}" \
        "SubnetIds=${SUBNET_IDS}" \
        "ContainerImage=${CONTAINER_IMAGE}" \
        "OtelEndpoint=${OTEL_ENDPOINT}" \
        "OtelResourceAttributes=${OTEL_RESOURCE_ATTRS}" \
        "TemplatesBucketName=${TEMPLATES_BUCKET}" \
        "Team=${TEAM}" \
        "CostCenter=${COST_CENTER}" \
    --capabilities CAPABILITY_NAMED_IAM \
    --region "${AWS_REGION}" \
    --no-fail-on-empty-changeset

echo ""
echo "✅ Deployment complete for stack: ${STACK_NAME}"
echo ""
echo "==> Stack outputs:"
aws cloudformation describe-stacks \
    --stack-name "${STACK_NAME}" \
    --region "${AWS_REGION}" \
    --query "Stacks[0].Outputs" \
    --output table

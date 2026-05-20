# Deploying AWS Batch Telemetry to mon-sandbox (723346695882) via CLI

> No CI/CD pipeline — all steps run from your local machine using AWS CLI.

---

## Prerequisites Checklist

- [ ] AWS CLI v2 installed (`aws --version`)
- [ ] Docker 20+ running (`docker info`)
- [ ] AWS profile configured for mon-sandbox account (723346695882)
- [ ] Sufficient IAM permissions (Batch, ECR, IAM, CloudFormation, S3, VPC read)

---

## PHASE 0: Configure AWS CLI Profile for mon-sandbox

```bash
# Set your profile and region
export AWS_PROFILE=mon-sandbox
export AWS_REGION=us-west-2
export AWS_ACCOUNT_ID=723346695882

# Verify you're in the right account
aws sts get-caller-identity
# Should show "Account": "723346695882"
```

---

## PHASE 1: VPC and Subnet IDs (Already Identified)

| Resource | Value | Details |
|---|---|---|
| VPC | `vpc-0693b34275513631c` | shared_eks (10.0.0.0/21) |
| Private Subnet (us-west-2a) | `subnet-0cdc843ec821d3ea5` | 10.0.0.0/23 |
| Private Subnet (us-west-2b) | `subnet-071fb611b8b3abf42` | 10.0.2.0/23 |
| Private Subnet (us-west-2c) | `subnet-00c9a34807a6d3742` | 10.0.4.0/23 |

```bash
export VPC_ID="vpc-0693b34275513631c"
export SUBNET_IDS="subnet-0cdc843ec821d3ea5,subnet-071fb611b8b3abf42,subnet-00c9a34807a6d3742"
```

---

## PHASE 2: Build & Push Docker Image to ECR

```bash
cd /path/to/sre-aws-batch-telemetry-openapm

# Set variables
export AWS_PROFILE=mon-sandbox
export AWS_REGION=us-west-2
export AWS_ACCOUNT_ID=723346695882

# Run the build-and-push script
bash scripts/build-and-push.sh --tag latest --region us-west-2

# This will:
# 1. Create ECR repo "sre-aws-batch-telemetry" if it doesn't exist
# 2. Build the Docker image from docker/Dockerfile
# 3. Push to 723346695882.dkr.ecr.us-west-2.amazonaws.com/sre-aws-batch-telemetry:latest
```

**Note the output image URI** — it will look like:
```
723346695882.dkr.ecr.us-west-2.amazonaws.com/sre-aws-batch-telemetry:latest
```

Save it:
```bash
export CONTAINER_IMAGE="723346695882.dkr.ecr.us-west-2.amazonaws.com/sre-aws-batch-telemetry:latest"
```

---

## PHASE 3: Deploy CloudFormation Stacks

You have two options: **Option A** (automated via deploy.sh) or **Option B** (manual stack-by-stack).

### Option A: Automated Deploy via deploy.sh (Recommended)

This uses the nested master stack — all 5 sub-stacks in one shot.

```bash
# Step 1: Create an S3 bucket for CloudFormation templates
export TEMPLATES_BUCKET="sre-batch-telemetry-cfn-templates-723346695882"

aws s3 mb "s3://${TEMPLATES_BUCKET}" --region us-west-2 2>/dev/null || true

# Step 2: Run the deploy script
bash scripts/deploy.sh \
    --env dev \
    --vpc-id "${VPC_ID}" \
    --subnet-ids "${SUBNET_IDS}" \
    --container-image "${CONTAINER_IMAGE}" \
    --templates-bucket "${TEMPLATES_BUCKET}" \
    --region us-west-2 \
    --team sre \
    --cost-center 0000
```

**Important env vars to customize** (edit in deploy.sh or pass as env vars):

```bash
# Change these to match mon-sandbox context
export OTEL_ENDPOINT="https://apm-na1.service.nicecxone-dev.com:4317"
export OTEL_RESOURCE_ATTRS="environment=mon-sandbox,account.id=723346695882"
```

### Option B: Manual Stack-by-Stack Deploy (If nested stacks give trouble)

Deploy each CloudFormation template individually in order:

```bash
SERVICE_NAME="sre-aws-batch-telemetry"
ENV="dev"

# ── Stack 1: IAM Roles ──
aws cloudformation deploy \
    --stack-name "${SERVICE_NAME}-${ENV}-iam" \
    --template-file cloudformation/iam-roles.yaml \
    --parameter-overrides \
        EnvironmentName=${ENV} \
        ServiceName=${SERVICE_NAME} \
        Team=sre \
        CostCenter=0000 \
    --capabilities CAPABILITY_NAMED_IAM \
    --region us-west-2

# Get the role ARNs
EXEC_ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name "${SERVICE_NAME}-${ENV}-iam" \
    --query "Stacks[0].Outputs[?OutputKey=='BatchExecutionRoleArn'].OutputValue" \
    --output text --region us-west-2)

JOB_ROLE_ARN=$(aws cloudformation describe-stacks \
    --stack-name "${SERVICE_NAME}-${ENV}-iam" \
    --query "Stacks[0].Outputs[?OutputKey=='BatchJobRoleArn'].OutputValue" \
    --output text --region us-west-2)

echo "Execution Role: ${EXEC_ROLE_ARN}"
echo "Job Role: ${JOB_ROLE_ARN}"

# ── Stack 2: Security Groups ──
aws cloudformation deploy \
    --stack-name "${SERVICE_NAME}-${ENV}-sg" \
    --template-file cloudformation/security-groups.yaml \
    --parameter-overrides \
        EnvironmentName=${ENV} \
        ServiceName=${SERVICE_NAME} \
        VpcId=${VPC_ID} \
        OtelEndpointCidr=0.0.0.0/0 \
        Team=sre \
        CostCenter=0000 \
    --region us-west-2

# Get the SG ID
SG_ID=$(aws cloudformation describe-stacks \
    --stack-name "${SERVICE_NAME}-${ENV}-sg" \
    --query "Stacks[0].Outputs[?OutputKey=='BatchTaskSecurityGroupId'].OutputValue" \
    --output text --region us-west-2)

echo "Security Group: ${SG_ID}"

# ── Stack 3: Compute Environment ──
aws cloudformation deploy \
    --stack-name "${SERVICE_NAME}-${ENV}-ce" \
    --template-file cloudformation/batch-compute-environment.yaml \
    --parameter-overrides \
        EnvironmentName=${ENV} \
        ServiceName=${SERVICE_NAME} \
        SubnetIds=${SUBNET_IDS} \
        SecurityGroupId=${SG_ID} \
        MaxvCpus=16 \
        Team=sre \
        CostCenter=0000 \
    --region us-west-2

# Get the CE ARN
CE_ARN=$(aws cloudformation describe-stacks \
    --stack-name "${SERVICE_NAME}-${ENV}-ce" \
    --query "Stacks[0].Outputs[?OutputKey=='ComputeEnvironmentArn'].OutputValue" \
    --output text --region us-west-2)

echo "Compute Environment: ${CE_ARN}"

# ── Stack 4: Job Queue ──
aws cloudformation deploy \
    --stack-name "${SERVICE_NAME}-${ENV}-queue" \
    --template-file cloudformation/batch-job-queue.yaml \
    --parameter-overrides \
        EnvironmentName=${ENV} \
        ServiceName=${SERVICE_NAME} \
        ComputeEnvironmentArn=${CE_ARN} \
        Team=sre \
        CostCenter=0000 \
    --region us-west-2

# ── Stack 5: Job Definition + Log Group ──
aws cloudformation deploy \
    --stack-name "${SERVICE_NAME}-${ENV}-jobdef" \
    --template-file cloudformation/batch-job-definition.yaml \
    --parameter-overrides \
        EnvironmentName=${ENV} \
        ServiceName=${SERVICE_NAME} \
        ContainerImage=${CONTAINER_IMAGE} \
        ExecutionRoleArn=${EXEC_ROLE_ARN} \
        JobRoleArn=${JOB_ROLE_ARN} \
        OtelEndpoint="https://apm-na1.service.nicecxone-dev.com:4317" \
        OtelResourceAttributes="environment=mon-sandbox,account.id=723346695882" \
        JobVcpu=0.25 \
        JobMemory=512 \
        Team=sre \
        CostCenter=0000 \
    --region us-west-2
```

---

## PHASE 4: Submit a Test Job

```bash
# If you used Option A (master stack):
bash scripts/submit-job.sh --env dev --region us-west-2

# If you used Option B (individual stacks), submit manually:
aws batch submit-job \
    --job-name "sre-batch-telemetry-test-$(date +%Y%m%d%H%M%S)" \
    --job-queue "sre-aws-batch-telemetry-dev-queue" \
    --job-definition "sre-aws-batch-telemetry-dev-job" \
    --region us-west-2

# Save the Job ID from the output
```

---

## PHASE 5: Monitor & Verify

### Check Job Status

```bash
# Replace JOB_ID with the actual ID from submit output
aws batch describe-jobs \
    --jobs <JOB_ID> \
    --region us-west-2 \
    --query "jobs[0].{Status:status, StatusReason:statusReason, StartedAt:startedAt, StoppedAt:stoppedAt}"
```

Job will transition through: `SUBMITTED → PENDING → RUNNABLE → STARTING → RUNNING → SUCCEEDED`

### Check CloudWatch Logs

```bash
# Tail the log group to see batch_job.py output
aws logs tail /aws/batch/sre-aws-batch-telemetry \
    --follow \
    --region us-west-2
```

You should see output like:
```
TracerProvider initialised, exporting to https://apm-na1.service.nicecxone-dev.com:4317
MeterProvider initialised, exporting to https://apm-na1.service.nicecxone-dev.com:4317
Fetched 142 items in 0.35s
Processed 142 items in 0.68s
Wrote 142 results in 0.12s
Batch job completed successfully. Items processed: 142
OTel providers shut down cleanly.
```

### Verify in Grafana / OpenAPM

1. Open Grafana dashboards connected to your OpenAPM instance
2. Go to **Explore → Tempo** (traces)
3. Search for `service.name = sre-aws-batch-telemetry`
4. You should see the trace hierarchy:
   - `batch-job-execution` (parent)
     - `fetch-data`
     - `process-data`
     - `write-results`
5. Go to **Explore → Mimir/Prometheus** (metrics)
6. Query for: `job_duration_seconds`, `job_items_processed_total`, `job_status`

---

## Troubleshooting

### Job stuck in RUNNABLE

- The compute environment can't provision Fargate capacity in your subnets
- Verify subnets have a route to a NAT Gateway (needed for ECR pull)
- Check: `aws ec2 describe-route-tables --filters "Name=association.subnet-id,Values=<SUBNET_ID>"`

### Job fails with ESSENTIAL CONTAINER EXITED

```bash
# Check the container exit reason
aws batch describe-jobs --jobs <JOB_ID> --region us-west-2 \
    --query "jobs[0].attempts[*].{StatusReason:statusReason, ExitCode:container.exitCode}"
```

### No telemetry in OpenAPM

1. Check CloudWatch Logs for OTel errors (connection refused, timeout, TLS errors)
2. Verify Security Group allows egress on port 4317 to the APM endpoint
3. Test DNS resolution from a resource in the same VPC:
   ```bash
   nslookup apm-na1.service.nicecxone-dev.com
   ```
4. Ensure the private DNS `apm-na1.service.nicecxone-dev.com` resolves in your VPC

### ECR image pull fails

- Check the Execution Role has `ecr:GetAuthorizationToken` + image pull permissions
- Verify the subnets can reach ECR (NAT Gateway or VPC Endpoint for `com.amazonaws.us-west-2.ecr.api` and `com.amazonaws.us-west-2.ecr.dkr`)

### CloudFormation stack fails

```bash
# See the failure reason
aws cloudformation describe-stack-events \
    --stack-name <STACK_NAME> \
    --region us-west-2 \
    --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
    --output table
```

---

## Cleanup

When done with the POC, delete everything:

```bash
# If you used Option A (master stack):
aws cloudformation delete-stack --stack-name sre-aws-batch-telemetry-dev --region us-west-2

# If you used Option B (individual stacks) — delete in reverse order:
aws cloudformation delete-stack --stack-name sre-aws-batch-telemetry-dev-jobdef --region us-west-2
aws cloudformation wait stack-delete-complete --stack-name sre-aws-batch-telemetry-dev-jobdef --region us-west-2

aws cloudformation delete-stack --stack-name sre-aws-batch-telemetry-dev-queue --region us-west-2
aws cloudformation wait stack-delete-complete --stack-name sre-aws-batch-telemetry-dev-queue --region us-west-2

aws cloudformation delete-stack --stack-name sre-aws-batch-telemetry-dev-ce --region us-west-2
aws cloudformation wait stack-delete-complete --stack-name sre-aws-batch-telemetry-dev-ce --region us-west-2

aws cloudformation delete-stack --stack-name sre-aws-batch-telemetry-dev-sg --region us-west-2
aws cloudformation delete-stack --stack-name sre-aws-batch-telemetry-dev-iam --region us-west-2

# Delete ECR repo (optional)
aws ecr delete-repository --repository-name sre-aws-batch-telemetry --force --region us-west-2

# Delete S3 bucket (optional)
aws s3 rb "s3://sre-batch-telemetry-cfn-templates-723346695882" --force
```

---

## Quick Reference — All Values

| Parameter | Value |
|---|---|
| AWS Account | 723346695882 (mon-sandbox) |
| Region | us-west-2 (Oregon) |
| VPC | vpc-0693b34275513631c (shared_eks) |
| Private Subnets | subnet-0cdc843ec821d3ea5 (2a), subnet-071fb611b8b3abf42 (2b), subnet-00c9a34807a6d3742 (2c) |
| Service Name | sre-aws-batch-telemetry |
| ECR Image | 723346695882.dkr.ecr.us-west-2.amazonaws.com/sre-aws-batch-telemetry:latest |
| OTel Endpoint | https://apm-na1.service.nicecxone-dev.com:4317 |
| OTel Protocol | OTLP/gRPC |
| OTel Resource Attrs | environment=mon-sandbox,account.id=723346695882 |
| Compute Type | Fargate (serverless) |
| vCPU / Memory | 0.25 vCPU / 512 MiB |
| Log Group | /aws/batch/sre-aws-batch-telemetry |

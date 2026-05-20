#!/bin/bash
# ──────────────────────────────────────────────────────────────────
# Build and deploy the Java Spring Boot Batch Telemetry POC
# Run this in AWS CloudShell (us-west-2, mon-sandbox account)
# ──────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Configuration ──
S3_BUCKET="sre-batch-telemetry-code-723346695882"
S3_KEY="java/batch-telemetry.jar"
STACK_NAME="sre-batch-java-dev-jobdef"
REGION="us-west-2"
EXEC_ROLE="arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-execution-role"
JOB_ROLE="arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-job-role"
JOB_QUEUE="sre-aws-batch-telemetry-dev-queue"

echo "================================================"
echo "  Java Spring Boot Batch Telemetry POC - Deploy"
echo "================================================"

# ── Step 1: Check prerequisites ──
echo ""
echo "[1/5] Checking prerequisites..."
java -version 2>&1 | head -1
mvn -version 2>&1 | head -1 || {
    echo "Maven not found. Installing..."
    sudo yum install -y maven -q 2>/dev/null || sudo dnf install -y maven -q 2>/dev/null || {
        echo "Installing Maven manually..."
        curl -sL https://archive.apache.org/dist/maven/maven-3/3.9.6/binaries/apache-maven-3.9.6-bin.tar.gz | tar xz -C /tmp
        export PATH="/tmp/apache-maven-3.9.6/bin:$PATH"
        mvn -version 2>&1 | head -1
    }
}

# ── Step 2: Build fat JAR ──
echo ""
echo "[2/5] Building fat JAR..."
cd "$(dirname "$0")/../java-poc"
mvn clean package -DskipTests -q
JAR_PATH="target/batch-telemetry.jar"
JAR_SIZE=$(du -h "$JAR_PATH" | cut -f1)
echo "Built: $JAR_PATH ($JAR_SIZE)"

# ── Step 3: Upload JAR to S3 ──
echo ""
echo "[3/5] Uploading JAR to S3..."
aws s3 cp "$JAR_PATH" "s3://${S3_BUCKET}/${S3_KEY}" --region "$REGION"
echo "Uploaded to s3://${S3_BUCKET}/${S3_KEY}"

# ── Step 4: Deploy CloudFormation stack ──
echo ""
echo "[4/5] Deploying CloudFormation stack: ${STACK_NAME}..."
cd "$(dirname "$0")/.."
aws cloudformation deploy \
    --stack-name "$STACK_NAME" \
    --template-file cloudformation/batch-job-definition-java.yaml \
    --parameter-overrides \
        ExecutionRoleArn="$EXEC_ROLE" \
        JobRoleArn="$JOB_ROLE" \
    --no-fail-on-empty-changeset \
    --region "$REGION"
echo "Stack deployed: ${STACK_NAME}"

# ── Step 5: Submit job ──
echo ""
echo "[5/5] Submitting batch job..."
JOB_ID=$(aws batch submit-job \
    --job-name "java-telemetry-poc-$(date +%s)" \
    --job-queue "$JOB_QUEUE" \
    --job-definition "sre-batch-telemetry-java-dev-job" \
    --region "$REGION" \
    --query 'jobId' --output text)

echo ""
echo "================================================"
echo "  Job submitted: $JOB_ID"
echo "================================================"
echo ""
echo "Monitor with:"
echo "  aws batch describe-jobs --jobs $JOB_ID --region $REGION --query 'jobs[0].status'"
echo ""
echo "View logs:"
echo "  aws logs tail /aws/batch/sre-batch-telemetry-java --follow --region $REGION"

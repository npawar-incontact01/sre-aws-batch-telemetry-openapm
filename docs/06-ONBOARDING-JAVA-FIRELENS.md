# AWS Batch — OpenAPM Onboarding Guide: Java Dual-Container (Firelens Pattern)

> **Document Type:** Team Onboarding Guide  
> **Audience:** R&D Teams running AWS Batch jobs who want to send telemetry to OpenAPM  
> **Pattern:** Java 17 / Spring Boot 3.3 · Dual-container · Micrometer + Firelens sidecar  
> **OpenAPM Platform:** Hosted in NICE mon accounts (Grafana / Tempo / Mimir / Loki)  
> **Reference POC:** `poc/java-firelens-architect-approach` — All 3 signals confirmed working  
> **Last Updated:** May 2026

---

## Table of Contents

1. [What This Guide Covers](#1-what-this-guide-covers)
2. [Responsibility Split — SRE vs Your Team](#2-responsibility-split--sre-vs-your-team)
3. [Prerequisites Checklist](#3-prerequisites-checklist)
4. [Step 1 — Request OpenAPM Access and VPC Endpoint](#4-step-1--request-openapm-access-and-vpc-endpoint)
5. [Step 2 — Create S3 Bucket and Upload Fluent Bit Config](#5-step-2--create-s3-bucket-and-upload-fluent-bit-config)
6. [Step 3 — Deploy IAM Roles](#6-step-3--deploy-iam-roles)
7. [Step 4 — Deploy Batch Infrastructure](#7-step-4--deploy-batch-infrastructure)
8. [Step 5 — Adapt Your Java Application](#8-step-5--adapt-your-java-application)
9. [Step 6 — Deploy the Firelens Job Definition](#9-step-6--deploy-the-firelens-job-definition)
10. [Step 7 — Build, Upload and Submit a Test Job](#10-step-7--build-upload-and-submit-a-test-job)
11. [Step 8 — Validate in Grafana OpenAPM](#11-step-8--validate-in-grafana-openapm)
12. [Mandatory Labels and Environment Variables](#12-mandatory-labels-and-environment-variables)
13. [Cross-Account Considerations](#13-cross-account-considerations)
14. [Common Errors and Fixes](#14-common-errors-and-fixes)
15. [Support and Contacts](#15-support-and-contacts)

---

## 1. What This Guide Covers

This guide walks R&D teams through onboarding an **AWS Batch Java job** to send all three observability signals — **Traces, Metrics, and Logs** — to NICE's **OpenAPM** platform using the validated **Firelens sidecar pattern**.

### What OpenAPM gives you

| Signal | Backend | What you get |
|--------|---------|-------------|
| **Traces** | Grafana Tempo | End-to-end job execution trace, latency by phase, span attributes |
| **Metrics** | Grafana Mimir | Job duration, items processed, success/failure gauge, dashboards, alerts |
| **Logs** | Grafana Loki | Structured logs correlated to trace IDs, searchable by `service_name` |

### Why the Firelens pattern?

This is the **Architect-recommended approach** for NICE, consistent with how all ECS microservices route logs. Key benefits:

- **No app code changes for logs** — stdout is captured automatically by the Firelens sidecar
- **ECS-aligned** — same log routing pattern as existing NICE microservices
- **All 3 signals working** — confirmed in POC, job IDs `91ef7036` and `7dd4a30b`

### Important: OpenAPM is hosted in NICE mon accounts

OpenAPM (Grafana, Tempo, Mimir, Loki, OTLP Collector) runs in NICE's **mon accounts**. If your Batch job runs in a **different AWS account or region**, you need additional network setup to reach the OpenAPM endpoint. See [Step 1](#4-step-1--request-openapm-access-and-vpc-endpoint) and [Section 13 — Cross-Account Considerations](#13-cross-account-considerations).

---

## 2. Responsibility Split — SRE vs Your Team

Understanding who owns what before you start.

| Item | Owner | Notes |
|------|-------|-------|
| OpenAPM platform (Grafana, Tempo, Mimir, Loki) | **SRE / Platform** | Already running in mon accounts |
| OpenAPM OTLP Collector endpoint | **SRE / Platform** | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` |
| VPC Endpoint (PrivateLink) in **mon-sandbox** | **SRE / Platform** | `vpce-07577bcf5fb4edc78` — already exists |
| VPC Endpoint in **your AWS account** (if different) | **Your team + Network/Platform** | Must be requested — see Step 1 |
| Reference Fluent Bit config (`batch-fluent-bit.conf`) | **SRE** | Stored in SRE S3 bucket; you copy it to your own bucket |
| CloudFormation templates (IAM, CE, JQ, Job Def) | **SRE (templates)** | You deploy them in your account with your parameters |
| Your S3 bucket (JAR + Fluent Bit config) | **Your team** | Must be in your account and region |
| Your IAM Roles (Execution + Job) | **Your team** | Deployed from SRE-provided IAM template |
| Your Batch infrastructure (CE, JQ, SG) | **Your team** | Deployed from SRE-provided CF templates |
| Java app code changes | **Your team** | Add Micrometer deps + env vars |
| Grafana dashboards and alerts | **Your team** | Using your `service_name` and `openapm_product_name` |

---

## 3. Prerequisites Checklist

Complete these before starting the setup steps.

### Your Team Must Have

- [ ] An **AWS account** (any NICE-managed account) with AWS Batch on Fargate enabled
- [ ] A **VPC** with private subnets (no public internet required)
- [ ] Network connectivity to the OpenAPM endpoint (VPC Endpoint or VPC Peering — see Step 1)
- [ ] **AWS CLI** access with sufficient IAM permissions to deploy CloudFormation stacks
- [ ] **Java 17** and **Maven 3.6+** installed locally for building the JAR
- [ ] **AWS CloudShell** access in your account (recommended for uploads — avoids GitHub CDN cache issues)
- [ ] A confirmed `openapm_product_name` value — agree this with your Platform/SRE contact
- [ ] A confirmed `service_name` value — must be unique across OpenAPM (e.g. `my-team-batch-processor`)

### Information to Gather

| Item | Where to get it | Example |
|------|----------------|---------|
| Your AWS Account ID | AWS Console → top-right | `123456789012` |
| Your AWS Region | Where your Batch jobs run | `us-east-1` |
| Your VPC ID | VPC Console | `vpc-0abc12345` |
| Your private subnet IDs | VPC → Subnets | `subnet-0abc...` |
| OpenAPM endpoint hostname | SRE team | `apm-na1.mon-sandbox.nicecxone-sbx.com` |
| OpenAPM endpoint port | Always `4318` (OTLP/HTTP) | `4318` |
| `openapm_product_name` | Agree with SRE/Platform | `my-product` |

---

## 4. Step 1 — Request OpenAPM Access and VPC Endpoint

### Why this step is critical

Your Batch job on Fargate runs in a **private subnet** (no public internet). It must reach the OpenAPM OTLP Collector at `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` over a private network path.

In the SRE POC (mon-sandbox), this is already wired up. **If your job runs in a different account or region, you need to request this connectivity from your Network / Platform team.**

### What to Request

Raise a request to your Platform/Network team with the following:

```
Request: OpenAPM VPC Endpoint or network connectivity
Account: <your-account-id>
Region: <your-region>
VPC ID: <your-vpc-id>

We need network access (port 4318 TCP, OTLP/HTTP) from our Fargate Batch tasks
to the OpenAPM OTLP Collector endpoint:
  Host: apm-na1.mon-sandbox.nicecxone-sbx.com
  Port: 4318
  Protocol: HTTPS / OTLP-HTTP (NOT gRPC / port 4317)

Preferred method: AWS PrivateLink VPC Endpoint in our account/VPC.
Alternative: VPC Peering or Transit Gateway if PrivateLink is not available.
```

> **Critical protocol note:** OpenAPM only supports **OTLP/HTTP (`http/protobuf`) on port 4318**.  
> gRPC (port 4317) is **not supported** through the VPC Endpoint. Attempting gRPC will result in `UNAVAILABLE` errors with zero telemetry delivered.

### After Your VPC Endpoint Is Provisioned

Once you have a VPC Endpoint (or equivalent), note:
- The VPC Endpoint **Security Group must allow TCP inbound on port 4318** from your VPC CIDR
- Your Batch task **Security Group must allow TCP egress on port 4318** to the endpoint
- Confirm connectivity with a simple `curl` test from CloudShell within your VPC:

```bash
curl -v --max-time 5 \
  https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318/v1/traces \
  -H "Content-Type: application/x-protobuf" \
  --data ""
# Expected: HTTP 400 (bad request) — confirms connectivity is working
# NOT expected: connection timeout or connection refused
```

---

## 5. Step 2 — Create S3 Bucket and Upload Fluent Bit Config

Your Batch job (and the Firelens sidecar) need an S3 bucket **in your account and region** for:
1. Your compiled Java JAR (`batch-telemetry.jar`)
2. The Fluent Bit configuration file (`batch-fluent-bit.conf`)

### 2.1 Create the S3 Bucket

```bash
# Replace with your account ID and region
ACCOUNT_ID=<your-account-id>
REGION=<your-region>
BUCKET="batch-telemetry-code-${ACCOUNT_ID}"

aws s3 mb s3://${BUCKET} --region ${REGION}

# Block all public access (security requirement)
aws s3api put-public-access-block \
  --bucket ${BUCKET} \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

### 2.2 Download and Upload the Fluent Bit Config

The Fluent Bit config is maintained by SRE. Download it from the SRE reference repository and upload to your bucket:

```bash
# Clone the reference repo (or download the file directly)
git clone https://github.com/npawar-incontact01/sre-aws-batch-telemetry-openapm.git
cd sre-aws-batch-telemetry-openapm

# Upload Fluent Bit config to YOUR bucket
aws s3 cp docker/fluent-bit/batch-fluent-bit.conf \
  s3://${BUCKET}/fluent-bit/batch-fluent-bit.conf \
  --region ${REGION}

echo "Fluent Bit config uploaded."
echo "ARN: arn:aws:s3:::${BUCKET}/fluent-bit/batch-fluent-bit.conf"
```

> **Note the ARN format** — the Firelens init image requires `arn:aws:s3:::bucket/key` format (not `s3://bucket/key`). Using the S3 URI format will cause a startup error: `Could not parse arn: s3://...`

### What the Fluent Bit Config Does

The config (`batch-fluent-bit.conf`) handles three critical tasks:
1. **Receives** app stdout via Unix socket (Firelens)
2. **Injects** `service_name`, `openapm_product_name`, `region` into every log record via `record_modifier`
3. **Sends** logs to OpenAPM as OTLP with `logs_body_key_attributes true` — this promotes the injected fields to **OTLP resource attributes**, which is what the OpenAPM Collector reads to set the correct Loki stream label

> If you need to customise the config (e.g. add extra record fields), copy and modify it in your own bucket. Do not modify the SRE reference copy.

---

## 6. Step 3 — Deploy IAM Roles

Your Batch job needs two IAM roles:

| Role | Purpose | Assumed by |
|------|---------|------------|
| **Execution Role** | Allows Fargate to pull container images and write CloudWatch logs | ECS/Fargate agent |
| **Job Role** | Assumed by your running container — grants S3, SSM, CloudWatch Logs access | Your app code |

### Deploy from SRE CloudFormation Template

```bash
STACK_NAME="my-batch-iam-${ENVIRONMENT}"   # e.g. my-team-batch-iam-dev
SERVICE_NAME="my-batch-service"             # e.g. my-team-batch-processor
ENVIRONMENT="dev"

aws cloudformation deploy \
  --stack-name ${STACK_NAME} \
  --template-file cloudformation/iam-roles.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    EnvironmentName=${ENVIRONMENT} \
    ServiceName=${SERVICE_NAME} \
    Team=<your-team-name> \
    CostCenter=<your-cost-center> \
  --region ${REGION}
```

### Retrieve the Role ARNs for Later Steps

```bash
EXEC_ROLE=$(aws cloudformation describe-stacks \
  --stack-name ${STACK_NAME} \
  --query "Stacks[0].Outputs[?OutputKey=='BatchExecutionRoleArn'].OutputValue" \
  --output text --region ${REGION})

JOB_ROLE=$(aws cloudformation describe-stacks \
  --stack-name ${STACK_NAME} \
  --query "Stacks[0].Outputs[?OutputKey=='BatchJobRoleArn'].OutputValue" \
  --output text --region ${REGION})

echo "Execution Role: ${EXEC_ROLE}"
echo "Job Role:       ${JOB_ROLE}"
```

### Additional IAM Permissions Required for Firelens

The **Job Role** must also have these permissions (add them to the `MinimalJobPolicy` in the IAM template if not already present):

```yaml
# Required for Firelens init image to download custom config from S3
- Effect: Allow
  Action:
    - s3:GetObject
    - s3:GetBucketLocation       # ← Critical — init image calls this first
    - s3:ListBucket
  Resource:
    - arn:aws:s3:::batch-telemetry-code-<your-account-id>
    - arn:aws:s3:::batch-telemetry-code-<your-account-id>/*

# Required for CloudWatch Logs backup output in Fluent Bit config
- Effect: Allow
  Action:
    - logs:CreateLogGroup
    - logs:CreateLogStream
    - logs:PutLogEvents
  Resource:
    - arn:aws:logs:<region>:<account-id>:log-group:/aws/batch/*
```

> **`s3:GetBucketLocation` is commonly missed.** The Firelens init image calls `GetBucketLocation` before downloading the config. Without this permission, the sidecar will fail to start with `AccessDenied` even if `s3:GetObject` is present.

---

## 7. Step 4 — Deploy Batch Infrastructure

If you don't already have a Batch Compute Environment and Job Queue, deploy them using the SRE reference templates.

### 4.1 Security Group

```bash
aws cloudformation deploy \
  --stack-name "my-batch-sg-${ENVIRONMENT}" \
  --template-file cloudformation/security-groups.yaml \
  --parameter-overrides \
    EnvironmentName=${ENVIRONMENT} \
    ServiceName=${SERVICE_NAME} \
    VpcId=<your-vpc-id> \
  --region ${REGION}
```

### 4.2 Compute Environment

```bash
# Get SG ID from previous stack
SG_ID=$(aws cloudformation describe-stacks \
  --stack-name "my-batch-sg-${ENVIRONMENT}" \
  --query "Stacks[0].Outputs[?OutputKey=='BatchSecurityGroupId'].OutputValue" \
  --output text --region ${REGION})

aws cloudformation deploy \
  --stack-name "my-batch-ce-${ENVIRONMENT}" \
  --template-file cloudformation/batch-compute-environment.yaml \
  --parameter-overrides \
    EnvironmentName=${ENVIRONMENT} \
    ServiceName=${SERVICE_NAME} \
    SubnetIds="<subnet-id-1>,<subnet-id-2>" \
    SecurityGroupId=${SG_ID} \
    ExecutionRoleArn=${EXEC_ROLE} \
  --region ${REGION}
```

### 4.3 Job Queue

```bash
CE_ARN=$(aws cloudformation describe-stacks \
  --stack-name "my-batch-ce-${ENVIRONMENT}" \
  --query "Stacks[0].Outputs[?OutputKey=='ComputeEnvironmentArn'].OutputValue" \
  --output text --region ${REGION})

aws cloudformation deploy \
  --stack-name "my-batch-jq-${ENVIRONMENT}" \
  --template-file cloudformation/batch-job-queue.yaml \
  --parameter-overrides \
    EnvironmentName=${ENVIRONMENT} \
    ServiceName=${SERVICE_NAME} \
    ComputeEnvironmentArn=${CE_ARN} \
  --region ${REGION}
```

---

## 8. Step 5 — Adapt Your Java Application

### 5.1 Required Dependencies (`pom.xml`)

Add these to your `pom.xml`. The **key dependencies** are Micrometer tracing bridge, OTLP metric exporter, and the OTel BOM for version alignment. You do **not** need the OTel log appender — logs go through stdout → Firelens.

```xml
<dependencyManagement>
  <dependencies>
    <!-- OTel BOM — aligns all opentelemetry-* versions -->
    <dependency>
      <groupId>io.opentelemetry</groupId>
      <artifactId>opentelemetry-bom</artifactId>
      <version>1.39.0</version>
      <type>pom</type>
      <scope>import</scope>
    </dependency>
    <!-- Spring Boot BOM -->
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-dependencies</artifactId>
      <version>3.3.13</version>
      <type>pom</type>
      <scope>import</scope>
    </dependency>
  </dependencies>
</dependencyManagement>

<dependencies>
  <!-- Spring Boot -->
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter</artifactId>
  </dependency>
  <dependency>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-actuator</artifactId>
  </dependency>

  <!-- Traces: Micrometer → OTel bridge -->
  <dependency>
    <groupId>io.micrometer</groupId>
    <artifactId>micrometer-tracing-bridge-otel</artifactId>
  </dependency>
  <dependency>
    <groupId>io.opentelemetry</groupId>
    <artifactId>opentelemetry-exporter-otlp</artifactId>
  </dependency>

  <!-- Metrics: Micrometer OTLP registry (push to Mimir) -->
  <dependency>
    <groupId>io.micrometer</groupId>
    <artifactId>micrometer-registry-otlp</artifactId>
  </dependency>

  <!-- CVE fix: protobuf 3.25.5 (CVE-2024-7254) -->
  <dependency>
    <groupId>com.google.protobuf</groupId>
    <artifactId>protobuf-java</artifactId>
    <version>3.25.5</version>
  </dependency>
</dependencies>

<build>
  <plugins>
    <plugin>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-maven-plugin</artifactId>
    </plugin>
  </plugins>
</build>
```

> **No `opentelemetry-logback-appender` needed.** On the Firelens pattern, logs go to stdout and are captured by the sidecar — no OTel log wiring in the app.

### 5.2 Application Configuration (`application.yml`)

```yaml
spring:
  application:
    name: ${OTEL_SERVICE_NAME}
  main:
    web-application-type: none          # Batch job — no HTTP server

management:
  tracing:
    sampling:
      probability: 1.0                  # 100% sampling for Batch
  otlp:
    tracing:
      endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces
    metrics:
      export:
        url: ${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics
        step: 10s                       # Push metrics every 10 seconds
        resource-attributes:
          service.name:          ${OTEL_SERVICE_NAME}
          service_name:          ${OTEL_SERVICE_NAME}
          environment:           ${ENVIRONMENT:dev}
          openapm_product_name:  ${OPENAPM_PRODUCT_NAME}
          region:                ${AWS_REGION:us-east-1}
          account.id:            ${AWS_ACCOUNT_ID}
```

### 5.3 Logback Configuration (`logback-spring.xml`)

On the Firelens pattern, use **CONSOLE appender only**. Do not add `OpenTelemetryAppender`.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <appender name="CONSOLE" class="ch.qos.logback.core.ConsoleAppender">
        <encoder>
            <pattern>%d{ISO8601} %-5level [%thread] %logger{36} - %msg%n</pattern>
        </encoder>
    </appender>
    <root level="INFO">
        <appender-ref ref="CONSOLE"/>
        <!-- No OpenTelemetryAppender — Firelens sidecar captures stdout -->
    </root>
</configuration>
```

### 5.4 Java Application Pattern (`CommandLineRunner`)

```java
@SpringBootApplication
public class YourBatchApplication implements CommandLineRunner {

    private static final Logger log = LoggerFactory.getLogger(YourBatchApplication.class);

    @Autowired private Tracer tracer;
    @Autowired private MeterRegistry meterRegistry;

    public static void main(String[] args) {
        SpringApplication.run(YourBatchApplication.class, args);
    }

    @Override
    public void run(String... args) throws Exception {
        // ── Metrics setup ──────────────────────────────────────────────
        Timer jobTimer = Timer.builder("job_duration_seconds")
            .description("Total job wall-clock time")
            .register(meterRegistry);

        Counter itemsCounter = Counter.builder("job_items_processed_total")
            .description("Items processed")
            .register(meterRegistry);

        AtomicInteger jobStatus = new AtomicInteger(1);  // 1 = error (default)
        Gauge.builder("job_status", jobStatus, AtomicInteger::get)
            .description("0=success, 1=error")
            .register(meterRegistry);

        // ── Trace the full job ─────────────────────────────────────────
        Span rootSpan = tracer.nextSpan().name("your-job-execution").start();
        try (Tracer.SpanInScope ws = tracer.withSpan(rootSpan)) {

            jobTimer.record(() -> {
                log.info("Batch job starting");
                int count = fetchData();           // implement your phases
                processData(count);
                writeResults(count);
                itemsCounter.increment(count);
                jobStatus.set(0);                  // 0 = success
                log.info("Batch job complete. items={}", count);
            });

        } catch (Exception e) {
            log.error("Batch job failed", e);
            rootSpan.error(e);
            throw e;
        } finally {
            rootSpan.end();
            flushTelemetry();
        }
    }

    private void flushTelemetry() throws InterruptedException {
        // Close MeterRegistry → triggers final metric push at step boundary
        if (meterRegistry instanceof AutoCloseable c) {
            try { c.close(); } catch (Exception ignored) {}
        }
        // Sleep 15s to allow BatchSpanProcessor to flush traces
        // and Micrometer to complete the final metric step push
        Thread.sleep(15_000);
    }
}
```

> **Why `Thread.sleep(15_000)` before exit?**  
> Micrometer OTLP metrics are pushed at each `step` boundary (10s). If the JVM exits before the next step boundary, the final metrics are lost. The sleep ensures at least one full step cycle completes after `meterRegistry.close()`. This is a known pattern for short-lived Batch jobs — not an issue for long-running services.

---

## 9. Step 6 — Deploy the Firelens Job Definition

Use the SRE reference CloudFormation template `cloudformation/batch-job-definition-java-firelens.yaml`, overriding parameters for your service.

### Required Parameter Overrides

| Parameter | Your Value | Notes |
|-----------|-----------|-------|
| `ServiceName` | `your-service-name` | Becomes Loki `service_name` label |
| `EnvironmentName` | `dev` / `staging` / `prod` | |
| `ExecutionRoleArn` | From Step 3 | |
| `JobRoleArn` | From Step 3 | |
| `ContainerImage` | Your image URI | Must be publicly reachable or in ECR in your account |
| `CodeS3Bucket` | `batch-telemetry-code-<your-account-id>` | From Step 2 |
| `CodeS3Key` | `java/your-app.jar` | Your JAR key in the bucket |
| `OtelEndpoint` | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` | Confirm with SRE |
| `OtelEndpointHost` | `apm-na1.mon-sandbox.nicecxone-sbx.com` | Hostname without port |
| `OpenapmProductName` | `your-product-name` | Agree with SRE team |
| `FirelensConfigS3Path` | `arn:aws:s3:::batch-telemetry-code-<your-account-id>/fluent-bit/batch-fluent-bit.conf` | **ARN format — not s3:// URI** |
| `OtelResourceAttributes` | See below | |

**`OtelResourceAttributes` value (substitute your values):**
```
environment=dev,account.id=<your-account-id>,openapm_product_name=<your-product>,service_name=<your-service>,region=<your-region>
```

### Deploy Command

```bash
SERVICE_NAME="your-service-name"          # e.g. my-team-batch-processor
ENVIRONMENT="dev"
ACCOUNT_ID=<your-account-id>
REGION=<your-region>
PRODUCT_NAME="your-product-name"
BUCKET="batch-telemetry-code-${ACCOUNT_ID}"
OTEL_ENDPOINT="https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318"
OTEL_HOST="apm-na1.mon-sandbox.nicecxone-sbx.com"
FIRELENS_CONFIG_ARN="arn:aws:s3:::${BUCKET}/fluent-bit/batch-fluent-bit.conf"
OTEL_ATTRS="environment=${ENVIRONMENT},account.id=${ACCOUNT_ID},openapm_product_name=${PRODUCT_NAME},service_name=${SERVICE_NAME},region=${REGION}"

aws cloudformation deploy \
  --stack-name "${SERVICE_NAME}-${ENVIRONMENT}-firelens-jobdef" \
  --template-file cloudformation/batch-job-definition-java-firelens.yaml \
  --parameter-overrides \
    EnvironmentName=${ENVIRONMENT} \
    ServiceName=${SERVICE_NAME} \
    ExecutionRoleArn=${EXEC_ROLE} \
    JobRoleArn=${JOB_ROLE} \
    CodeS3Bucket=${BUCKET} \
    CodeS3Key="java/${SERVICE_NAME}.jar" \
    OtelEndpoint=${OTEL_ENDPOINT} \
    OtelEndpointHost=${OTEL_HOST} \
    OpenapmProductName=${PRODUCT_NAME} \
    FirelensConfigS3Path=${FIRELENS_CONFIG_ARN} \
    OtelResourceAttributes="${OTEL_ATTRS}" \
    Team=<your-team> \
    CostCenter=<your-cost-center> \
  --region ${REGION}
```

### Retrieve the Job Definition Name

```bash
JOB_DEF=$(aws cloudformation describe-stacks \
  --stack-name "${SERVICE_NAME}-${ENVIRONMENT}-firelens-jobdef" \
  --query "Stacks[0].Outputs[?OutputKey=='JobDefinitionArn'].OutputValue" \
  --output text --region ${REGION})
echo "Job Definition: ${JOB_DEF}"
```

---

## 10. Step 7 — Build, Upload and Submit a Test Job

### 7.1 Build the Fat JAR

```bash
cd your-java-project
mvn clean package -DskipTests
# → target/your-app.jar
```

### 7.2 Upload JAR to S3 via CloudShell

> **Use CloudShell upload, not `curl` from GitHub.** GitHub CDN caches file downloads for up to 5 minutes. Uploading directly via CloudShell "Actions → Upload file" bypasses the cache entirely.

```bash
# In AWS CloudShell (your account):
# 1. Actions → Upload file → select your-app.jar
# 2. Then push to S3:

aws s3 cp ~/your-app.jar \
  s3://${BUCKET}/java/${SERVICE_NAME}.jar \
  --region ${REGION}
echo "JAR uploaded."
```

### 7.3 Get the Job Queue Name

```bash
JOB_QUEUE=$(aws cloudformation describe-stacks \
  --stack-name "my-batch-jq-${ENVIRONMENT}" \
  --query "Stacks[0].Outputs[?OutputKey=='JobQueueArn'].OutputValue" \
  --output text --region ${REGION})
```

### 7.4 Submit a Test Job

```bash
JOB_ID=$(aws batch submit-job \
  --job-name "${SERVICE_NAME}-test-$(date +%Y%m%d%H%M%S)" \
  --job-queue ${JOB_QUEUE} \
  --job-definition ${JOB_DEF} \
  --region ${REGION} \
  --query 'jobId' --output text)

echo "Submitted job: ${JOB_ID}"
```

### 7.5 Monitor Job Status

```bash
# Poll every 15 seconds
watch -n 15 "aws batch describe-jobs \
  --jobs ${JOB_ID} --region ${REGION} \
  --query 'jobs[0].[status,statusReason]' --output text"

# One-shot check
aws batch describe-jobs \
  --jobs ${JOB_ID} --region ${REGION} \
  --query 'jobs[0].{Status:status,Reason:statusReason,StartedAt:startedAt,StoppedAt:stoppedAt}'
```

Expected progression: `SUBMITTED → PENDING → RUNNABLE → STARTING → RUNNING → SUCCEEDED`

---

## 11. Step 8 — Validate in Grafana OpenAPM

Once your job reaches `SUCCEEDED`, wait 1–2 minutes for data to propagate, then validate all three signals in Grafana.

> **Grafana URL:** Provided by SRE team — `https://grafana.apm-na1.mon-sandbox.nicecxone-sbx.com` (or equivalent)

### Traces — Grafana Explore → Tempo

1. Go to **Explore** → select **Tempo** datasource
2. Search by service name:

```
{service.name="<your-service-name>"}
```

**Confirm:**
- [ ] Root span `your-job-execution` present
- [ ] Child spans: `fetch-data`, `process-data`, `write-results` (or your phase names)
- [ ] `service.name` attribute correct
- [ ] Span duration is non-zero
- [ ] No error spans (unless job intentionally failed)

### Metrics — Grafana Explore → Mimir (Prometheus)

```promql
# Job duration
job_duration_seconds_sum{service_name="<your-service-name>"}

# Items processed
job_items_processed_total{service_name="<your-service-name>"}

# Job status (0=success, 1=error)
job_status{service_name="<your-service-name>"}
```

**Confirm:**
- [ ] All three metrics present
- [ ] `service_name` label matches your service name
- [ ] `openapm_product_name` label present
- [ ] `job_status = 0` (success)

### Logs — Grafana Explore → Loki

```logql
{service_name="<your-service-name>"}
```

**Confirm:**
- [ ] Logs appear (not empty result)
- [ ] `service_name` label is correct — **not `unknown_service`**
- [ ] `openapm_product_name` and `region` labels indexed correctly
- [ ] Log body contains `traceId` and `spanId` (from Micrometer MDC)
- [ ] `container_name=app` field present

### Trace-Log Correlation

In a trace view, click on any span → **"Logs for this span"** — you should see the correlated log lines from Loki for that exact `traceId`.

---

## 12. Mandatory Labels and Environment Variables

These must be present on every job definition. Missing or incorrect values will result in telemetry landing under `unknown_service` in Loki.

### App Container Environment Variables

| Variable | Value | Purpose |
|----------|-------|---------|
| `OTEL_SERVICE_NAME` | `your-service-name` | Service identity for traces and metrics |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` | OTLP collector endpoint |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | **Must be `http/protobuf`** — gRPC not supported |
| `OTEL_RESOURCE_ATTRIBUTES` | `environment=dev,account.id=...,service_name=...,openapm_product_name=...,region=...` | Resource labels for all signals |
| `ENVIRONMENT` | `dev` / `staging` / `prod` | Deployment environment |
| `OPENAPM_PRODUCT_NAME` | `your-product-name` | Product grouping in OpenAPM |
| `AWS_REGION` | `us-east-1` (or your region) | Region label on metrics |
| `AWS_ACCOUNT_ID` | `123456789012` | Account label on metrics |

### Firelens Sidecar (`log_router`) Environment Variables

| Variable | Value | Purpose |
|----------|-------|---------|
| `SERVICE_NAME` | `your-service-name` | Injected by `record_modifier` into every log record |
| `PRODUCT_NAME` | `your-product-name` | Injected into every log record |
| `REGION` | `us-east-1` | Injected into every log record |
| `APM_HOST` | `apm-na1.mon-sandbox.nicecxone-sbx.com` | OTLP output host (no port, no https) |
| `aws_fluent_bit_init_s3_1` | `arn:aws:s3:::your-bucket/fluent-bit/batch-fluent-bit.conf` | Config download ARN (**ARN format required**) |

### `service_name` Naming Convention

| Rule | Example |
|------|---------|
| Lowercase, hyphens only (no underscores, no dots) | `my-team-batch-processor` |
| Include team name prefix | `payments-batch-reconciler` |
| Must be unique across OpenAPM | Confirm with SRE before using |
| Must match across all three signals | Same value in `OTEL_SERVICE_NAME`, `SERVICE_NAME` sidecar env, and `OtelResourceAttributes` |

---

## 13. Cross-Account Considerations

If your Batch jobs run in an AWS account **other than** the mon-sandbox account (`723346695882`) used for the SRE POC, you need to address the following:

### Network Connectivity to OpenAPM

The most critical requirement. Options (in order of preference):

| Option | Description | Setup effort |
|--------|-------------|-------------|
| **VPC Endpoint (PrivateLink) in your account** | AWS PrivateLink endpoint in your VPC pointing to OpenAPM | Raise request with Network/Platform team |
| **VPC Peering / Transit Gateway** | Peer your VPC with the mon VPC | Raise request with Network/Platform team |
| **Direct public internet** | HTTPS to OpenAPM hostname | Not recommended — no private network path |

Raise a ticket with your Platform/Network team and reference this document. The endpoint details to provide:

```
Service endpoint: apm-na1.mon-sandbox.nicecxone-sbx.com
Port: 4318 (TCP)
Protocol: HTTPS / OTLP-HTTP
Direction: Outbound from your Batch Fargate tasks
```

### S3 Bucket

Your S3 bucket **must be in your account and region**. The Firelens init image uses the job's IAM role to download the config — cross-account S3 access requires additional bucket policy setup. The simplest approach is to copy `batch-fluent-bit.conf` to your own bucket (as described in Step 2).

### IAM Roles

IAM roles are account-scoped. Deploy the SRE-provided IAM CloudFormation template (`iam-roles.yaml`) in **your account**. Do not try to use the SRE POC roles from mon-sandbox.

### Different Region

If running in a region other than `us-west-2`, update:
- `AWS_REGION` environment variable on the app container
- `REGION` environment variable on the log_router sidecar
- `--region` flag on all AWS CLI commands
- The `region` field in `OtelResourceAttributes`
- S3 bucket region (use the same region as your Batch jobs)

The OpenAPM endpoint hostname does **not** change by region — it is a global endpoint served via PrivateLink.

---

## 14. Common Errors and Fixes

| Error | Symptom | Root Cause | Fix |
|-------|---------|-----------|-----|
| `Could not parse arn: s3://...` | log_router fails to start | `aws_fluent_bit_init_s3_1` uses `s3://` URI format | Change to `arn:aws:s3:::bucket/key` format |
| `s3:GetBucketLocation AccessDenied` | log_router fails to start | Job Role missing this permission | Add `s3:GetBucketLocation` to Job Role IAM policy |
| `CreateLogStream AccessDenied` | Sidecar startup error | Execution/Job Role missing CloudWatch Logs permissions | Add `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` to Job Role |
| Job SUCCEEDED but no telemetry in Grafana | Zero data in Tempo/Mimir/Loki | VPC Endpoint SG has no inbound rule | Add TCP 4318 inbound from VPC CIDR to VPC Endpoint SG |
| `UNAVAILABLE` on OTLP exporter | Connection error at job startup | gRPC protocol being used | Set `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` |
| Logs in Loki but `service_name=unknown_service` | Wrong Loki stream label | Using `:stable` Fluent Bit image (1.9.x) | Use `aws-for-fluent-bit:init-3.2.4` — do not use `:stable` |
| No logs in Loki at all | Empty Loki results | Using `:stable` Fluent Bit (metrics-only, no log support) | Use `aws-for-fluent-bit:init-3.2.4` |
| `Fargate resource requirements not valid` | CloudFormation deploy error | Task size not a valid Fargate combination | Set app to `0.75 vCPU / 1920 MiB`, sidecar to `0.25 / 128` — total = `1 vCPU / 2048 MiB` |
| Metrics missing final data point | Last metric push not received | JVM exited before step boundary | Add `meterRegistry.close()` + `Thread.sleep(15_000)` before exit |
| `EarlyValidation::ResourceExistenceCheck` | CloudFormation fails | Log group resource already exists | Remove `AWS::Logs::LogGroup` resource from template; CloudWatch auto-creates it |
| `!Sub not resolved` in CF hook | Stack fails early | Org-wide CF EarlyValidation hook | Replace `!Sub` references with literal hardcoded values |
| Stale template via `curl` | Changes not reflected | GitHub CDN cache | Use CloudShell "Upload file" button instead of `curl` |

---

## 15. Support and Contacts

| Topic | Contact |
|-------|---------|
| OpenAPM platform access and endpoint details | SRE / Platform team |
| VPC Endpoint / PrivateLink provisioning in your account | Network / Infrastructure team |
| `openapm_product_name` registration | SRE / Platform team |
| This onboarding guide and reference templates | SRE team — reference repo: `sre-aws-batch-telemetry-openapm` |
| CloudFormation template questions | Raise a GitHub issue or PR on the reference repo |

### Reference Repository

```
https://github.com/npawar-incontact01/sre-aws-batch-telemetry-openapm
Branch: poc/java-firelens-architect-approach
```

Key files for onboarding teams:

| File | Purpose |
|------|---------|
| `cloudformation/iam-roles.yaml` | Deploy Execution + Job IAM roles |
| `cloudformation/security-groups.yaml` | Batch Fargate security group |
| `cloudformation/batch-compute-environment.yaml` | Fargate compute environment |
| `cloudformation/batch-job-queue.yaml` | Job queue |
| `cloudformation/batch-job-definition-java-firelens.yaml` | **Main template — dual-container Firelens job definition** |
| `docker/fluent-bit/batch-fluent-bit.conf` | Fluent Bit config — copy to your S3 bucket |
| `java-poc/pom.xml` | Reference pom.xml with all required dependencies |
| `java-poc/src/main/resources/application.yml` | Reference application.yml |
| `java-poc/src/main/resources/logback-spring.xml` | Reference logback config (CONSOLE only) |
| `docs/05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md` | Full technical deep-dive on this pattern |
| `docs/01-AWS-BATCH-OPENAPM-GUIDELINES.md` | Engineering standards and constraints |

---

*Related documents in this repo:*
- [01-AWS-BATCH-OPENAPM-GUIDELINES.md](01-AWS-BATCH-OPENAPM-GUIDELINES.md) — Platform standards
- [02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md](02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md) — Developer quick-reference
- [03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md](03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md) — Architecture overview
- [05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md](05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md) — Technical deep-dive POC doc

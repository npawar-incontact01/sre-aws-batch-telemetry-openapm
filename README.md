# sre-aws-batch-telemetry — OpenAPM POC for AWS Batch Jobs

> **Repository:** `sre-aws-batch-telemetry-openapm`  
> **Team:** SRE | **Account:** mon-sandbox (`723346695882`) | **Region:** `us-west-2`

---

## Table of Contents

1. [Why We Did This](#1-why-we-did-this)
2. [What We Built](#2-what-we-built)
3. [Branches Overview](#3-branches-overview)
4. [Architecture](#4-architecture)
5. [How We Built It](#5-how-we-built-it)
6. [End Results](#6-end-results)
7. [Problems We Faced](#7-problems-we-faced)
8. [What Is Still Pending](#8-what-is-still-pending)
9. [Brian's Firelens Approach — Evidence](#9-brians-firelens-approach--evidence)
10. [Infrastructure Deployed](#10-infrastructure-deployed)
11. [Consumer Onboarding](#11-consumer-onboarding)
12. [Repository Structure](#12-repository-structure)

---

## 1. Why We Did This

### Background

AWS Batch jobs are used across multiple teams at NICE for running background workloads (data pipelines, reporting, scheduled processing). Until this POC, there was **no standard mechanism** to send observability signals (traces, metrics, logs) from Batch jobs into **OpenAPM** (NICE's central Grafana platform backed by Tempo, Mimir, Loki).

Without telemetry:
- No distributed tracing for batch job phases (fetch / process / write)
- No metrics for job duration, items processed, or success/failure rates
- No structured logs correlated to trace IDs in Grafana Explore
- Engineers debugging failures are limited to CloudWatch raw logs with no context

### Goal

Prove that a **Java Spring Boot AWS Batch job on Fargate** (and a Python equivalent) can send all three OpenTelemetry signals — **traces, metrics, and logs** — to OpenAPM with the correct `service_name` labels, without needing ECR image push or a sidecar.

### Constraints We Had to Work Around

| Constraint | Detail |
|---|---|
| No ECR access | Org IAM policy has explicit deny on `ecr:GetAuthorizationToken` in mon-sandbox |
| MFA required locally | All AWS CLI deploys had to go through AWS CloudShell |
| OpenAPM port 4318 only | gRPC (port 4317) returns `UNAVAILABLE` through the VPC Endpoint |
| No custom Firelens config | S3-sourced Fluent Bit configs are blocked on Fargate (ECS-only feature) |
| VPC Endpoint SG | Initially had no inbound rules — blocked all OTLP traffic |

---

## 2. What We Built

Three proof-of-concept implementations across three Git branches:

| Branch | Language | Approach | All 3 Signals Correct? |
|---|---|---|---|
| `poc/python-aws-batch` | Python 3.11 | OTel SDK direct OTLP/HTTP | ✅ Yes |
| `poc/java-aws-batch` | Java 17 / Spring Boot 3.3 | Micrometer + OTel Logback Appender | ✅ Yes |
| `poc/java-firelens-brian-approach` | Java 17 / Spring Boot 3.3 | Micrometer + Firelens sidecar | ⚠️ Logs arrive with wrong `service_name` |

The **working solution** is `poc/java-aws-batch`. The Firelens branch provides evidence for the architectural discussion with Architect Brian.

---

## 3. Branches Overview

### `poc/python-aws-batch`

Python 3.11 batch job using the OpenTelemetry Python SDK. All 3 signals sent directly via OTLP/HTTP. Code fetched from S3 at runtime — no ECR needed. Includes a reusable `batch_otel` library.  
See [docs/PYTHON-POC-SUMMARY.md](docs/PYTHON-POC-SUMMARY.md).

### `poc/java-aws-batch` — Working Solution ✅

Java Spring Boot 3.3.13 batch job:
- **Traces** via Micrometer Tracing (OTel bridge) → Tempo ✅
- **Metrics** via Micrometer OTLP Registry → Mimir ✅
- **Logs** via OTel Logback Appender + custom `SdkLoggerProvider` → Loki ✅

Fat JAR built locally, uploaded to S3. No ECR push. No sidecar.  
See [docs/JAVA-POC-SUMMARY.md](docs/JAVA-POC-SUMMARY.md).

### `poc/java-firelens-brian-approach` — Brian's Firelens Approach ✅

Implements Architect Brian's recommended approach: **Firelens sidecar for log routing**, same pattern as ECS microservices. Multi-container Batch task (app + Fluent Bit sidecar). All 3 signals working with correct `service_name` in Loki.  
Uses `aws-for-fluent-bit:init-3.2.4` + S3 custom config (`record_modifier` + `logs_body_key_attributes true`) — pattern from R&D team.  
See [docs/FIRELENS-EVIDENCE.md](docs/FIRELENS-EVIDENCE.md).

---

## 4. Architecture

### Working Solution (`poc/java-aws-batch`)

```
AWS Batch Fargate Task (1 vCPU / 2048 MiB)
└── Java container (amazoncorretto:17)
    │
    ├── Entrypoint: aws s3 cp .../batch-telemetry.jar → java -jar /app.jar
    │
    ├── Spring Boot 3.3.13 (CommandLineRunner — no web server)
    │
    ├── Micrometer Tracing (OTel bridge)
    │     └── BatchSpanProcessor → OTLPSpanExporter
    │           → OTLP/HTTP :4318/v1/traces → VPC Endpoint → Tempo ✅
    │
    ├── Micrometer OTLP Registry (push every 10s)
    │     └── OTLPMetricExporter
    │           → OTLP/HTTP :4318/v1/metrics → VPC Endpoint → Mimir ✅
    │
    └── Logback + OpenTelemetryAppender
          └── custom SdkLoggerProvider (built from OTEL_* env vars)
                → OtlpHttpLogRecordExporter
                      → OTLP/HTTP :4318/v1/logs → VPC Endpoint → Loki ✅
```

### Brian's Firelens Approach (`poc/java-firelens-brian-approach`)

```
AWS Batch Fargate Task (1 vCPU / 2048 MiB total)
├── app container (0.75 vCPU / 1920 MiB)
│     ├── Micrometer Tracing → OTLP :4318/v1/traces → Tempo ✅
│     ├── Micrometer OTLP   → OTLP :4318/v1/metrics → Mimir ✅
│     └── stdout (JSON logs) ──────────────────────┐
│                                                   ↓
└── log_router sidecar (0.25 vCPU / 128 MiB)  [aws-for-fluent-bit:3.3.0]
      └── opentelemetry plugin → OTLP :4318/v1/logs → Loki
            ⚠️  Logs arrive but service_name="unknown_service"
```

---

## 5. How We Built It

### Step 1 — Infrastructure via CloudFormation

Deployed in sequence from CloudShell (`mon-sandbox` profile, MFA):

```bash
# IAM roles
aws cloudformation deploy --stack-name sre-batch-iam-dev \
  --template-file cloudformation/iam-roles.yaml --capabilities CAPABILITY_NAMED_IAM

# Security group (TCP 4317, 4318, 443 open to VPC CIDR 10.0.0.0/21)
aws cloudformation deploy --stack-name sre-batch-sg-dev \
  --template-file cloudformation/security-groups.yaml

# Compute environment + job queue
aws cloudformation deploy --stack-name sre-batch-ce-dev \
  --template-file cloudformation/batch-compute-environment.yaml
aws cloudformation deploy --stack-name sre-batch-jq-dev \
  --template-file cloudformation/batch-job-queue.yaml

# Java job definition
aws cloudformation deploy --stack-name sre-batch-java-dev-jobdef \
  --template-file cloudformation/batch-job-definition-java.yaml \
  --parameter-overrides ExecutionRoleArn=... JobRoleArn=...
```

**Key infrastructure fix:** The shared VPC Endpoint Security Group had no inbound rules. Added TCP 4317, 4318, 443 inbound from VPC CIDR `10.0.0.0/21`.

### Step 2 — Java Application

**pom.xml key dependencies:**

```xml
<!-- opentelemetry-bom:1.39.0 manages versions -->
<dependency>
    <groupId>io.micrometer</groupId>
    <artifactId>micrometer-tracing-bridge-otel</artifactId>
</dependency>
<dependency>
    <groupId>io.opentelemetry</groupId>
    <artifactId>opentelemetry-exporter-otlp</artifactId>
</dependency>
<dependency>
    <groupId>io.micrometer</groupId>
    <artifactId>micrometer-registry-otlp</artifactId>
</dependency>
<dependency>
    <groupId>io.opentelemetry.instrumentation</groupId>
    <artifactId>opentelemetry-logback-appender-1.0</artifactId>
    <version>2.4.0-alpha</version>
</dependency>
<!-- CVE fix: CVE-2024-7254 -->
<dependency>
    <groupId>com.google.protobuf</groupId>
    <artifactId>protobuf-java</artifactId>
    <version>3.25.5</version>
</dependency>
```

**Critical log wiring** — Spring Boot 3.3's `OpenTelemetry` bean has no `SdkLoggerProvider`, so we build one from env vars:

```java
private static SdkLoggerProvider buildLoggerProvider() {
    String endpoint = System.getenv("OTEL_EXPORTER_OTLP_ENDPOINT");
    String serviceName = System.getenv("OTEL_SERVICE_NAME");
    // parse OTEL_RESOURCE_ATTRIBUTES into Resource attributes
    Resource resource = Resource.getDefault().merge(
        Resource.create(Attributes.of(
            AttributeKey.stringKey("service.name"), serviceName,
            AttributeKey.stringKey("service_name"), serviceName,
            // ... environment, region, account.id, openapm_product_name
        ))
    );
    OtlpHttpLogRecordExporter exporter = OtlpHttpLogRecordExporter.builder()
        .setEndpoint(endpoint + "/v1/logs")
        .build();
    return SdkLoggerProvider.builder()
        .setResource(resource)
        .addLogRecordProcessor(BatchLogRecordProcessor.builder(exporter).build())
        .build();
}

// In constructor — install BEFORE Spring context starts logging
OpenTelemetryAppender.install(
    OpenTelemetrySdk.builder().setLoggerProvider(buildLoggerProvider()).build()
);
```

### Step 3 — Build and Deploy

```bash
# Local — build fat JAR
cd java-poc && mvn clean package -DskipTests
# → target/batch-telemetry.jar (~23 MB)

# CloudShell — upload via Actions → Upload file, then:
aws s3 cp ~/batch-telemetry.jar \
  s3://sre-batch-telemetry-code-723346695882/java/batch-telemetry.jar \
  --region us-west-2

# Submit job
aws batch submit-job \
  --job-name sre-batch-java-test \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-java-dev-job \
  --region us-west-2
```

---

## 6. End Results

### Java POC (`poc/java-aws-batch`) — All 3 Signals Confirmed ✅

| Signal | Grafana Backend | Grafana Query | Status |
|--------|----------------|--------------|--------|
| Traces | Tempo | `{service.name="sre-batch-telemetry-java"}` | ✅ Correct label |
| Metrics | Mimir | `{service_name="sre-batch-telemetry-java"}` | ✅ Correct label |
| Logs | Loki | `{service_name="sre-batch-telemetry-java"}` | ✅ Correct label |

**Traces:** 4 spans per job run with full parent-child hierarchy:
```
batch-job-execution  (root, ~60s)
  ├── fetch-data      (~5s)
  ├── process-data    (~5s)
  └── write-results   (~5s)
```
Span tags: `job.items_processed`, `job.status`, `data.count`, `data.processed`, `data.written`.

**Logs:** Each log line carries `traceId` + `spanId` (MDC from Micrometer) — enables one-click drill-down from a Tempo trace to the matching Loki log line in Grafana Explore. Also carries `code_filepath`, `code_function`, `code_lineno`.

**Metrics pushed every 10s:**
- `job_duration_seconds`
- `job_items_processed_total`
- `job_status` (0=success, 1=error)

**Confirmed job:** `a0dd88f9` — SUCCEEDED. All 3 signals visible in Grafana with `service_name=sre-batch-telemetry-java`.

---

### Python POC (`poc/python-aws-batch`) — All 3 Signals Confirmed ✅

Python 3.11, OTel SDK. Code fetched from S3 at runtime. All signals reach OpenAPM with correct labels.

**Confirmed job:** `677348fb` — SUCCEEDED.

---

### Brian's Firelens Approach (`poc/java-firelens-brian-approach`) ✅

| Signal | Result |
|--------|--------|
| Traces | ✅ Correct (`service.name=sre-batch-telemetry-java`) |
| Metrics | ✅ Correct (`service_name=sre-batch-telemetry-java`) |
| Logs | ✅ Correct (`service_name=sre-batch-telemetry-java`) |

**Confirmed job:** `91ef7036` — SUCCEEDED. Logs visible in Loki under `{service_name="sre-batch-telemetry-java"}` with correct indexed labels.

---

## 7. Problems We Faced

### Problem 1 — VPC Endpoint Security Group Had No Inbound Rules

**Symptom:** All OTLP exports timed out. Job completed but zero telemetry in Grafana.

**Root cause:** VPC Endpoint SG had no inbound rules. Batch tasks in the VPC could not reach port 4318 of the endpoint.

**Fix:** Added inbound TCP 4317, 4318, 443 from VPC CIDR `10.0.0.0/21` to the VPC Endpoint security group.

---

### Problem 2 — gRPC (Port 4317) Does Not Work Through VPC Endpoint

**Symptom:** With `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, exporter returned `UNAVAILABLE`.

**Root cause:** The VPC Endpoint is HTTP-only. gRPC uses HTTP/2 framing that is not supported through this endpoint.

**Fix:** Switched to `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` on port 4318. All 3 signals work.

---

### Problem 3 — Logs Showing `service_name=unknown_service` (Attempt 1)

**Symptom:** Traces and metrics had correct label; logs appeared under `unknown_service`.

**Root cause:** `OpenTelemetryAppender` by default uses `GlobalOpenTelemetry`, which resolves to a no-op when no SDK is installed. Logs were silently dropped or sent without resource attributes.

**Attempted fix:** Called `OpenTelemetryAppender.install(springOpenTelemetryBean)`.

---

### Problem 4 — Logs Still `service_name=unknown_service` After Spring Bean Install (Attempt 2)

**Symptom:** Even after `install(springOpenTelemetryBean)`, logs arrived in Loki with `unknown_service`.

**Root cause (Spring Boot 3.3 specific):** Spring Boot 3.3's auto-configured `OpenTelemetry` bean is built for Micrometer Tracing only. Its internal `SdkLoggerProvider` is a **no-op** — it silently drops log records.

**Fix (final — on `poc/java-aws-batch`):** Built a dedicated `SdkLoggerProvider` directly from `OTEL_*` environment variables using the OTel Java SDK, bypassing the Spring bean entirely. See `BatchTelemetryApplication.buildLoggerProvider()`.

---

### Problem 5 — Firelens `service_name=unknown_service` (Now Resolved ✅)

**Symptom:** On Firelens branch, logs arrived in Loki with `service_name=unknown_service` regardless of configuration.

**Root cause:** The OpenAPM OTel Collector maps the OTLP **resource attribute** `service.name` → Loki stream label `service_name`. Firelens with the standard `aws-for-fluent-bit` image sends app stdout as raw OTLP log records with **empty resource attributes**. The `add_label` ECS option only adds an HTTP-level Loki stream label — the Collector ignores it when building `service_name`.

**Attempts that failed:**

| Attempt | Result |
|---|---|
| `add_label: "service.name sre-batch-telemetry-java"` | Rejected by ECS (dot in key name breaks ECS JSON options) |
| `add_label: "service_name sre-batch-telemetry-java"` | Sets HTTP stream label, not OTLP resource attr — collector ignores it |
| `OTEL_SERVICE_NAME` env var on app container | Only affects traces/metrics; Firelens sidecar has no access to app env vars |
| `aws-for-fluent-bit:stable` image | Ships Fluent Bit 1.9.x — opentelemetry plugin has no log support (metrics only) |

**Solution (from R&D team — `notification-manager-dynamic-routed`):**

1. Use `aws-for-fluent-bit:init-3.2.4` — the `init` variant downloads a custom Fluent Bit config from S3 at container startup via the `aws_fluent_bit_init_s3_1` env var. This works on Fargate because it's a **container-level** S3 download (unlike `FirelensConfiguration` S3 options which are ECS-infrastructure-only and Fargate-blocked).
2. Custom config uses `record_modifier` filter to add `service_name`, `openapm_product_name`, `region` as Fluent Bit record fields.
3. `logs_body_key_attributes true` on the OpenTelemetry output promotes those record fields to **OTLP log resource attributes**.
4. OpenAPM Collector reads the OTLP resource attribute `service_name` → correct Loki stream label.

**Confirmed working:** Job `91ef7036` — Loki query `{service_name="sre-batch-telemetry-java"}` returns logs with correct indexed labels.

---

### Problem 6 — CloudFormation Deployment Failures (Firelens Stack)

Multiple errors during CF deployment of the Firelens job definition stack:

| Error | Root Cause | Fix Applied |
|---|---|---|
| `EarlyValidation::ResourceExistenceCheck` | `/aws/batch/.../app` log group already exists in another stack | Removed both log group resources from the template — CloudWatch auto-creates them |
| `Unresolved resource dependencies [FirelensLogGroup]` | Outputs block still referenced the deleted resource | Replaced `!Ref FirelensLogGroup` with literal string in Outputs |
| `Fargate resource requirements (1.25 vCPU) not valid` | app (1 vCPU) + sidecar (0.25 vCPU) = 1.25 total — not a valid Fargate size | Set app to 0.75 vCPU + 1920 MiB → total = exactly 1 vCPU / 2048 MiB |
| `!Sub` not evaluated by EarlyValidation hook | CF EarlyValidation does not resolve intrinsic functions | Hardcoded log group name as literal string |
| GitHub CDN cache | `curl` from raw.githubusercontent.com served stale template | Used CloudShell file upload button to bypass CDN |

---

### Problem 7 — ECR Access Blocked

**Symptom:** Could not push custom Docker images.

**Root cause:** Org IAM policy has explicit deny on `ecr:GetAuthorizationToken`.

**Fix:** Used public ECR images (`public.ecr.aws/amazoncorretto/amazoncorretto:17`) and fetched the JAR from S3 at container startup via `aws s3 cp`. No custom image or ECR access needed.

---

## 8. What Is Still Pending

### 8.1 Both Approaches Now Confirmed Working

All pending validation is complete:

| Branch | Approach | Logs `service_name` | Job confirmed |
|---|---|---|---|
| `poc/java-aws-batch` | Direct OTel Logback appender | ✅ `sre-batch-telemetry-java` | Multiple jobs |
| `poc/java-firelens-brian-approach` | Firelens + init image + S3 config | ✅ `sre-batch-telemetry-java` | Job `91ef7036` |

### 8.2 Architecture Decision with Brian

Two fully working paths forward for log routing in Batch jobs:

| Path | Description | `service_name` correct? | ECS Consistent? | Complexity |
|---|---|---|---|---|
| **A — Direct OTel** | OTel Logback appender → OTLP/HTTP direct | ✅ Yes | ❌ App-level change per service | Low |
| **B — Firelens (Brian's)** | `init` image + S3 config + `record_modifier` + `logs_body_key_attributes` | ✅ Yes | ✅ Yes | Medium |

Both paths are implemented and confirmed working. Path B matches ECS microservice patterns.

### 8.3 Python Library Packaging

`src/batch_otel/` is a working prototype. Still pending:
- Publish to internal PyPI / NICE artifact registry
- Add `pyproject.toml` and packaging metadata
- Add pip install instructions for consumers

### 8.4 Consumer CloudFormation Template

`cloudformation/consumer-job-definition.yaml` needs:
- Validation on AllowedValues for container image options
- Integration with VPC/subnet exports from shared master stacks
- IAM policy review for minimum required permissions per consumer job

### 8.5 Grafana Dashboard

No dashboard created yet:
- Custom dashboard for batch job metrics (`job_duration_seconds`, `job_items_processed_total`, `job_status`)
- Alert rule: `job_status=1` (failure)
- Correlation panel: trace + logs side by side in Grafana Explore

### 8.6 Multi-Region Validation

All testing done in `us-west-2` only. If Batch jobs run in other regions, the VPC Endpoint and OpenAPM endpoint configuration must be validated per region.

---

## 9. Brian's Firelens Approach — Evidence

Full evidence: [docs/FIRELENS-EVIDENCE.md](docs/FIRELENS-EVIDENCE.md)

### What Works

| Signal | Status | Notes |
|--------|--------|-------|
| Traces | ✅ Working | Micrometer direct OTLP — no Firelens involved |
| Metrics | ✅ Working | Micrometer direct OTLP — no Firelens involved |
| Logs delivery | ✅ Working | Via Firelens → OTLP → OpenAPM → Loki |
| Logs label | ✅ Correct | `service_name=sre-batch-telemetry-java` in Loki |

### How It Works (R&D Team Pattern)

The `init` image variant of `aws-for-fluent-bit` downloads a custom Fluent Bit config from S3 at container startup. The custom config uses `record_modifier` to inject `service_name` as a Fluent Bit record field, and `logs_body_key_attributes true` promotes those fields to OTLP log resource attributes — which the OpenAPM Collector then maps to the correct Loki stream label:

```
OTLP log record (from Firelens with init image + custom config)
  resourceLogs.resource.attributes = {
    service_name = "sre-batch-telemetry-java"   ← set by record_modifier + logs_body_key_attributes
    openapm_product_name = "sre-batch-telemetry"
    region = "us-west-2"
  }
  Result: collector maps service_name → correct Loki label ✅
```

### Critical Fluent Bit Version Discovery

`aws-for-fluent-bit:stable` ships **Fluent Bit 1.9.x** which has **no log support** in the `opentelemetry` output plugin — logs are silently dropped.

| Image | Fluent Bit version | Log support |
|---|---|---|
| `:stable` | 1.9.10 | ❌ Metrics only |
| `:3.3.0` | 5.0.3 | ✅ Logs work |
| `:init-3.2.4` | 4.x | ✅ Logs work + S3 config download |

### Comparison: Firelens (init image) vs Direct OTLP

| | Brian's Firelens (init image) | Working Solution (Direct OTLP) |
|---|---|---|
| `service_name` label | ✅ `sre-batch-telemetry-java` | ✅ `sre-batch-telemetry-java` |
| Trace-log correlation | ✅ `traceId` + `spanId` in every log | ✅ `traceId` + `spanId` in every log |
| ECS pattern consistency | ✅ Same as ECS microservices | ❌ App-level change required |
| Multi-container overhead | ❌ Extra sidecar container | ✅ Single container |
| Custom config (Fargate) | ❌ Blocked | N/A |

---

## 10. Infrastructure Deployed

### AWS Resources

| Resource | Name | CloudFormation Stack |
|---|---|---|
| IAM Execution Role | `sre-aws-batch-telemetry-dev-execution-role` | `sre-batch-iam-dev` |
| IAM Job Role | `sre-aws-batch-telemetry-dev-job-role` | `sre-batch-iam-dev` |
| Security Group | `sg-0cf31b827483e31fc` | `sre-batch-sg-dev` |
| Compute Environment | `sre-aws-batch-telemetry-dev-ce` | `sre-batch-ce-dev` |
| Job Queue | `sre-aws-batch-telemetry-dev-queue` | `sre-batch-jq-dev` |
| Job Definition (Java) | `sre-batch-telemetry-java-dev-job` | `sre-batch-java-dev-jobdef` |
| Job Definition (Firelens) | `sre-batch-telemetry-java-dev-firelens-job` | `sre-batch-java-firelens-jobdef` |
| S3 Bucket | `sre-batch-telemetry-code-723346695882` | (pre-existing) |
| CloudWatch Log Group | `/aws/batch/sre-batch-telemetry-java/app` | `sre-batch-java-dev-jobdef` |
| VPC Endpoint | `vpce-07577bcf5fb4edc78` | (pre-existing, shared) |

### Network

| Item | Value |
|---|---|
| VPC | `vpc-0693b34275513631c` (`shared_eks`, 10.0.0.0/21) |
| Subnets | `subnet-0cdc843ec821d3ea5` (2a), `subnet-071fb611b8b3abf42` (2b), `subnet-00c9a34807a6d3742` (2c) |
| OpenAPM Endpoint | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` |
| Protocol | OTLP/HTTP (`http/protobuf`) — port 4318 only |

### Environment Variables (Java Job Definition)

| Variable | Value |
|---|---|
| `OTEL_SERVICE_NAME` | `sre-batch-telemetry-java` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` |
| `OTEL_RESOURCE_ATTRIBUTES` | `environment=mon-sandbox,account.id=723346695882,openapm_product_name=sre-batch-telemetry,service_name=sre-batch-telemetry-java,region=us-west-2` |

---

## 11. Consumer Onboarding

### Java Consumers

1. Add dependencies from `java-poc/pom.xml` (use `opentelemetry-bom:1.39.0`)
2. Copy `buildLoggerProvider()` from `BatchTelemetryApplication.java`
3. Call `OpenTelemetryAppender.install(...)` at startup
4. Deploy using `cloudformation/batch-job-definition-java.yaml` as template
5. Set the 4 required environment variables in your job definition

### Python Consumers

```python
from batch_otel import init_telemetry, shutdown_telemetry

def main():
    otel = init_telemetry()  # reads OTEL_* env vars automatically

    with otel.tracer.start_as_current_span("my-job"):
        otel.logger.info("Processing started")
        # ... your job logic ...
        otel.logger.info("Processing complete")

    shutdown_telemetry(otel)  # flushes all signals before exit

if __name__ == "__main__":
    main()
```

### Required Environment Variables (Both Languages)

```bash
OTEL_SERVICE_NAME=your-service-name
OTEL_EXPORTER_OTLP_ENDPOINT=https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_RESOURCE_ATTRIBUTES=environment=mon-sandbox,account.id=723346695882,openapm_product_name=your-product,service_name=your-service-name,region=us-west-2
```

---

## 12. Repository Structure

```
sre-aws-batch-telemetry-openapm/
├── README.md                                      # This file — full POC overview
├── Makefile
├── cloudformation/
│   ├── master-stack.yaml                          # Nested stack orchestrator
│   ├── batch-compute-environment.yaml             # Fargate compute environment
│   ├── batch-job-queue.yaml                       # Job queue
│   ├── batch-job-definition.yaml                  # Python POC job definition
│   ├── batch-job-definition-java.yaml             # Java POC job definition (working)
│   ├── batch-job-definition-java-firelens.yaml    # Brian's Firelens approach (evidence)
│   ├── batch-job-definition-firelens.yaml         # Python Firelens alternative
│   ├── consumer-job-definition.yaml               # Generic template for consumers
│   ├── iam-roles.yaml                             # Execution + Job roles
│   └── security-groups.yaml                       # SG with OTLP ports
├── docker/
│   ├── Dockerfile                                 # Not used (ECR blocked)
│   ├── Dockerfile.base
│   ├── requirements.txt
│   └── fluent-bit/
│       ├── batch-fluent-bit.conf                  # Custom FB config (Fargate-blocked)
│       ├── fluent-bit.conf
│       └── parsers.conf
├── docs/
│   ├── architecture.md
│   ├── CONSOLE-DEPLOYMENT-GUIDE.md                # Step-by-step CloudShell guide
│   ├── CONSUMER-ONBOARDING.md
│   ├── FIRELENS-EVIDENCE.md                       # Brian's Firelens approach evidence
│   ├── JAVA-POC-SUMMARY.md                        # Java POC full story
│   └── PYTHON-POC-SUMMARY.md                      # Python POC full story
├── examples/
│   ├── simple_job.py                              # Minimal new job example
│   └── existing_job_retrofit.py                   # Retrofit existing Python job
├── java-poc/
│   ├── pom.xml                                    # Spring Boot 3.3.13, Java 17
│   └── src/main/
│       ├── java/com/nice/sre/batch/
│       │   └── BatchTelemetryApplication.java     # Main job + SdkLoggerProvider
│       └── resources/
│           ├── application.yml                    # Micrometer config
│           └── logback-spring.xml                 # CONSOLE + OTelAppender
├── scripts/
│   ├── build-and-deploy-java.sh
│   ├── build-and-push.sh
│   ├── deploy.sh
│   ├── submit-job.sh
│   ├── upload-code-to-s3.sh
│   └── cleanup.sh
├── src/
│   ├── batch_job.py                               # Python POC job
│   └── batch_otel/
│       ├── __init__.py
│       ├── instrumentation.py                     # OTel SDK setup (traces+metrics+logs)
│       └── requirements.txt
└── tests/
    └── test_batch_job.py
```

---

## Quick Reference

### Grafana Queries

| Signal | Working Solution | Firelens Branch |
|---|---|---|
| Traces | `{service.name="sre-batch-telemetry-java"}` in Tempo | Same ✅ |
| Metrics | `{service_name="sre-batch-telemetry-java"}` in Mimir | Same ✅ |
| Logs | `{service_name="sre-batch-telemetry-java"}` in Loki | `{service_name="unknown_service"}` ⚠️ |

### CloudShell Commands

```bash
# Check job status
aws batch describe-jobs \
  --jobs <JOB_ID> --region us-west-2 \
  --query 'jobs[0].[status,statusReason]' --output text

# Submit Java job (working solution)
aws batch submit-job \
  --job-name sre-batch-java-test \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-java-dev-job \
  --region us-west-2

# Submit Firelens job (Brian's approach — evidence)
aws batch submit-job \
  --job-name sre-batch-firelens-evidence \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-java-dev-firelens-job \
  --region us-west-2

# View CloudWatch app logs
aws logs tail /aws/batch/sre-batch-telemetry-java/app \
  --region us-west-2 --since 30m --format short

# Describe CF stack events (for debugging)
aws cloudformation describe-stack-events \
  --stack-name <STACK_NAME> --region us-west-2 \
  --query 'StackEvents[?contains(ResourceStatus,`FAILED`)].[ResourceType,LogicalResourceId,ResourceStatusReason]' \
  --output text
```

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| OTLP export timeout | VPC Endpoint SG missing inbound rule | Add TCP 4318 inbound from VPC CIDR |
| `UNAVAILABLE` on port 4317 | gRPC not supported through this VPC Endpoint | Use `http/protobuf` on port 4318 |
| `service_name=unknown_service` in Loki | Spring Boot 3.3 `OpenTelemetry` bean has no-op logger | Use `buildLoggerProvider()` pattern |
| ECR push denied | Org IAM deny on `ecr:GetAuthorizationToken` | Use public ECR image + S3 code fetch |
| CF `EarlyValidation` failure | Log group already exists in another stack | Remove log group resource from template |
| Fargate task size invalid | vCPU total not a valid Fargate size (e.g. 1.25) | Adjust container vCPU so total = 0.5/1/2/4 |
| No data in Grafana | Missing required labels | Ensure `openapm_product_name` and `service_name` in resource attributes |

---

*Internal POC — SRE Team, May 2026*

# AWS Batch OpenAPM Telemetry POC — Director-Level Summary

> **Prepared by:** SRE Team  
> **Account:** mon-sandbox (`723346695882`) | **Region:** `us-west-2`  
> **Repository:** `sre-aws-batch-telemetry-openapm`  
> **Date:** May 2026

---

## Executive Summary

This POC proves that AWS Batch jobs (Java and Python) running on Fargate can send all three observability signals — **Traces, Metrics, and Logs** — into NICE's central OpenAPM platform (Grafana / Tempo / Mimir / Loki) with correct service identity labels. The work involved navigating significant infrastructure constraints, multiple failed approaches, an architectural consultation with Architect, and ultimately delivering **two fully working implementations**.

---

## 1. Why We Did This — The Problem

### The Gap

AWS Batch jobs are used across multiple teams at NICE for background workloads (data pipelines, reporting, scheduled processing). Until this POC, there was **no standard mechanism** for Batch jobs to send observability data to OpenAPM.

### Impact of the Gap (Before This POC)

| Signal | Before POC | Impact |
|--------|-----------|--------|
| Traces | ❌ None | Engineers had no visibility into which job phase failed or was slow |
| Metrics | ❌ None | No job duration, item count, or success/failure rates in dashboards |
| Logs | ❌ Only raw CloudWatch | No correlation between logs and traces; no structured search in Grafana |

> **Bottom line:** When a Batch job failed or was slow, engineers had to grep raw CloudWatch logs with no context. There was no way to correlate a log line to a trace, and no metrics to alert on job failures.

---

## 2. How Many POCs We Tried — Overview

We implemented and tested **3 POCs across 3 Git branches**:

| # | Branch | Language | Approach | Outcome |
|---|--------|----------|----------|---------|
| POC 1 | `poc/python-aws-batch` | Python 3.11 | OTel SDK direct OTLP/HTTP | ✅ All 3 signals working |
| POC 2 | `poc/java-aws-batch` | Java 17 / Spring Boot 3.3 | Micrometer + OTel Logback Appender (direct) | ✅ All 3 signals working — **Primary Solution** |
| POC 3 | `poc/java-firelens-architect-approach` | Java 17 / Spring Boot 3.3 | Micrometer + Firelens sidecar | ✅ All 3 signals working — **Architect's Recommended Pattern** |

---

## 3. What Architect Suggested — Architect Recommendation

Architect **Architect** reviewed our initial approaches and recommended the **Firelens sidecar pattern** for log routing, citing consistency with how all ECS microservices operate at NICE.

### Architect's Recommendation (Summary)

> "Use Firelens for log routing in Batch — the same pattern used by ECS microservices. Don't route logs from inside the app; use the sidecar."

### References Architect Shared

| Reference | Link |
|-----------|------|
| AWS Batch multi-container support (Feb 2024) | AWS What's New announcement |
| Firelens support for AWS Batch (Apr 2025) | AWS What's New announcement |
| Open APM Logs Migration Guide (internal) | Confluence: WFM space / Log Routing with FireLens |

### What Architect's Firelens Architecture Looks Like

```
AWS Batch Fargate Task (1 vCPU / 2048 MiB total)
├── app container  (0.75 vCPU / 1920 MiB)
│     ├── Micrometer Tracing → OTLP/HTTP → Tempo  ✅
│     ├── Micrometer OTLP Registry → OTLP/HTTP → Mimir  ✅
│     └── stdout (JSON logs) ─────────────────────┐
│                                                  ↓
└── log_router sidecar (0.25 vCPU / 128 MiB)
      [aws-for-fluent-bit:init-3.2.4]
      ├── record_modifier → injects service_name, region
      ├── opentelemetry output → OTLP/HTTP → Loki  ✅ correct service_name
      └── cloudwatch_logs output → CloudWatch (backup)
```

---

## 4. Infrastructure Constraints We Had to Work Around

These were not optional complications — they are **hard platform constraints** that defined our entire solution approach.

| Constraint | Detail | How It Affected Us |
|-----------|--------|--------------------|
| **No ECR access** | Org IAM has explicit deny on `ecr:GetAuthorizationToken` in mon-sandbox | Could not push custom Docker images. Had to use public ECR images + S3 JAR download |
| **MFA required locally** | All AWS CLI commands required MFA | All deployments done via AWS CloudShell. No local `aws` CLI deploys |
| **OpenAPM port 4318 only** | gRPC (port 4317) returns `UNAVAILABLE` through VPC Endpoint | Forced to use OTLP/HTTP (`http/protobuf`) on port 4318 for all signals |
| **No custom Firelens config from S3 (standard)** | S3-sourced Fluent Bit configs via `FirelensConfiguration` are blocked on Fargate — it is an ECS-infrastructure-only feature | Had to find an alternative (the `init` image variant) |
| **VPC Endpoint SG — no inbound rules** | The shared VPC Endpoint Security Group had zero inbound rules at the start | All OTLP traffic was silently blocked; zero telemetry arrived in Grafana until fixed |

---

## 5. Step-by-Step: How We Built and Deployed Each POC

### POC 1 — Python (`poc/python-aws-batch`)

**Goal:** Prove feasibility with the simplest possible approach.

#### Build
- Python 3.11 job using the OpenTelemetry Python SDK
- Reusable `batch_otel` library (`src/batch_otel/`) wrapping traces, metrics, logs setup
- No Docker build needed — code zipped and uploaded to S3

#### Deploy
```bash
# Upload Python code to S3 (from CloudShell)
aws s3 cp src/ s3://sre-batch-telemetry-code-723346695882/python/ --recursive

# Deploy CloudFormation stacks
aws cloudformation deploy --stack-name sre-batch-iam-dev \
  --template-file cloudformation/iam-roles.yaml --capabilities CAPABILITY_NAMED_IAM

aws cloudformation deploy --stack-name sre-batch-ce-dev \
  --template-file cloudformation/batch-compute-environment.yaml

aws cloudformation deploy --stack-name sre-batch-jq-dev \
  --template-file cloudformation/batch-job-queue.yaml

aws cloudformation deploy --stack-name sre-batch-python-dev-jobdef \
  --template-file cloudformation/batch-job-definition.yaml
```

#### Validate
```bash
aws batch submit-job \
  --job-name sre-batch-python-test \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-python-dev-job \
  --region us-west-2

# Confirmed job: 677348fb — SUCCEEDED
# Grafana: Traces in Tempo, Metrics in Mimir, Logs in Loki — all with correct service_name
```

**Result:** ✅ All 3 signals confirmed working. Job `677348fb` SUCCEEDED.

---

### POC 2 — Java Direct OTLP (`poc/java-aws-batch`) — Primary Solution ✅

**Goal:** Prove the same for Java Spring Boot, which is the primary language for NICE Batch workloads.

#### Build
```bash
# Build fat JAR locally
cd java-poc
mvn clean package -DskipTests
# → target/batch-telemetry.jar (~23 MB)
```

#### Deploy
```bash
# Upload to CloudShell (via Actions → Upload file), then:
aws s3 cp ~/batch-telemetry.jar \
  s3://sre-batch-telemetry-code-723346695882/java/batch-telemetry.jar

# Deploy job definition CloudFormation
aws cloudformation deploy \
  --stack-name sre-batch-java-dev-jobdef \
  --template-file cloudformation/batch-job-definition-java.yaml \
  --parameter-overrides \
    ExecutionRoleArn=arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-execution-role \
    JobRoleArn=arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-job-role
```

#### Submit and Validate
```bash
aws batch submit-job \
  --job-name sre-batch-java-test \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-java-dev-job \
  --region us-west-2

# Confirmed job: a0dd88f9 — SUCCEEDED
```

**Grafana Validation Queries:**

| Signal | Grafana Query | Result |
|--------|--------------|--------|
| Traces | `{service.name="sre-batch-telemetry-java"}` in Tempo | ✅ 4 spans per job |
| Metrics | `job_items_processed_total{service_name="sre-batch-telemetry-java"}` in Mimir | ✅ Increments per run |
| Logs | `{service_name="sre-batch-telemetry-java"}` in Loki | ✅ Structured JSON with traceId/spanId |

**Result:** ✅ All 3 signals confirmed working.

---

### POC 3 — Java Firelens / Architect's Approach (`poc/java-firelens-architect-approach`) ✅

**Goal:** Implement Architect's recommended Firelens sidecar pattern and confirm it works for Batch.

#### Build
- Same Java JAR as POC 2
- Additional Fluent Bit config file (`batch-fluent-bit.conf`) uploaded to S3
- Multi-container CloudFormation job definition (`batch-job-definition-java-firelens.yaml`)

#### Deploy
```bash
# Upload Fluent Bit config to S3
aws s3 cp docker/fluent-bit/batch-fluent-bit.conf \
  s3://sre-batch-telemetry-code-723346695882/fluent-bit/batch-fluent-bit.conf

# Deploy Firelens job definition
aws cloudformation deploy \
  --stack-name sre-batch-java-firelens-jobdef \
  --template-file cloudformation/batch-job-definition-java-firelens.yaml \
  --parameter-overrides \
    ExecutionRoleArn=arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-execution-role \
    JobRoleArn=arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-job-role
```

#### Submit and Validate
```bash
aws batch submit-job \
  --job-name sre-batch-java-firelens-test \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-java-dev-firelens-job \
  --region us-west-2

# Confirmed job: 91ef7036 — SUCCEEDED
```

**Grafana Validation Queries:**

| Signal | Grafana Query | Result |
|--------|--------------|--------|
| Traces | `{service.name="sre-batch-telemetry-java"}` in Tempo | ✅ |
| Metrics | `job_items_processed_total{service_name="sre-batch-telemetry-java"}` in Mimir | ✅ |
| Logs | `{service_name="sre-batch-telemetry-java"}` in Loki | ✅ Correct label |

**Result:** ✅ All 3 signals confirmed working with correct `service_name` label.

---

## 6. Blockers We Faced — Full Detail

### Blocker 1 — VPC Endpoint Security Group Had No Inbound Rules

**Symptom:** All OTLP exports timed out silently. Job completed successfully (exit code 0) but zero telemetry appeared in Grafana.

**Root cause:** The shared VPC Endpoint Security Group (`sg-0cf31b827483e31fc`) had no inbound rules. Batch Fargate tasks in the VPC were routed to the endpoint but all TCP connections were rejected.

**Fix applied:**
```
Added inbound rules to VPC Endpoint SG:
  - TCP 4317 from 10.0.0.0/21 (VPC CIDR)
  - TCP 4318 from 10.0.0.0/21
  - TCP 443  from 10.0.0.0/21
```

**Time lost:** ~1 day debugging app configuration before checking the network layer.

---

### Blocker 2 — gRPC (Port 4317) Does Not Work Through VPC Endpoint

**Symptom:** With `OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, all exporters returned `UNAVAILABLE`.

**Root cause:** The OpenAPM VPC Endpoint uses HTTP/1.1. gRPC requires HTTP/2, which this endpoint does not support.

**Fix applied:**
```bash
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf  # switched from grpc
# Port 4318 instead of 4317
```

---

### Blocker 3 — Java Logs Showing `service_name=unknown_service` (Attempt 1)

**Symptom:** Traces and Metrics had correct `service_name`; Logs appeared in Loki under `unknown_service`.

**Root cause:** `OpenTelemetryAppender` defaults to `GlobalOpenTelemetry`, which is a no-op when no SDK is registered globally. Log records were silently discarded.

**Fix attempted:** Called `OpenTelemetryAppender.install(springOpenTelemetryBean)` — passed the Spring Boot auto-configured OTel bean.

**Result:** Still `unknown_service`. See Blocker 4.

---

### Blocker 4 — Logs Still `service_name=unknown_service` After Spring Bean Install (Attempt 2)

**Symptom:** Even after installing the Spring Boot `OpenTelemetry` bean into the Logback appender, logs still arrived in Loki with `unknown_service`.

**Root cause (Spring Boot 3.3 specific — non-obvious):**  
Spring Boot 3.3's auto-configured `OpenTelemetry` bean is built exclusively for **Micrometer Tracing**. Its internal `SdkLoggerProvider` is a **no-op stub** — it accepts log records but silently drops them. This is undocumented behavior.

**Final fix (breakthrough):**  
Built a dedicated `SdkLoggerProvider` directly from `OTEL_*` environment variables using the raw OTel Java SDK — bypassing the Spring bean entirely. This must be installed **before** Spring's application context starts logging.

```java
// In BatchTelemetryApplication — runs BEFORE Spring context
private static SdkLoggerProvider buildLoggerProvider() {
    String endpoint = System.getenv("OTEL_EXPORTER_OTLP_ENDPOINT");
    String serviceName = System.getenv("OTEL_SERVICE_NAME");

    Resource resource = Resource.getDefault().merge(
        Resource.create(Attributes.of(
            AttributeKey.stringKey("service.name"), serviceName,
            AttributeKey.stringKey("service_name"), serviceName,
            AttributeKey.stringKey("environment"), System.getenv("ENVIRONMENT"),
            AttributeKey.stringKey("region"), System.getenv("AWS_REGION"),
            AttributeKey.stringKey("account.id"), System.getenv("AWS_ACCOUNT_ID"),
            AttributeKey.stringKey("openapm_product_name"), System.getenv("OPENAPM_PRODUCT_NAME")
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

// Install before Spring starts
OpenTelemetryAppender.install(
    OpenTelemetrySdk.builder().setLoggerProvider(buildLoggerProvider()).build()
);
```

---

### Blocker 5 — Firelens Logs `service_name=unknown_service` (3 Failed Attempts)

**Symptom:** When using Firelens (Architect's recommended approach), logs arrived with `service_name=unknown_service` regardless of configuration.

**Root cause:**  
OpenAPM's OTel Collector maps the OTLP **resource attribute** `service.name` to the Loki stream label `service_name`. Standard Firelens sends app stdout as raw OTLP log records with **empty resource attributes**. The `add_label` ECS option adds an HTTP-level stream label only — the Collector ignores it for `service_name` mapping.

**Attempts that failed:**

| Attempt | Why It Failed |
|---------|--------------|
| `add_label: "service.name sre-batch-telemetry-java"` | ECS rejects keys with dots in the name |
| `add_label: "service_name sre-batch-telemetry-java"` | Sets HTTP stream label only — Collector ignores it for OTLP resource attr mapping |
| `OTEL_SERVICE_NAME` env var on app container | Fluent Bit sidecar has no access to the app container's env vars |
| `aws-for-fluent-bit:stable` image | Fluent Bit 1.9.x — the `opentelemetry` output plugin in this version has **no log support** (metrics only). Logs were silently dropped. |
| `FirelensConfiguration` S3 config source | ECS infrastructure feature — blocked on Fargate |

**Breakthrough — R&D Team Pattern (`notification-manager-dynamic-routed`):**

The solution came from studying how the R&D team's `saas-platform-ms-notification-manager-dynamic-routed` service solved the same problem for ECS:

1. Use `aws-for-fluent-bit:init-3.2.4` — the `init` image variant downloads a custom Fluent Bit config from S3 at **container startup** via the `aws_fluent_bit_init_s3_1` environment variable. This is a container-level operation (not ECS-infrastructure-level), so it works on Fargate.

2. Custom config uses `record_modifier` filter to inject `service_name` etc. as Fluent Bit record fields.

3. `logs_body_key_attributes true` on the OpenTelemetry output promotes those record fields to **OTLP log resource attributes**.

4. OpenAPM Collector reads `service_name` as an OTLP resource attribute → maps it correctly to the Loki stream label.

**Custom Fluent Bit config (key sections):**
```ini
[FILTER]
  Name    record_modifier
  Match   *
  Record  service_name         ${SERVICE_NAME}
  Record  openapm_product_name ${PRODUCT_NAME}
  Record  region               ${REGION}

[OUTPUT]
  Name                      opentelemetry
  Match                     *
  Host                      ${APM_HOST}
  Port                      4318
  logs_uri                  /v1/logs
  TLS                       On
  logs_body_key             $log
  logs_body_key_attributes  true   ← this is the critical flag
```

---

### Blocker 6 — CloudFormation Deployment Failures (Firelens Stack)

Multiple CloudFormation errors were hit during Firelens stack deployment:

| Error | Root Cause | Fix Applied |
|-------|-----------|-------------|
| `EarlyValidation::ResourceExistenceCheck` | CloudWatch log group `/aws/batch/.../app` already existed, owned by another stack | Removed log group resources from the template — CloudWatch auto-creates them |
| `Unresolved resource dependencies [FirelensLogGroup]` | Outputs block still referenced the deleted log group resource | Replaced `!Ref FirelensLogGroup` with a hardcoded literal string |
| `Fargate resource requirements (1.25 vCPU) not valid` | app (1.0 vCPU) + log_router sidecar (0.25 vCPU) = 1.25 — not a valid Fargate task size | Set app to **0.75 vCPU + 1920 MiB** → total = exactly 1 vCPU / 2048 MiB (valid Fargate size) |
| `!Sub` not evaluated by EarlyValidation hook | CF EarlyValidation hook does not resolve intrinsic functions at validation time | Hardcoded log group name as a literal string in the template |
| Stale template from GitHub CDN | `curl` from raw.githubusercontent.com served a cached (older) version of the template | Used CloudShell's built-in **"Actions → Upload file"** button to bypass GitHub CDN caching |

---

### Blocker 7 — ECR Access Blocked

**Symptom:** Could not push custom-built Docker images.

**Root cause:** Org IAM policy has an explicit `Deny` on `ecr:GetAuthorizationToken` in mon-sandbox account.

**Fix:**  
- Used public ECR image: `public.ecr.aws/amazoncorretto/amazoncorretto:17`
- Fat JAR uploaded to S3; container entrypoint downloads it at runtime:
  ```bash
  aws s3 cp s3://sre-batch-telemetry-code-723346695882/java/batch-telemetry.jar /app.jar && java -jar /app.jar
  ```
- No ECR push, no custom Docker image needed.

---

### Blocker 8 — Firelens init Image — Additional IAM and Config Errors

When switching to `aws-for-fluent-bit:init-3.2.4`, additional errors appeared:

| Error | Root Cause | Fix Applied |
|-------|-----------|-------------|
| `Could not parse arn: s3://...` | The `init` image requires the S3 path in **ARN format**, not S3 URI format | Changed `s3://bucket/key` → `arn:aws:s3:::bucket/key` |
| `s3:GetBucketLocation AccessDenied` | The Batch job IAM role was missing this S3 permission | Added `s3:GetBucketLocation` to the Job Role IAM policy |
| `CreateLogStream AccessDenied` | Job role missing CloudWatch Logs permissions | Added `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` to IAM |

---

## 7. Final Architecture — Both Working Solutions

### Solution A — Direct OTel (Primary, `poc/java-aws-batch`)

```
AWS Batch Fargate Task (1 vCPU / 2048 MiB)
└── Java container (amazoncorretto:17)
    │
    ├── Entrypoint: aws s3 cp .../batch-telemetry.jar → java -jar /app.jar
    ├── Spring Boot 3.3.13 (CommandLineRunner — no web server)
    │
    ├── Micrometer Tracing (OTel bridge)
    │     └── OTLPSpanExporter → OTLP/HTTP :4318/v1/traces → Tempo  ✅
    │
    ├── Micrometer OTLP Registry (push every 10s)
    │     └── OTLPMetricExporter → OTLP/HTTP :4318/v1/metrics → Mimir  ✅
    │
    └── Logback + OpenTelemetryAppender
          └── custom SdkLoggerProvider (from OTEL_* env vars)
                → OtlpHttpLogRecordExporter → OTLP/HTTP :4318/v1/logs → Loki  ✅

All traffic → VPC Endpoint (PrivateLink) → OpenAPM :4318
```

### Solution B — Firelens Sidecar (Architect's Pattern, `poc/java-firelens-architect-approach`)

```
AWS Batch Fargate Task (1 vCPU / 2048 MiB)
├── app container (amazoncorretto:17 — 0.75 vCPU / 1920 MiB)
│     ├── Micrometer Tracing → OTLP/HTTP :4318/v1/traces → Tempo  ✅
│     ├── Micrometer OTLP Registry → OTLP/HTTP :4318/v1/metrics → Mimir  ✅
│     └── stdout (JSON logs) ─────────────────────────────────────┐
│                                                                  ↓
└── log_router sidecar (aws-for-fluent-bit:init-3.2.4 — 0.25 vCPU / 128 MiB)
      Downloads custom config from S3 at startup (aws_fluent_bit_init_s3_1)
      ├── record_modifier filter: adds service_name, region, openapm_product_name
      ├── opentelemetry output (logs_body_key_attributes true)
      │     → OTLP/HTTP :4318/v1/logs → Loki  ✅ correct service_name
      └── cloudwatch_logs → /aws/batch/sre-batch-telemetry-java (backup)
```

---

## 8. Validation Results — Confirmed Working

### Confirmed Job Runs

| Job ID | Branch / Approach | Traces | Metrics | Logs `service_name` | Status |
|--------|-------------------|--------|---------|---------------------|--------|
| `677348fb` | Python direct OTel | ✅ | ✅ | ✅ correct | SUCCEEDED |
| `a0dd88f9` | Java direct OTel (`poc/java-aws-batch`) | ✅ | ✅ | ✅ correct | SUCCEEDED |
| `91ef7036` | Java Firelens init image (`poc/java-firelens-architect-approach`) | ✅ | ✅ | ✅ correct | SUCCEEDED |
| `7dd4a30b` | Java Firelens — metrics step boundary fix | ✅ | ✅ | ✅ correct | SUCCEEDED |

### What Is Validated in Grafana (per run)

**Traces (Tempo):**
- 4 spans per job with correct parent-child hierarchy:
  ```
  batch-job-execution  (root, ~60s)
    ├── fetch-data      (~5s)
    ├── process-data    (~5s)
    └── write-results   (~5s)
  ```
- Span tags: `job.items_processed`, `job.status`, `data.count`, `data.processed`

**Metrics (Mimir):**
- `job_duration_seconds` (Timer — `_sum`, `_count`, `_max`)
- `job_items_processed_total` (Counter — increments per run)
- `job_status` (Gauge — `0`=success, `1`=error)
- All carry correct labels: `service_name`, `environment`, `region`, `account_id`, `openapm_product_name`

**Logs (Loki):**
- Structured JSON log lines with correct indexed stream labels
- `traceId` + `spanId` in every log record (enables one-click drill-down from Tempo trace → Loki logs in Grafana Explore)
- Additional fields: `code_filepath`, `code_function`, `code_lineno`, `code_namespace`

---

## 9. Comparison: Path A vs Path B

| | Path A — Direct OTel | Path B — Firelens (Architect's) |
|---|---|---|
| **`service_name` in Loki** | ✅ Correct | ✅ Correct |
| **Trace-log correlation** | ✅ `traceId` + `spanId` in logs | ✅ `traceId` + `spanId` in logs |
| **ECS microservice pattern consistency** | ❌ App-level change required per service | ✅ Same pattern as ECS microservices |
| **Container count** | 1 (simpler, cheaper) | 2 (app + log_router sidecar) |
| **ECR required** | ❌ No | ❌ No |
| **Complexity** | Low (no sidecar to manage) | Medium (sidecar, S3 config, IAM) |
| **Recommended for** | New Batch services starting fresh | Teams already on ECS Firelens pattern |

---

## 10. What Is Still Pending

| Item | Description | Priority |
|------|-------------|----------|
| **Architecture decision** | Architect needs to confirm which path (A or B) becomes the NICE standard for Batch telemetry | High |
| **Python library packaging** | `src/batch_otel/` works but needs `pyproject.toml`, versioning, and publishing to NICE internal PyPI | Medium |
| **Consumer CloudFormation template** | `cloudformation/consumer-job-definition.yaml` needs validation, VPC integration, and IAM review | Medium |
| **Grafana dashboard** | No standard dashboard yet for Batch job metrics / alerts | Medium |
| **Alert rule** | `job_status=1` should trigger a Grafana alert | Medium |
| **Multi-region validation** | All testing done in `us-west-2` only | Low |

---

## 11. Infrastructure Deployed

| Resource | Name | Stack |
|----------|------|-------|
| IAM Execution Role | `sre-aws-batch-telemetry-dev-execution-role` | `sre-batch-iam-dev` |
| IAM Job Role | `sre-aws-batch-telemetry-dev-job-role` | `sre-batch-iam-dev` |
| Security Group | `sg-0cf31b827483e31fc` | `sre-batch-sg-dev` |
| Compute Environment | `sre-aws-batch-telemetry-dev-ce` | `sre-batch-ce-dev` |
| Job Queue | `sre-aws-batch-telemetry-dev-queue` | `sre-batch-jq-dev` |
| Job Definition (Java Direct) | `sre-batch-telemetry-java-dev-job` | `sre-batch-java-dev-jobdef` |
| Job Definition (Firelens) | `sre-batch-telemetry-java-dev-firelens-job` | `sre-batch-java-firelens-jobdef` |
| S3 Bucket | `sre-batch-telemetry-code-723346695882` | (pre-existing) |
| VPC Endpoint | `vpce-07577bcf5fb4edc78` | (pre-existing, shared) |

### Key Network Configuration

| Item | Value |
|------|-------|
| VPC | `vpc-0693b34275513631c` (shared_eks, 10.0.0.0/21) |
| OpenAPM Endpoint | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` |
| Protocol | OTLP/HTTP (`http/protobuf`) — port 4318 only (gRPC/4317 not supported) |

---

## 12. Complexity Justification for the Director

This POC appears straightforward on paper ("just send telemetry from a Batch job") but encountered compounding constraints that required architectural problem-solving at multiple layers:

| Layer | Blocker Encountered | Root Cause | Resolution Effort |
|-------|--------------------|-----------|--------------------|
| Network | VPC Endpoint SG blocked all OTLP | Missing inbound rules on shared infra | Investigation + coordination |
| Network | gRPC unsupported | VPC Endpoint HTTP-only | Protocol switch |
| IAM | ECR blocked org-wide | Explicit deny in IAM policy | S3 workaround architecture |
| IAM | Multiple S3/CloudWatch permissions missing | Minimal job role | Incremental IAM fixes |
| App (Java) | Spring Boot 3.3 no-op log provider | Undocumented Spring Boot limitation | Custom SdkLoggerProvider implementation |
| Sidecar | Fluent Bit 1.9.x drops logs silently | Plugin version mismatch | Image version research |
| Sidecar | Firelens `service_name` wrong | Architecture-level misunderstanding of how Collector maps attributes | R&D team pattern research |
| Sidecar | Fargate blocks S3 Firelens config | ECS vs Fargate feature difference | `init` image discovery |
| CloudFormation | 5 distinct CF deploy errors | Fargate sizing, log group conflicts, EarlyValidation quirks | Iterative debugging |
| Deploy tooling | GitHub CDN cache served stale template | CDN caching of raw.githubusercontent.com | CloudShell upload bypass |

> **Total blockers resolved: 10+, across network, IAM, application, container, CloudFormation, and deploy tooling layers.**

---

## 13. Quick Reference for Deployment

### Deploy Order (CloudShell, mon-sandbox account, us-west-2)

```bash
# 1. IAM roles
aws cloudformation deploy --stack-name sre-batch-iam-dev \
  --template-file cloudformation/iam-roles.yaml --capabilities CAPABILITY_NAMED_IAM

# 2. Security group
aws cloudformation deploy --stack-name sre-batch-sg-dev \
  --template-file cloudformation/security-groups.yaml

# 3. Compute environment
aws cloudformation deploy --stack-name sre-batch-ce-dev \
  --template-file cloudformation/batch-compute-environment.yaml

# 4. Job queue
aws cloudformation deploy --stack-name sre-batch-jq-dev \
  --template-file cloudformation/batch-job-queue.yaml

# 5a. Java job definition (Path A — Direct OTel)
aws cloudformation deploy --stack-name sre-batch-java-dev-jobdef \
  --template-file cloudformation/batch-job-definition-java.yaml \
  --parameter-overrides ExecutionRoleArn=<arn> JobRoleArn=<arn>

# 5b. Firelens job definition (Path B — Architect's Approach)
aws cloudformation deploy --stack-name sre-batch-java-firelens-jobdef \
  --template-file cloudformation/batch-job-definition-java-firelens.yaml \
  --parameter-overrides ExecutionRoleArn=<arn> JobRoleArn=<arn>
```

### Required Environment Variables (per Job Definition)

```bash
OTEL_SERVICE_NAME=your-service-name
OTEL_EXPORTER_OTLP_ENDPOINT=https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_RESOURCE_ATTRIBUTES=environment=mon-sandbox,account.id=723346695882,openapm_product_name=your-product,service_name=your-service-name,region=us-west-2
```

### Grafana Validation Queries

```
# Traces (Tempo Explorer)
{service.name="sre-batch-telemetry-java"}

# Metrics (Mimir / Prometheus)
job_items_processed_total{service_name="sre-batch-telemetry-java"}
job_duration_seconds_sum{service_name="sre-batch-telemetry-java"}
job_status{service_name="sre-batch-telemetry-java"}

# Logs (Loki Explorer)
{service_name="sre-batch-telemetry-java"}
```

---

*Full technical details: see [JAVA-POC-SUMMARY.md](JAVA-POC-SUMMARY.md), [FIRELENS-EVIDENCE.md](FIRELENS-EVIDENCE.md), and [CONSOLE-DEPLOYMENT-GUIDE.md](CONSOLE-DEPLOYMENT-GUIDE.md)*

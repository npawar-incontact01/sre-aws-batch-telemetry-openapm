# AWS Batch — OpenAPM Observability Standards & Guidelines

> **Document Type:** Engineering Standards & Guidelines  
> **Audience:** Platform Engineers, SRE, Service Owners, Tech Leads  
> **Scope:** All AWS Batch workloads at NICE sending telemetry to OpenAPM  
> **Account:** mon-sandbox (`723346695882`) | **Region:** `us-west-2`  
> **Status:** Approved — based on confirmed POC results (May 2026)

---

## Table of Contents

1. [Purpose](#1-purpose)
2. [Scope and Applicability](#2-scope-and-applicability)
3. [The Three Telemetry Signals — What You Must Send](#3-the-three-telemetry-signals--what-you-must-send)
4. [Approved Implementation Approaches](#4-approved-implementation-approaches)
5. [Mandatory Labels and Resource Attributes](#5-mandatory-labels-and-resource-attributes)
6. [Network and Infrastructure Requirements](#6-network-and-infrastructure-requirements)
7. [Protocol and Endpoint Standards](#7-protocol-and-endpoint-standards)
8. [IAM Permissions Required](#8-iam-permissions-required)
9. [Fluent Bit Version Requirements (Firelens Path)](#9-fluent-bit-version-requirements-firelens-path)
10. [Grafana Validation Checklist](#10-grafana-validation-checklist)
11. [What Is Explicitly Not Supported](#11-what-is-explicitly-not-supported)
12. [Known Platform Constraints](#12-known-platform-constraints)

---

## 1. Purpose

This document defines the **engineering standards and guidelines** for sending observability data (traces, metrics, logs) from AWS Batch jobs on Fargate to **OpenAPM** — NICE's central Grafana-based observability platform.

Prior to these guidelines, there was no standard pattern for Batch job telemetry. Engineers had only raw CloudWatch logs, no distributed traces, and no metrics for failure alerting. This document formalises the patterns validated through a comprehensive SRE POC.

---

## 2. Scope and Applicability

These guidelines apply to:

- All **AWS Batch on Fargate** workloads (Java, Python, or any OTel-compatible language)
- Jobs running in any NICE-managed AWS account with an OpenAPM VPC Endpoint
- Both new jobs and retrofits of existing Batch jobs

These guidelines do **not** cover:
- EC2-based Batch compute (different network configuration)
- AWS Lambda (separate observability pattern)
- ECS Services (use the existing ECS/Firelens pattern)

---

## 3. The Three Telemetry Signals — What You Must Send

All Batch jobs integrating with OpenAPM **must** send all three signals to be considered fully observable:

### 3.1 Traces → Tempo

**What they are:** Distributed traces showing the job execution flow, timing, and span attributes.

**Minimum requirement:**
- One root span covering the full job execution
- Child spans for each logical phase (e.g., fetch-data, process-data, write-results)
- Span attributes: at minimum `job.items_processed` and `job.status`

**Standard span hierarchy:**
```
<job-name>-execution           ← root span (full job duration)
  ├── fetch-data               ← data ingestion phase
  ├── process-data             ← processing / transformation phase
  └── write-results            ← output / persistence phase
```

### 3.2 Metrics → Mimir

**What they are:** Numerical measurements pushed on a regular interval, used for dashboards and alerts.

**Minimum required metrics:**

| Metric Name | Type | Description | Required? |
|------------|------|-------------|-----------|
| `job_duration_seconds` | Timer / Histogram | Total job wall-clock time | Yes |
| `job_items_processed_total` | Counter | Number of items handled | Yes |
| `job_status` | Gauge | `0` = success, `1` = error | Yes |

**Push interval:** 10 seconds (set `step: 10s` in Java; `export_interval_millis=10000` in Python).

**Important:** Always call `meterRegistry.close()` / `meter_provider.shutdown()` before the job exits to ensure the final metric step boundary is exported.

### 3.3 Logs → Loki

**What they are:** Structured log records with indexed stream labels, correlated to trace IDs.

**Requirements:**
- Logs must carry `service_name` as an **OTLP resource attribute** (not just a Loki stream label)
- Every log record must include `traceId` and `spanId` for trace-log correlation in Grafana Explore
- Log format: structured JSON (for Loki parsing)

---

## 4. Approved Implementation Approaches

Two approaches have been validated and approved. Choose based on your team's context:

### Approach A — Direct OTel SDK (Single Container)

**Recommended for:** New Java/Python services, teams where simplicity is preferred.

| | Detail |
|---|---|
| Language | Java (Micrometer + OTel Logback Appender) or Python (OTel SDK) |
| Containers | 1 (application only) |
| Log routing | App → OTLP/HTTP directly from within the application |
| ECR required | No (public ECR image + S3 code fetch) |
| Reference | `poc/java-aws-batch`, `poc/python-aws-batch` branches |

**Java-specific requirement:** Spring Boot 3.3's `OpenTelemetry` bean has **no `SdkLoggerProvider`**. You must build a dedicated `SdkLoggerProvider` from `OTEL_*` env vars and call `OpenTelemetryAppender.install(...)` before the Spring context begins logging. See [03-POC-JAVA-DUAL-CONTAINER.md](03-POC-JAVA-DUAL-CONTAINER.md) for the implementation pattern.

### Approach B — Firelens Sidecar (Dual Container)

**Recommended for:** Teams that want to match the ECS microservice Firelens pattern, or when standardising log routing infrastructure-side.

| | Detail |
|---|---|
| Language | Java (recommended); Python also supported |
| Containers | 2 — app container + `log_router` sidecar |
| Log routing | App stdout → Firelens Unix socket → Fluent Bit → OTLP/HTTP → Loki |
| ECR required | No |
| Fluent Bit image | `aws-for-fluent-bit:init-3.2.4` **(not `:stable`, not `:3.3.0`)** |
| Reference | `poc/java-firelens-architect-approach` branch |

---

## 5. Mandatory Labels and Resource Attributes

All three signals **must** carry the following resource attributes. These are how signals are identified and filtered in Grafana.

| Attribute Key | Example Value | Signal Mapping | Required |
|--------------|--------------|----------------|----------|
| `service.name` | `sre-batch-telemetry-java` | Traces: `service.name` label | **Yes** |
| `service_name` | `sre-batch-telemetry-java` | Metrics & Logs: `service_name` label | **Yes** |
| `environment` | `mon-sandbox` | All signals | **Yes** |
| `openapm_product_name` | `sre-batch-telemetry` | All signals | **Yes** |
| `region` | `us-west-2` | All signals | **Yes** |
| `account.id` | `723346695882` | All signals | Yes |

> **Note:** Both `service.name` (dot) and `service_name` (underscore) must be set. Tempo uses `service.name`; Mimir and Loki use `service_name`. Failure to set both results in signals appearing under `unknown_service`.

### How to Set These in Your Job Definition

```bash
# CloudFormation / Task Definition environment variables:
OTEL_SERVICE_NAME=your-service-name
OTEL_EXPORTER_OTLP_ENDPOINT=https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_RESOURCE_ATTRIBUTES=environment=mon-sandbox,account.id=723346695882,openapm_product_name=your-product,service_name=your-service-name,region=us-west-2
```

---

## 6. Network and Infrastructure Requirements

### 6.1 VPC Endpoint (PrivateLink)

All OTLP traffic **must** route through the OpenAPM VPC Endpoint. Direct internet egress to the OpenAPM endpoint is not supported.

| Item | Value (mon-sandbox) |
|------|-------------------|
| VPC Endpoint ID | `vpce-07577bcf5fb4edc78` |
| Private DNS | `apm-na1.mon-sandbox.nicecxone-sbx.com` |
| ENI IPs | `10.0.5.202`, `10.0.1.197`, `10.0.2.198` |

### 6.2 VPC Endpoint Security Group — Critical Rule

**The VPC Endpoint Security Group must have inbound rules for OTLP ports from the VPC CIDR.**

| Direction | Protocol | Port | Source | Purpose |
|-----------|----------|------|--------|---------|
| Inbound | TCP | 4318 | `10.0.0.0/21` (VPC CIDR) | OTLP/HTTP |
| Inbound | TCP | 443 | `10.0.0.0/21` | HTTPS |

> ⚠️ **Common failure:** The VPC Endpoint SG had no inbound rules at POC start. All OTLP calls timed out silently — jobs completed but zero telemetry appeared in Grafana. Always verify this rule before debugging application code.

### 6.3 Batch Task Security Group

The Batch task's own Security Group must allow:

| Direction | Protocol | Port | Destination | Purpose |
|-----------|----------|------|-------------|---------|
| Egress | TCP | 4318 | `0.0.0.0/0` | OTLP/HTTP to VPC Endpoint |
| Egress | TCP | 443 | `0.0.0.0/0` | S3 access (code fetch), ECR (if used) |

Existing SG for mon-sandbox: `sg-0cf31b827483e31fc`

### 6.4 Subnets

Jobs must run in **private subnets** with the VPC Endpoint attached.

| Subnet | AZ | CIDR |
|--------|----|------|
| `subnet-0cdc843ec821d3ea5` | us-west-2a | 10.0.0.0/23 |
| `subnet-071fb611b8b3abf42` | us-west-2b | 10.0.2.0/23 |
| `subnet-00c9a34807a6d3742` | us-west-2c | 10.0.4.0/23 |

---

## 7. Protocol and Endpoint Standards

| Protocol | Port | Supported? | Reason |
|----------|------|-----------|--------|
| OTLP/HTTP (`http/protobuf`) | **4318** | ✅ **Yes — use this** | VPC Endpoint is HTTP/1.1 compatible |
| OTLP/gRPC | 4317 | ❌ No | VPC Endpoint does not support HTTP/2 (gRPC) — returns `UNAVAILABLE` |

**Always set:**
```bash
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

**Signal-specific paths:**

| Signal | Path |
|--------|------|
| Traces | `https://<endpoint>:4318/v1/traces` |
| Metrics | `https://<endpoint>:4318/v1/metrics` |
| Logs | `https://<endpoint>:4318/v1/logs` |

---

## 8. IAM Permissions Required

### Execution Role (Fargate Task Execution Role)

The execution role is used by ECS/Fargate to start the container. Required permissions:

```json
{
  "Effect": "Allow",
  "Action": [
    "s3:GetObject",
    "s3:GetBucketLocation",
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
    "ecr:GetAuthorizationToken",
    "ecr:BatchCheckLayerAvailability",
    "ecr:GetDownloadUrlForLayer",
    "ecr:BatchGetImage"
  ]
}
```

> **Note:** `ecr:*` permissions are only needed if using a private ECR image. In mon-sandbox, ECR is blocked by org policy — use public ECR images instead.

### Job Role (Batch Job Role — attached to the running container)

The job role is used by the application code at runtime:

```json
{
  "Effect": "Allow",
  "Action": [
    "s3:GetObject",
    "s3:GetBucketLocation",
    "logs:CreateLogGroup",
    "logs:CreateLogStream",
    "logs:PutLogEvents"
  ]
}
```

> `s3:GetBucketLocation` is required by the Firelens `init` image when downloading the Fluent Bit config from S3.

---

## 9. Fluent Bit Version Requirements (Firelens Path)

If using **Approach B (Firelens)**, the Fluent Bit image version is critical.

| Image Tag | Fluent Bit Version | Log Support | S3 Config Download | Approved? |
|-----------|-------------------|-------------|-------------------|-----------|
| `:stable` | 1.9.10 | ❌ Metrics only | ❌ No | ❌ **Do not use** |
| `:3.3.0` | 5.0.3 | ✅ Yes | ❌ No (ECS-infra only, Fargate-blocked) | ❌ **Do not use** |
| `:init-3.2.4` | 4.x | ✅ Yes | ✅ Yes (container-level, Fargate-compatible) | ✅ **Use this** |

**Required Fluent Bit config settings for correct `service_name`:**

```ini
[FILTER]
  Name    record_modifier
  Match   *
  Record  service_name         ${SERVICE_NAME}
  Record  openapm_product_name ${PRODUCT_NAME}
  Record  region               ${REGION}

[OUTPUT]
  Name                      opentelemetry
  logs_body_key_attributes  true   ← CRITICAL: promotes fields to OTLP resource attributes
```

Without `logs_body_key_attributes true`, logs arrive with `service_name=unknown_service`.

---

## 10. Grafana Validation Checklist

After deploying and running a job, validate all three signals in Grafana:

### Traces (Grafana Explore → Tempo)

- [ ] Query: `{service.name="<your-service-name>"}`
- [ ] At least one trace appears with the correct job run timestamp
- [ ] Root span name matches your job name
- [ ] Child spans (fetch / process / write) are visible
- [ ] Span attributes (`job.items_processed`, `job.status`) are populated

### Metrics (Grafana Explore → Mimir / Prometheus)

- [ ] `job_duration_seconds_sum{service_name="<your-service-name>"}` returns a value
- [ ] `job_items_processed_total{service_name="<your-service-name>"}` increments per run
- [ ] `job_status{service_name="<your-service-name>"}` shows `0` (success)
- [ ] All metrics carry `environment`, `region`, `openapm_product_name` labels

### Logs (Grafana Explore → Loki)

- [ ] Query: `{service_name="<your-service-name>"}`
- [ ] Log lines appear with the correct job run timestamp
- [ ] Each log line contains `traceId` and `spanId` fields
- [ ] Clicking a trace in Tempo and navigating to Loki returns correlated logs

---

## 11. What Is Explicitly Not Supported

| Approach | Status | Reason |
|----------|--------|--------|
| OTLP/gRPC (port 4317) | ❌ Not supported | VPC Endpoint HTTP-only |
| Custom ECR images in mon-sandbox | ❌ Blocked | Org IAM explicit deny on `ecr:GetAuthorizationToken` |
| `FirelensConfiguration` S3 config source | ❌ Blocked on Fargate | ECS infrastructure feature, not available on Fargate |
| `aws-for-fluent-bit:stable` for logs | ❌ Silently drops logs | Fluent Bit 1.9.x opentelemetry plugin has no log support |
| `add_label` for `service.name` in Firelens | ❌ Does not work | ECS rejects dots in label keys; even if accepted, sets HTTP label not OTLP resource attr |
| Spring Boot 3.3 `OpenTelemetry` bean for logs | ❌ No-op logger provider | `SdkLoggerProvider` is absent in SB 3.3 auto-config — logs silently dropped |

---

## 12. Known Platform Constraints

| Constraint | Impact | Workaround |
|-----------|--------|-----------|
| No ECR access in mon-sandbox | Cannot push custom Docker images | Use public ECR images + S3 code fetch at container startup |
| MFA required for all AWS CLI operations | Cannot deploy from local machine without MFA session | All deployments use AWS CloudShell |
| Port 4317 (gRPC) unsupported via VPC Endpoint | Cannot use gRPC exporters | Use `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` on port 4318 |
| `FirelensConfiguration` S3 blocked on Fargate | Cannot use S3 config via ECS infrastructure method | Use `aws-for-fluent-bit:init-3.2.4` which downloads config at container level |
| Spring Boot 3.3 no-op log provider | Logs sent to Loki with `unknown_service` | Build dedicated `SdkLoggerProvider` from env vars (resolved in SB 3.4+) |
| Fargate task size must be valid combination | 1.25 vCPU is invalid (app 1.0 + sidecar 0.25) | Set app to 0.75 vCPU + 1920 MiB → total = 1 vCPU / 2048 MiB |

---

*Related documents:*
- [02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md](02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md) — Step-by-step integration guide for developers
- [03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md](03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md) — Architecture overview
- [04-POC-PYTHON-SINGLE-CONTAINER.md](04-POC-PYTHON-SINGLE-CONTAINER.md) — Python POC detail
- [05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md](05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md) — Java Firelens POC detail

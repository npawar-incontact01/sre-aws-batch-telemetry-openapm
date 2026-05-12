# Firelens Evidence — Brian's Approach: Confirmed Working ✅

## Background

Architect Brian recommended using **Firelens for log routing** in AWS Batch, consistent with how regular ECS microservices operate at NICE. This document captures the full journey: the initial blocker, the R&D team pattern that resolved it, and the confirmed results.

**References Brian shared:**
- [AWS Batch multi-container support (Feb 2024)](https://aws.amazon.com/about-aws/whats-new/2024/02/aws-batch-multi-container-jobs/)
- [Firelens support for AWS Batch (Apr 2025)](https://aws.amazon.com/about-aws/whats-new/2025/04/aws-batch-amazon-elastic-container-service-exec-firelens-log-router/)
- [Open APM Logs Migration Guide](https://nice-ce-cxone-prod.atlassian.net/wiki/spaces/WFM/pages/3188392157/Open+APM+Logs+Migration+Guide+Log+Routing+with+FireLens)

---

## Final Result ✅

All 3 signals confirmed working with correct `service_name` label using Brian's Firelens approach.

| Signal | Result | Grafana Query | Confirmed Job |
|--------|--------|--------------|---------------|
| **Traces** | ✅ Correct `service.name` in Tempo | `{service.name="sre-batch-telemetry-java"}` | `91ef7036` |
| **Metrics** | ✅ Correct `service_name` in Mimir | `job_items_processed_total{service_name="sre-batch-telemetry-java"}` | `7dd4a30b` |
| **Logs** | ✅ Correct `service_name` in Loki | `{service_name="sre-batch-telemetry-java"}` | `91ef7036` |

**Branch:** `poc/java-firelens-brian-approach`

---

## Architecture (Brian's Firelens Approach)

```
AWS Batch Fargate Task (1 vCPU / 2048 MiB)
├── app container (amazoncorretto:17 — 0.75 vCPU / 1920 MiB)
│     Spring Boot 3.3.13 CommandLineRunner
│     ├── Micrometer Tracing → OTLP/HTTP → /v1/traces → Tempo   ✅
│     ├── Micrometer OTLP Registry → OTLP/HTTP → /v1/metrics → Mimir  ✅
│     └── stdout (JSON) → Firelens Unix socket
│
└── log_router container (aws-for-fluent-bit:init-3.2.4 — 0.25 vCPU / 128 MiB)
      Downloads custom config from S3 at startup (aws_fluent_bit_init_s3_1)
      ├── record_modifier filter: adds service_name, openapm_product_name, region
      ├── opentelemetry output (logs_body_key_attributes true)
      │     → OTLP/HTTP → /v1/logs → Loki    ✅ service_name correct
      └── cloudwatch_logs output
            → /aws/batch/sre-batch-telemetry-java (CloudWatch)

All OTLP traffic → VPC Endpoint (PrivateLink) → OpenAPM :4318
```

---

## Evidence Summary

### What Works ✅

### Traces (Micrometer Tracing → Tempo)

Spring Boot 3.3 with `micrometer-tracing-bridge-otel` sends traces directly from the app via OTLP/HTTP. No Firelens involvement.

Each job run produces a trace with 4 spans:
```
batch-job-execution  (root)
  ├── fetch-data
  ├── process-data
  └── write-results
```

### Metrics (Micrometer OTLP Registry → Mimir)

`micrometer-registry-otlp` pushes metrics every 10s. A 10s sleep before `meterRegistry.close()` ensures a full step boundary is crossed so all metrics are exported.

Custom metrics confirmed in Mimir:
- `job_duration_seconds` (Timer — `_sum`, `_count`, `_max`)
- `job_items_processed_total` (Counter)
- `job_status` (Gauge — 0=success, 1=error)

All carry correct labels: `service_name`, `environment`, `region`, `account_id`, `openapm_product_name`.

### Logs (Firelens → OTLP → Loki) ✅

Using `aws-for-fluent-bit:init-3.2.4` with custom S3 config. Log records visible in Loki under `{service_name="sre-batch-telemetry-java"}` with:
- Correct indexed labels: `service_name`, `openapm_product_name`, `region`
- Full JSON log body with `traceId`, `spanId` correlation
- `container_name=app`, `source=stdout`

---

## The Journey: How `service_name` Was Fixed

### Initial Blocker

First Firelens runs showed logs under `{service_name="unknown_service"}`.

**Root cause:** OpenAPM OTel Collector maps OTLP resource attribute `service.name` → Loki label `service_name`. Standard Firelens sends stdout with **empty OTLP resource attributes**. The `add_label` ECS option sets an HTTP-level stream label — ignored by the Collector for `service_name` mapping.

### Attempts That Failed

| Attempt | Result |
|---|---|
| `add_label: "service.name sre-batch-telemetry-java"` | Rejected by ECS (dot in key not allowed) |
| `add_label: "service_name sre-batch-telemetry-java"` | HTTP label only — Collector ignores it |
| `OTEL_SERVICE_NAME` env var on app container | Firelens sidecar has no access to app env vars |
| `aws-for-fluent-bit:stable` | Fluent Bit 1.9.x — no log support in opentelemetry plugin |
| `FirelensConfiguration` S3 config source | ECS-infrastructure-only — blocked on Fargate |

### Solution — R&D Team Pattern

From `saas-platform-ms-notification-manager-dynamic-routed`:

1. **`aws-for-fluent-bit:init-3.2.4`** — downloads custom Fluent Bit config from S3 at container startup via `aws_fluent_bit_init_s3_1` env var. Works on Fargate (container-level, not ECS-infrastructure-level).
2. **`record_modifier` filter** — adds `service_name`, `openapm_product_name`, `region` as Fluent Bit record fields.
3. **`logs_body_key_attributes true`** — promotes those fields to OTLP log resource attributes.
4. OpenAPM Collector reads `service_name` resource attribute → correct Loki label.

---

## Custom Fluent Bit Config

Stored at: `arn:aws:s3:::sre-batch-telemetry-code-723346695882/fluent-bit/batch-fluent-bit.conf`

```ini
[SERVICE]
  Log_Level  warn
  Flush      0.1

[INPUT]
  Name           forward
  unix_path      /var/run/fluent.sock
  Mem_Buf_Limit  17M

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
  logs_body_key_attributes  true
  compress                  gzip
  Retry_Limit               no_retries

[OUTPUT]
  Name               cloudwatch_logs
  Match              *
  region             ${REGION}
  log_group_name     /aws/batch/${SERVICE_NAME}
  log_stream_prefix  firelens-
  auto_create_group  true
  log_key            log
  Retry_Limit        2
```

---

## Fluent Bit Version Comparison

| Image tag | Fluent Bit version | Log support | S3 config download |
|---|---|---|---|
| `:stable` | 1.9.10 | ❌ Metrics only | ❌ No |
| `:3.3.0` | 5.0.3 | ✅ Yes | ❌ No (ECS-only) |
| `:init-3.2.4` | 4.x | ✅ Yes | ✅ Yes (container-level) |

---

## Deployment Issues Resolved

| Error | Root Cause | Fix |
|---|---|---|
| `Could not parse arn: s3://...` | init image requires ARN format | Changed to `arn:aws:s3:::bucket/key` |
| `s3:GetBucketLocation AccessDenied` | Job role missing permission | Added `s3:GetBucketLocation` to IAM |
| `CreateLogStream AccessDenied` | Job role missing CloudWatch Logs perms | Added `logs:CreateLogGroup/Stream/PutLogEvents` to IAM |
| CF `Description >1024 chars` | CF hard limit | Shortened description |
| CF `EarlyValidation::ResourceExistenceCheck` | Log group owned by another stack | Removed log group resources — CloudWatch auto-creates |
| Fargate task size 1.25 vCPU invalid | app (1.0) + sidecar (0.25) = 1.25 — not valid | Set app to 0.75 vCPU + 1920 MiB → total 1 vCPU / 2048 MiB |

---

## Final Comparison: Firelens vs Direct OTLP

| | Brian's Firelens (init image) | Direct OTel Logback Appender |
|---|---|---|
| `service_name` in Loki | ✅ `sre-batch-telemetry-java` | ✅ `sre-batch-telemetry-java` |
| Trace-log correlation | ✅ `traceId` + `spanId` in logs | ✅ `traceId` + `spanId` in logs |
| ECS pattern consistency | ✅ Same as ECS microservices | ❌ App-level change required |
| Container count | 2 (app + log_router) | 1 |
| Branch | `poc/java-firelens-brian-approach` | `poc/java-aws-batch` |

---

## Confirmed Job Runs

| Job ID | Description | Traces | Metrics | Logs `service_name` |
|---|---|---|---|---|
| *(Fluent Bit 1.9.x run)* | Firelens stable image | ✅ | ✅ | ❌ Dropped silently |
| *(Fluent Bit 3.3.0 run)* | Firelens 3.3.0 | ✅ | ✅ | ⚠️ `unknown_service` |
| `91ef7036` | init image + ARN fix + IAM fix | ✅ | ✅ | ✅ `sre-batch-telemetry-java` |
| `7dd4a30b` | Metrics step boundary fix | ✅ | ✅ | ✅ `sre-batch-telemetry-java` |

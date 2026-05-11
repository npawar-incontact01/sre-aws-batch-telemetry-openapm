# Firelens Evidence — Brian's Approach: What Works and What Doesn't

## Background

Architect Brian recommended using **Firelens for log routing** in AWS Batch, consistent with how regular ECS microservices operate at NICE. This document captures the evidence from testing that approach and explains the hard blocker discovered.

**References Brian shared:**
- [AWS Batch multi-container support (Feb 2024)](https://aws.amazon.com/about-aws/whats-new/2024/02/aws-batch-multi-container-jobs/)
- [Firelens support for AWS Batch (Apr 2025)](https://aws.amazon.com/about-aws/whats-new/2025/04/aws-batch-amazon-elastic-container-service-exec-firelens-log-router/)
- [GitHub sidecar issue](https://github.com/aws/containers-roadmap/issues/1522)

---

## Evidence Summary

| Signal | Result | Grafana Query | Status |
|--------|--------|--------------|--------|
| **Traces** | Tempo receives spans with full hierarchy | `{service.name="sre-batch-telemetry-java"}` | ✅ Working |
| **Metrics** | Mimir receives `job_duration_seconds`, `job_items_processed_total`, `job_status` | `{service_name="sre-batch-telemetry-java"}` | ✅ Working |
| **Logs** | Loki receives log records (HTTP 200 from OTLP collector confirmed) | `{service_name="unknown_service"}` | ⚠️ Wrong label |

---

## What We Proved Works ✅

### Traces (Micrometer Tracing → Tempo)

Spring Boot 3.3 with `micrometer-tracing-bridge-otel` + `opentelemetry-exporter-otlp` sends traces directly from the app via OTLP/HTTP. Fully working, no Firelens involvement needed.

Each job run produces a trace with 4 spans:
```
batch-job-execution  (root)
  ├── fetch-data
  ├── process-data
  └── write-results
```

Tags confirmed in Tempo: `job.items_processed`, `job.status`, `data.count`, `data.processed`, `data.written`.

### Metrics (Micrometer OTLP Registry → Mimir)

`micrometer-registry-otlp` pushes metrics every 10 seconds. Confirmed in Mimir:
- `job_duration_seconds`
- `job_items_processed_total`
- `job_status` (0=success, 1=error)

All metrics carry the correct resource labels: `service_name`, `environment`, `region`, `account_id`, `openapm_product_name`.

### Logs Reaching Loki ✅ (via Firelens)

After upgrading from `aws-for-fluent-bit:stable` (Fluent Bit 1.9.x) to `aws-for-fluent-bit:3.3.0` (Fluent Bit 5.0.3), logs successfully reach Loki:
- HTTP 200 responses from OpenAPM OTLP collector confirmed
- Log records visible in Loki Explore
- Full log content preserved (JSON fields intact)

---

## The Hard Blocker ⚠️

### Problem: `service_name="unknown_service"` in Loki

Logs reach Loki but appear under `{service_name="unknown_service"}` instead of `{service_name="sre-batch-telemetry-java"}`.

### Why It Happens

The OpenAPM OTel Collector pipeline maps OTLP resource attributes to Loki stream labels:

```
OTLP log record
  └── resourceLogs.resource.attributes
        └── service.name = "sre-batch-telemetry-java"  ← mapped to Loki label: service_name
```

Firelens sends the app's stdout as raw log records **without any OTLP resource attributes**. The `add_label` ECS option only adds a **Loki HTTP stream label** on the push request — this is a completely different mechanism and is ignored by the OTLP collector when building `service_name`.

```
Firelens OTLP export:
  resourceLogs.resource.attributes = {}  ← EMPTY — no service.name
  add_label: service_name=...            ← HTTP header label, not OTLP resource attr
                                            collector ignores this for service_name mapping
```

Result: Collector has no `service.name` to map → falls back to `unknown_service`.

### What We Tried

| Attempt | Result |
|---|---|
| `add_label: "service.name sre-batch-telemetry-java"` | Rejected by ECS (space in key not allowed) |
| `add_label: "service_name sre-batch-telemetry-java"` | Accepted — sets Loki stream label, NOT OTLP resource attr |
| Set `OTEL_SERVICE_NAME` env var on app container | App uses it for traces/metrics — Firelens has no access to app env vars |
| Upgrade to Fluent Bit 3.3.0 | Logs now reach Loki ✅ — but wrong label problem remains |
| Custom Fluent Bit config via S3 | Blocked by Fargate — `FirelensConfiguration` S3 source only works in regular ECS, not Fargate |

### Why It Cannot Be Fixed with Firelens Alone

Fluent Bit's `opentelemetry` output plugin creates OTLP log records from stdin text. To set `service.name` as an **OTLP resource attribute** (not a stream label), you would need either:
1. A custom Fluent Bit config with `record_modifier` filter — but custom configs are blocked on Fargate
2. The OTel Collector pipeline to be modified to accept Firelens stream labels as resource attributes — requires OpenAPM team involvement

---

## Comparison: Firelens vs Direct OTLP Appender

| | Brian's Approach (Firelens) | Final Solution (Direct OTLP) |
|---|---|---|
| Traces | ✅ Micrometer direct | ✅ Micrometer direct |
| Metrics | ✅ Micrometer direct | ✅ Micrometer direct |
| Logs delivery | ✅ Reach Loki | ✅ Reach Loki |
| `service_name` label | ❌ `unknown_service` | ✅ `sre-batch-telemetry-java` |
| ECS consistency | ✅ Same as ECS microservices | ❌ App-level change required |
| Complexity | Multi-container task | Single container |
| Custom config | ❌ S3 config blocked on Fargate | N/A |
| Firelens version | Must use 3.3.0+ (1.9.x = no log support) | Not needed |

---

## Fluent Bit Version Discovery

**Critical finding:** The AWS-managed Firelens image `aws-for-fluent-bit:stable` ships **Fluent Bit 1.9.10**, which has the `opentelemetry` plugin for **metrics only**. Log records are silently dropped.

You **must** use `aws-for-fluent-bit:3.3.0` (Fluent Bit 5.0.3) or later for log support.

| Image tag | Fluent Bit version | Log support in opentelemetry plugin |
|---|---|---|
| `:stable` | 1.9.10 | ❌ Metrics only |
| `:3.3.0` | 5.0.3 | ✅ Yes — but `service_name` label still wrong |

---

## Recommendation for Brian

The Firelens approach works for log **delivery** but cannot produce the correct `service_name` Loki label without either:
- Custom Fluent Bit config (blocked on Fargate), or
- OpenAPM collector pipeline changes (team dependency)

**Two viable paths forward:**

| Path | Approach | `service_name` correct? | ECS consistent? |
|---|---|---|---|
| **A** | OTel Collector sidecar (Brian's Option 1) | ✅ Yes — sidecar can set resource attrs | Partial |
| **B** | OTel logback appender direct OTLP (current solution) | ✅ Yes | No |
| ~~C~~ | ~~Firelens only (Brian's Option 2)~~ | ❌ No | ✅ Yes |

**Path A** (OTel Collector sidecar) would achieve ECS consistency and correct labels, but requires shipping and maintaining the `otel/opentelemetry-collector-contrib` image alongside every Batch job.

**Path B** is what the `poc/java-aws-batch` branch implements — simpler, fully working today, all 3 signals confirmed in Grafana.

---

## Job Runs Used as Evidence

| Job ID | Branch state | Outcome | Traces | Metrics | Logs in Loki |
|---|---|---|---|---|---|
| *(Fluent Bit 1.9.x run)* | Firelens stable | SUCCEEDED | ✅ | ✅ | ❌ Dropped |
| *(Fluent Bit 3.3.0 run)* | Firelens 3.3.0 | SUCCEEDED | ✅ | ✅ | ⚠️ `unknown_service` |
| `a0dd88f9` | Direct OTLP appender | SUCCEEDED | ✅ | ✅ | ✅ `sre-batch-telemetry-java` |

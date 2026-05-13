# AWS Batch — OpenAPM Telemetry: Architecture One-Pager

> **Document Type:** Architecture One-Pager  
> **Audience:** Architects, Tech Leads, Directors, Platform Owners  
> **Purpose:** Concise visual and narrative overview of the two approved telemetry patterns for AWS Batch → OpenAPM  
> **Account:** mon-sandbox (`723346695882`) | **Region:** `us-west-2`  
> **Status:** Both approaches confirmed working — May 2026

---

## At a Glance

| | Approach A — Direct OTel | Approach B — Firelens Sidecar |
|---|---|---|
| **Description** | App sends all 3 signals directly via OTLP/HTTP | App sends traces/metrics directly; logs via Fluent Bit sidecar |
| **Containers** | 1 | 2 (app + log_router) |
| **Log routing** | In-process (OTel SDK Logback Appender) | Infrastructure-side (Firelens → Fluent Bit → OTLP) |
| **ECS consistency** | ❌ App-level change | ✅ Same pattern as ECS microservices |
| **Complexity** | Low | Medium |
| **All 3 signals** | ✅ Confirmed | ✅ Confirmed |
| **`service_name` correct** | ✅ | ✅ |
| **ECR required** | ❌ No | ❌ No |
| **Confirmed job** | `a0dd88f9` | `91ef7036` |

---

## Architecture Diagrams

### Approach A — Direct OTel (Single Container)

```
┌──────────────────────────────────────────────────────────────────────┐
│  AWS Account: mon-sandbox (723346695882) — VPC: shared_eks (10.0.0.0/21) │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  AWS Batch Fargate Task (1 vCPU / 2048 MiB)                  │   │
│  │                                                              │   │
│  │  ┌──────────────────────────────────────────────────────┐    │   │
│  │  │  app container (amazoncorretto:17)                   │    │   │
│  │  │                                                      │    │   │
│  │  │  Entrypoint:                                         │    │   │
│  │  │  aws s3 cp .../batch-telemetry.jar /app.jar          │    │   │
│  │  │  java -jar /app.jar                                  │    │   │
│  │  │                                                      │    │   │
│  │  │  Spring Boot 3.3.13 (CommandLineRunner)              │    │   │
│  │  │  ┌─────────────────────────────────────────────┐     │    │   │
│  │  │  │ Micrometer Tracing (OTel bridge)            │─────┼────┼──►│ /v1/traces
│  │  │  │ Micrometer OTLP Registry (push 10s)         │─────┼────┼──►│ /v1/metrics
│  │  │  │ Logback OTelAppender + SdkLoggerProvider    │─────┼────┼──►│ /v1/logs
│  │  │  └─────────────────────────────────────────────┘     │    │   │
│  │  │  stdout ──────────────────────────────────────────── ┼ ──►│ CloudWatch
│  │  └──────────────────────────────────────────────────────┘    │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │ OTLP/HTTP port 4318                  │
│                              ▼                                      │
│  ┌──────────────────────────────────────┐                           │
│  │  VPC Endpoint (PrivateLink)          │                           │
│  │  vpce-07577bcf5fb4edc78              │                           │
│  │  DNS: apm-na1.mon-sandbox.nicecxone  │                           │
│  └──────────────────────────────────────┘                           │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ AWS PrivateLink (stays in AWS network)
                           ▼
         ┌─────────────────────────────────────────┐
         │  OpenAPM Collector                       │
         │  /v1/traces  ──► Tempo  (Traces)         │
         │  /v1/metrics ──► Mimir  (Metrics)        │
         │  /v1/logs    ──► Loki   (Logs)           │
         └──────────────────────────┬──────────────┘
                                    │
                                    ▼
                         ┌──────────────────┐
                         │  Grafana Explore │
                         │  Traces/Metrics  │
                         │  /Logs unified   │
                         └──────────────────┘
```

### Approach B — Firelens Sidecar (Dual Container / Architect's Pattern)

```
┌──────────────────────────────────────────────────────────────────────┐
│  AWS Account: mon-sandbox (723346695882) — VPC: shared_eks           │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  AWS Batch Fargate Task (1 vCPU / 2048 MiB)                  │   │
│  │                                                              │   │
│  │  ┌───────────────────────────────────┐  ┌────────────────┐  │   │
│  │  │  app container                    │  │  log_router    │  │   │
│  │  │  (0.75 vCPU / 1920 MiB)          │  │  (0.25 vCPU /  │  │   │
│  │  │  amazoncorretto:17                │  │   128 MiB)     │  │   │
│  │  │                                   │  │ aws-for-fluent │  │   │
│  │  │  Micrometer Tracing ──────────────┼──┼──────────────► OTLP /v1/traces
│  │  │  Micrometer OTLP Registry ────────┼──┼──────────────► OTLP /v1/metrics
│  │  │                                   │  │  bit:init-3.2.4│  │   │
│  │  │  stdout (JSON) ──────────────────►│  │  ↓             │  │   │
│  │  │                                   │  │  record_mod    │  │   │
│  │  │                                   │  │  (service_name)│  │   │
│  │  │                                   │  │  ↓             │  │   │
│  │  │                                   │  │  otel output ──┼──┼──► OTLP /v1/logs
│  │  │                                   │  │  (body_key_attr│  │   │
│  │  │                                   │  │   true)        │  │   │
│  │  │                                   │  │  ↓             │  │   │
│  │  │                                   │  │  cw_logs ──────┼──┼──► CloudWatch
│  │  └───────────────────────────────────┘  └────────────────┘  │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │ OTLP/HTTP port 4318                  │
│                              ▼                                      │
│          VPC Endpoint (PrivateLink)  vpce-07577bcf5fb4edc78         │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ AWS PrivateLink
                               ▼
            ┌──────────────────────────────────────┐
            │  OpenAPM Collector                    │
            │  Traces  → Tempo                      │
            │  Metrics → Mimir                      │
            │  Logs    → Loki (service_name ✅)     │
            └──────────────────────────────────────┘
```

---

## Data Flow — Telemetry Signal by Signal

### Traces

```
Job Phase Execution
    │
    ├── tracer.start_as_current_span("batch-job-execution")   ← root span
    │       ├── tracer.start_as_current_span("fetch-data")
    │       ├── tracer.start_as_current_span("process-data")
    │       └── tracer.start_as_current_span("write-results")
    │
    ▼
BatchSpanProcessor (buffers spans in memory)
    │
    ▼
OTLPSpanExporter
    │  POST /v1/traces  (OTLP/HTTP protobuf)
    ▼
VPC Endpoint :4318
    │
    ▼
OpenAPM Collector → Tempo
    │
    ▼
Grafana Explore → {service.name="sre-batch-telemetry-java"}
```

### Metrics

```
Micrometer instruments (Timer, Counter, AtomicGauge)
    │  (recorded during job execution)
    ▼
OtlpMeterRegistry — pushes every 10 seconds
    │
    ▼
OTLPMetricExporter
    │  POST /v1/metrics  (OTLP/HTTP protobuf)
    ▼
VPC Endpoint :4318
    │
    ▼
OpenAPM Collector → Mimir
    │
    ▼
Grafana Explore → job_status{service_name="sre-batch-telemetry-java"}
                  job_duration_seconds{...}
                  job_items_processed_total{...}
```

### Logs — Approach A (Direct)

```
log.info("message") / logger.info("message")
    │
    ▼  [Logback OpenTelemetryAppender]
    │  traceId + spanId injected from Micrometer MDC
    ▼
SdkLoggerProvider (built from OTEL_* env vars)
    │
    ▼
OtlpHttpLogRecordExporter
    │  POST /v1/logs  (OTLP/HTTP protobuf)
    │  resource.attributes: { service.name, service_name, environment, ... }
    ▼
VPC Endpoint :4318 → OpenAPM Collector → Loki
    │
    ▼
{service_name="sre-batch-telemetry-java"} ← correct label ✅
```

### Logs — Approach B (Firelens)

```
log.info("message")
    │
    ▼  [stdout / JSON]
    │
    ▼  [Firelens Unix socket → aws-for-fluent-bit:init-3.2.4]
    │
    ▼  [record_modifier filter]
    │  Adds: service_name=${SERVICE_NAME}
    │        openapm_product_name=${PRODUCT_NAME}
    │        region=${REGION}
    │
    ▼  [opentelemetry output]
    │  logs_body_key_attributes true
    │  → Promotes record fields to OTLP log resource attributes
    │  POST /v1/logs  (OTLP/HTTP)
    │  resource.attributes: { service_name, openapm_product_name, region }
    ▼
VPC Endpoint :4318 → OpenAPM Collector → Loki
    │
    ▼
{service_name="sre-batch-telemetry-java"} ← correct label ✅
```

---

## Infrastructure Map

```
┌────────────────────────────────────────────────────────────────┐
│  CloudFormation Stacks (deployment order)                       │
│                                                                  │
│  1. sre-batch-iam-dev                                           │
│     ├── ExecutionRole: sre-aws-batch-telemetry-dev-execution-role│
│     └── JobRole: sre-aws-batch-telemetry-dev-job-role           │
│                                                                  │
│  2. sre-batch-sg-dev                                            │
│     └── sg-0cf31b827483e31fc (TCP 4318 egress)                  │
│                                                                  │
│  3. sre-batch-ce-dev                                            │
│     └── sre-aws-batch-telemetry-dev-ce (Fargate, 3 subnets)    │
│                                                                  │
│  4. sre-batch-jq-dev                                            │
│     └── sre-aws-batch-telemetry-dev-queue                       │
│                                                                  │
│  5a. sre-batch-java-dev-jobdef        (Approach A)              │
│      └── sre-batch-telemetry-java-dev-job                       │
│                                                                  │
│  5b. sre-batch-java-firelens-jobdef   (Approach B)              │
│      └── sre-batch-telemetry-java-dev-firelens-job              │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│  Pre-existing Shared Infrastructure                             │
│                                                                  │
│  VPC:    vpc-0693b34275513631c  (shared_eks, 10.0.0.0/21)      │
│  VPCE:   vpce-07577bcf5fb4edc78 (OpenAPM PrivateLink)           │
│  S3:     sre-batch-telemetry-code-723346695882                  │
│          ├── java/batch-telemetry.jar                           │
│          ├── python/batch_job.py                                │
│          └── fluent-bit/batch-fluent-bit.conf                   │
└────────────────────────────────────────────────────────────────┘
```

---

## Key Technical Decisions and Rationale

| Decision | Choice Made | Rationale |
|---------|------------|----------|
| Protocol | OTLP/HTTP port 4318 | VPC Endpoint is HTTP/1.1; gRPC (port 4317) returns `UNAVAILABLE` |
| Image delivery | Public ECR + S3 JAR download | ECR push blocked by org IAM in mon-sandbox |
| Deployment tool | AWS CloudShell | MFA required for all CLI ops; CloudShell inherits console MFA |
| Java log wiring | Custom `SdkLoggerProvider` from env vars | Spring Boot 3.3 auto-config bean has no-op logger provider |
| Firelens image | `aws-for-fluent-bit:init-3.2.4` | Only `init` variant downloads S3 config on Fargate; 1.9.x drops logs silently |
| Fluent Bit config delivery | Container-level S3 download via env var | `FirelensConfiguration` S3 option is ECS-infra only, blocked on Fargate |
| Firelens `service_name` | `record_modifier` + `logs_body_key_attributes true` | Promotes Fluent Bit record fields to OTLP resource attributes; `add_label` only sets HTTP stream labels |
| Fargate sizing (Firelens) | app=0.75 vCPU/1920 MiB + sidecar=0.25/128 MiB | 1.0 + 0.25 = 1.25 vCPU is not a valid Fargate task size |

---

## Validated Results — Confirmed Job Runs

| Job ID | Approach | Traces | Metrics | Logs `service_name` |
|--------|---------|--------|---------|---------------------|
| `677348fb` | Python Direct OTel | ✅ | ✅ | ✅ correct |
| `a0dd88f9` | Java Direct OTel (Approach A) | ✅ | ✅ | ✅ correct |
| `91ef7036` | Java Firelens init image (Approach B) | ✅ | ✅ | ✅ correct |
| `7dd4a30b` | Java Firelens — metrics step boundary fix | ✅ | ✅ | ✅ correct |

---

## Grafana — What to Expect

| Signal | Backend | Query | Example Result |
|--------|---------|-------|----------------|
| Traces | Tempo | `{service.name="sre-batch-telemetry-java"}` | 4-span hierarchy per job run |
| Metrics | Mimir | `job_status{service_name="sre-batch-telemetry-java"}` | `0` (success) or `1` (error) |
| Logs | Loki | `{service_name="sre-batch-telemetry-java"}` | JSON lines with `traceId`, `spanId` |

**Trace-to-log correlation:** Every log line contains `traceId` + `spanId` from Micrometer MDC injection. In Grafana Explore, clicking a span and selecting "Logs for this span" navigates directly to the correlated Loki log lines.

---

## Open Items — Architecture Decision Needed

| Item | Decision Required | Owner |
|------|------------------|-------|
| Standard pattern for NICE Batch telemetry | Choose Approach A (Direct OTel) or Approach B (Firelens) as the org standard | Architect / Architecture |
| Python `batch_otel` library publishing | Publish to internal PyPI / NICE artifact registry | SRE |
| Grafana dashboard template | Create standard Batch job dashboard | SRE / Observability team |
| Alert template | `job_status=1` alert rule for all Batch jobs | SRE |
| Multi-region support | Validate VPC Endpoint config per region | Platform team |

---

*Related documents:*
- [01-AWS-BATCH-OPENAPM-GUIDELINES.md](01-AWS-BATCH-OPENAPM-GUIDELINES.md) — Standards & constraints
- [02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md](02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md) — Developer integration guide
- [04-POC-PYTHON-SINGLE-CONTAINER.md](04-POC-PYTHON-SINGLE-CONTAINER.md) — Python POC deep-dive
- [05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md](05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md) — Java Firelens POC deep-dive

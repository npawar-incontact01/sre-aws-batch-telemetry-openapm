# Python AWS Batch Telemetry POC — What We Built and How

## Objective

Prove that a **Python AWS Batch job running on Fargate** can send all three OpenTelemetry signals — traces, metrics, and logs — to **OpenAPM** (Grafana Tempo / Mimir / Loki) with correct service labels and no ECR dependency.

---

## What We Achieved

| Signal | Destination | Label in Grafana | Status |
|--------|-------------|-----------------|--------|
| Traces | Tempo | `service.name=sre-aws-batch-telemetry` | ✅ Working |
| Metrics | Mimir | `service_name=sre-aws-batch-telemetry` | ✅ Working |
| Logs | Loki | `service_name=sre-aws-batch-telemetry` | ✅ Working |

All three signals arrive under the correct `service_name` label, enabling correlated trace-log drill-down in Grafana Explore.

---

## Architecture

```
AWS Batch Fargate Task
└── Python container (public.ecr.aws/docker/library/python:3.11-slim)
    │
    ├─ Downloads batch_job.py + requirements.txt from S3 at startup
    │     (avoids ECR — org policy blocks ecr:GetAuthorizationToken)
    │
    ├─ OTel SDK initialised via batch_otel library
    │     TracerProvider  → OTLPSpanExporter   → /v1/traces
    │     MeterProvider   → OTLPMetricExporter → /v1/metrics
    │     LoggerProvider  → OTLPLogExporter    → /v1/logs
    │
    └─ All OTLP/HTTP calls → VPC Endpoint (PrivateLink) → OpenAPM
```

### Key Infrastructure

| Resource | Value |
|---|---|
| AWS Account | `723346695882` (mon-sandbox) |
| Region | `us-west-2` |
| Batch Job Queue | `sre-aws-batch-telemetry-dev-queue` |
| Job Definition | `sre-batch-telemetry-dev-job` |
| Container Image | `public.ecr.aws/docker/library/python:3.11-slim` |
| S3 Bucket | `sre-batch-telemetry-code-723346695882` |
| OTLP Endpoint | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` |
| VPC Endpoint | `vpce-07577bcf5fb4edc78` |

---

## How We Built It

### Step 1 — Designed the `batch_otel` Library

Created `src/batch_otel/instrumentation.py` — a reusable library that:
- Reads all configuration from `OTEL_*` environment variables (12-factor style)
- Initialises `TracerProvider`, `MeterProvider`, and `LoggerProvider`
- Wires up `BatchSpanProcessor`, `PeriodicExportingMetricReader`, and `BatchLogRecordProcessor`
- Returns an `OTelContext` dataclass with `.tracer`, `.meter`, `.logger` ready to use
- Provides `shutdown_telemetry()` which flushes all providers before process exit

**Key design choice:** All three providers share the same `Resource` object built from `OTEL_SERVICE_NAME` + `OTEL_RESOURCE_ATTRIBUTES`. This guarantees the same `service_name` label appears consistently in Tempo, Mimir, and Loki.

```python
# src/batch_otel/__init__.py
from batch_otel.instrumentation import init_telemetry, shutdown_telemetry, OTelContext
```

### Step 2 — Wrote the Batch Job (`batch_job.py`)

```python
from batch_otel import init_telemetry, shutdown_telemetry

ctx = init_telemetry()

with ctx.tracer.start_as_current_span("batch-job"):
    ctx.logger.info("Fetching data...")
    # ... job logic with spans and metrics ...
    ctx.meter.create_counter("items.processed").add(count)

shutdown_telemetry(ctx)
```

The job simulates three phases — fetch, process, write — each wrapped in a child span, so all traces are hierarchically linked.

### Step 3 — Deployed Infrastructure via CloudFormation

Deployed a nested CloudFormation master stack covering:
- **IAM roles** — Execution role (pull from ECR / CW logs) + Job role (S3 read)
- **Security group** — egress on 4317, 4318, 443; inbound from VPC CIDR `10.0.0.0/21`
- **Compute environment** — Fargate, `MaxvCpus=16`
- **Job queue** — Priority 1
- **Job definition** — `0.25 vCPU / 512 MiB`, container downloads code from S3

### Step 4 — Solved the VPC Endpoint / Network Problem

**Problem:** Initial OTLP calls timed out.
**Root cause:** The VPC Endpoint's security group had NO inbound rules.
**Fix:** Added inbound TCP `4318` and `443` from VPC CIDR `10.0.0.0/21`.

Validated with `curl` from inside a Fargate task:
```bash
curl -k https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318/v1/traces \
  -H "Content-Type: application/x-protobuf" -d "" -w "%{http_code}"
# → 415 (correct — wrong Content-Type, but endpoint is reachable)
```

### Step 5 — Worked Around ECR Policy Block

**Problem:** `ecr:GetAuthorizationToken` is explicitly denied by org SCP in mon-sandbox.
**Solution:** Use a **public ECR image** (`public.ecr.aws/docker/library/python:3.11-slim`) which doesn't need ECR auth, and fetch code from S3 at container startup:

```bash
# In job definition entrypoint:
aws s3 cp s3://sre-batch-telemetry-code-723346695882/code/batch_job.py /app/
aws s3 cp s3://sre-batch-telemetry-code-723346695882/code/requirements.txt /app/
pip install -r /app/requirements.txt -q
python /app/batch_job.py
```

### Step 6 — Fixed Protocol: HTTP Not gRPC

**Problem:** gRPC (port 4317) returned `UNAVAILABLE` through the VPC Endpoint.
**Root cause:** The PrivateLink terminates TLS at the NLB; gRPC's HTTP/2 framing isn't preserved correctly.
**Fix:** Switched all exporters to OTLP/HTTP (port 4318) with `http/protobuf` content type.

Set `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf` in the job definition environment.

### Step 7 — Validated All 3 Signals in Grafana

After a successful job run:
- **Grafana → Explore → Tempo**: traces visible with full span hierarchy (fetch → process → write)
- **Grafana → Explore → Mimir**: `job_duration_seconds`, `job_items_processed_total`, `job_status` metrics
- **Grafana → Explore → Loki**: `{service_name="sre-aws-batch-telemetry"}` shows all log lines

---

## Problems Encountered & Solutions

| Problem | Root Cause | Solution |
|---|---|---|
| OTLP calls timing out | VPC Endpoint SG had no inbound rules | Added TCP 4318/443 inbound from VPC CIDR |
| gRPC `UNAVAILABLE` | PrivateLink doesn't preserve HTTP/2 framing | Switched to OTLP/HTTP port 4318 |
| ECR `AccessDeniedException` | Org SCP denies `ecr:GetAuthorizationToken` | Used public ECR image + S3 code download |
| Logs missing `service_name` in Loki | `LoggerProvider` resource not matching `service.name` | Shared `Resource` object across all three providers |
| Loki `415` on logs | Wrong content-type / endpoint path | Used `OTLPLogExporter` with correct `/v1/logs` path |

---

## Reusability for Consumers

The `batch_otel` library is designed as a drop-in for any Python Batch job:

1. **Add to your job's requirements:** `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`
2. **Wrap your entry point** with `init_telemetry()` / `shutdown_telemetry()`
3. **Set env vars** in your job definition (copy from `cloudformation/consumer-job-definition.yaml`)

No infrastructure changes needed — reuse the shared execution role, job queue, and VPC endpoint.

See [CONSUMER-ONBOARDING.md](CONSUMER-ONBOARDING.md) for a step-by-step guide.

---

## Files Added / Modified in This POC

| File | Purpose |
|---|---|
| `src/batch_job.py` | Main POC batch job — all 3 signals |
| `src/batch_otel/__init__.py` | Public API for the library |
| `src/batch_otel/instrumentation.py` | OTelContext — providers + exporters |
| `src/batch_otel/requirements.txt` | OTel SDK pip dependencies |
| `cloudformation/batch-job-definition.yaml` | Job definition (Python, 0.25 vCPU / 512 MiB) |
| `cloudformation/consumer-job-definition.yaml` | Generic template for other teams |
| `cloudformation/iam-roles.yaml` | Execution + Job IAM roles |
| `cloudformation/security-groups.yaml` | SG with OTLP ports open to VPC |
| `cloudformation/batch-compute-environment.yaml` | Fargate compute environment |
| `cloudformation/batch-job-queue.yaml` | Job queue |
| `cloudformation/master-stack.yaml` | Nested stack orchestrator |
| `docs/CONSOLE-DEPLOYMENT-GUIDE.md` | CloudShell step-by-step deploy guide |
| `docs/CONSUMER-ONBOARDING.md` | How other teams onboard |
| `examples/simple_job.py` | Minimal new job example |
| `examples/existing_job_retrofit.py` | How to retrofit an existing job |

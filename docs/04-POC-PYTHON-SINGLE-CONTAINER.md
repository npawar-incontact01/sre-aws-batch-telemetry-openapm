# POC: Python Single-Container — Sending Logs, Metrics & Traces to OpenAPM via OpenTelemetry

> **Document Type:** Proof of Concept — Technical Deep-Dive  
> **Branch:** `poc/python-aws-batch`  
> **Approach:** Single container, Python 3.11, OpenTelemetry SDK, OTLP/HTTP direct  
> **Status:** ✅ All 3 signals confirmed working  
> **Confirmed Job:** `677348fb` — SUCCEEDED  
> **Account:** mon-sandbox (`723346695882`) | **Region:** `us-west-2`

---

## Table of Contents

1. [Objective](#1-objective)
2. [Why Python First?](#2-why-python-first)
3. [Architecture — Single Container](#3-architecture--single-container)
4. [What We Built](#4-what-we-built)
5. [Step-by-Step Implementation](#5-step-by-step-implementation)
6. [Build and Deploy](#6-build-and-deploy)
7. [Validation — All 3 Signals Confirmed](#7-validation--all-3-signals-confirmed)
8. [Blockers Encountered and Resolutions](#8-blockers-encountered-and-resolutions)
9. [Full Code Listing](#9-full-code-listing)
10. [Reusable `batch_otel` Library](#10-reusable-batch_otel-library)
11. [What Is Still Pending](#11-what-is-still-pending)

---

## 1. Objective

Prove that a **Python 3.11 AWS Batch job running on Fargate** can send all three OpenTelemetry signals — Traces, Metrics, and Logs — to **OpenAPM** (Grafana Tempo / Mimir / Loki) with correct `service_name` labels, without:
- Building or pushing a custom Docker image (ECR is org-blocked in mon-sandbox)
- Any sidecar container
- Any infrastructure change beyond what a standard Batch job needs

---

## 2. Why Python First?

Python was chosen as the first POC language because:

| Reason | Detail |
|--------|--------|
| **Simplest OTel integration** | Python OTel SDK has no framework-specific quirks (unlike Spring Boot 3.3 log provider issues) |
| **No build step** | `.py` files uploaded to S3 and fetched at runtime — no JAR, no Docker build |
| **Fastest iteration** | Change code → `aws s3 cp` → `aws batch submit-job` — no CI/CD needed |
| **Validate network path first** | Confirming VPC Endpoint + SG + protocol before tackling Java complexities |

---

## 3. Architecture — Single Container

```
┌──────────────────────────────────────────────────────────────────────┐
│  AWS Account: mon-sandbox (723346695882)                              │
│  VPC: shared_eks  (10.0.0.0/21)  |  Region: us-west-2               │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  AWS Batch Fargate Task                                      │    │
│  │  (1 vCPU / 2048 MiB — single container)                     │    │
│  │                                                              │    │
│  │  ┌──────────────────────────────────────────────────────┐   │    │
│  │  │  app container                                        │   │    │
│  │  │  Image: public.ecr.aws/docker/library/python:3.11-slim│   │    │
│  │  │                                                       │   │    │
│  │  │  Entrypoint (container command):                      │   │    │
│  │  │    pip install awscli -q                              │   │    │
│  │  │    aws s3 cp s3://<bucket>/python/ . --recursive      │   │    │
│  │  │    pip install -r requirements.txt -q                 │   │    │
│  │  │    python3 batch_job.py                               │   │    │
│  │  │                                                       │   │    │
│  │  │  Python 3.11 + OpenTelemetry SDK                      │   │    │
│  │  │  ┌───────────────────────────────────────────────┐    │   │    │
│  │  │  │ TracerProvider → OTLPSpanExporter             │────┼───┼──► /v1/traces
│  │  │  │ MeterProvider  → OTLPMetricExporter           │────┼───┼──► /v1/metrics
│  │  │  │ LoggerProvider → OTLPLogExporter              │────┼───┼──► /v1/logs
│  │  │  └───────────────────────────────────────────────┘    │   │    │
│  │  │  stdout ─────────────────────────────────────────────►│ CloudWatch│
│  │  └──────────────────────────────────────────────────────┘   │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                        │  OTLP/HTTP  port 4318                       │
│                        ▼                                             │
│  ┌──────────────────────────────────────┐                            │
│  │  VPC Endpoint (PrivateLink)          │                            │
│  │  vpce-07577bcf5fb4edc78              │                            │
│  │  DNS: apm-na1.mon-sandbox.nicecxone-sbx.com                      │
│  └──────────────────────────────────────┘                            │
└─────────────────────────────────┬────────────────────────────────────┘
                                  │ AWS PrivateLink
                                  ▼
             ┌────────────────────────────────────────┐
             │  OpenAPM Collector                      │
             │  /v1/traces  → Tempo   (Traces)         │
             │  /v1/metrics → Mimir   (Metrics)        │
             │  /v1/logs    → Loki    (Logs)           │
             └────────────────────────────────────────┘
                                  │
                                  ▼
                      ┌───────────────────────┐
                      │  Grafana Explore       │
                      │  Unified Observability │
                      └───────────────────────┘
```

**Key design points:**
- **No ECR** — uses `public.ecr.aws/docker/library/python:3.11-slim` (public image)
- **No custom Docker build** — Python code lives in S3, downloaded at container startup
- **Single container** — no sidecar needed; all 3 signals sent directly from app via OTLP/HTTP
- **No web server** — job runs, sends telemetry, then exits (short-lived process)

---

## 4. What We Built

### Repository Structure (Python POC Files)

```
sre-aws-batch-telemetry-openapm/
├── src/
│   ├── batch_job.py              ← Main job (full OTel setup + simulated work)
│   └── batch_otel/
│       ├── __init__.py           ← Public API: init_telemetry(), shutdown_telemetry()
│       ├── instrumentation.py    ← Core OTel provider setup
│       └── requirements.txt      ← OTel SDK dependencies
├── cloudformation/
│   └── batch-job-definition.yaml ← Python job definition CF template
├── scripts/
│   ├── upload-code-to-s3.sh      ← Upload Python code to S3
│   └── submit-job.sh             ← Submit job and watch status
└── examples/
    ├── simple_job.py             ← Minimal new job example
    └── existing_job_retrofit.py  ← How to retrofit an existing Python job
```

### What the Job Simulates

The POC job simulates a realistic 3-phase batch workload:

| Phase | Span Name | Duration | Work Simulated |
|-------|-----------|----------|----------------|
| Fetch | `fetch-data` | 0.1–0.5s | Read 50–200 items from S3 |
| Process | `process-data` | 0.2–1.0s | Transform items |
| Write | `write-results` | 0.1–0.5s | Write results to destination |

Metrics emitted per run:
- `job_duration_seconds` — Timer (wall-clock time)
- `job_items_processed_total` — Counter (items handled)
- `job_status` — Gauge (`0`=success, `1`=error)

---

## 5. Step-by-Step Implementation

### Step 1 — OTel Resource Setup

All OTel providers share a single `Resource` object that carries the service identity labels:

```python
import os
from opentelemetry.sdk.resources import Resource

ENDPOINT    = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT",
                              "https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318")
SVC_NAME    = os.environ.get("OTEL_SERVICE_NAME", "sre-aws-batch-telemetry")
RES_ATTRS   = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")

# Parse comma-separated key=value pairs from OTEL_RESOURCE_ATTRIBUTES
attrs = {"service.name": SVC_NAME}
for pair in RES_ATTRS.split(","):
    if "=" in pair:
        k, _, v = pair.partition("=")
        attrs[k.strip()] = v.strip()

resource = Resource.create(attrs)
# Result: service.name, service_name, environment, region, account.id,
#         openapm_product_name all attached to every signal
```

### Step 2 — Traces via TracerProvider

```python
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

exporter = OTLPSpanExporter(endpoint=ENDPOINT + "/v1/traces")
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
trace.set_tracer_provider(tracer_provider)

tracer = trace.get_tracer(SVC_NAME)
```

**Using the tracer in job code:**

```python
# Root span for the full job
with tracer.start_as_current_span("batch-job-execution") as root_span:
    root_span.set_attribute("job.environment", "mon-sandbox")

    # Child span for each phase
    with tracer.start_as_current_span("fetch-data") as span:
        items = fetch_data()
        span.set_attribute("data.item_count", len(items))
        span.set_attribute("data.source", "s3")

    with tracer.start_as_current_span("process-data") as span:
        results = process(items)
        span.set_attribute("data.items_processed", len(results))

    with tracer.start_as_current_span("write-results") as span:
        write(results)
        span.set_attribute("data.records_written", len(results))
```

**Resulting trace hierarchy in Tempo:**
```
batch-job-execution  (root, ~60s total)
  ├── fetch-data      (~0.3s)
  ├── process-data    (~0.6s)
  └── write-results   (~0.3s)
```

### Step 3 — Metrics via MeterProvider

```python
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

exporter = OTLPMetricExporter(endpoint=ENDPOINT + "/v1/metrics")
reader = PeriodicExportingMetricReader(exporter, export_interval_millis=10_000)
meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
metrics.set_meter_provider(meter_provider)

meter = metrics.get_meter(SVC_NAME)

# Create instruments
job_duration  = meter.create_histogram("job_duration_seconds",
                                        description="Job execution time")
items_counter = meter.create_counter("job_items_processed_total",
                                      description="Items processed")
job_status    = meter.create_up_down_counter("job_status",
                                              description="0=success 1=error")
```

**Recording during job:**

```python
import time
start = time.time()
# ... job logic ...
duration = time.time() - start

job_duration.record(duration)
items_counter.add(item_count)
job_status.add(0)  # 0 = success
```

### Step 4 — Logs via LoggerProvider

```python
import logging
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

log_exporter  = OTLPLogExporter(endpoint=ENDPOINT + "/v1/logs")
log_provider  = LoggerProvider(resource=resource)
log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
set_logger_provider(log_provider)

# Attach OTel handler to Python's root logger
# ↑ This means ALL log.info() / log.error() calls go to Loki automatically
handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
logging.getLogger().addHandler(handler)

logger = logging.getLogger("batch_job")
```

**Log records automatically include:**
- `traceId` and `spanId` from the current active OTel span context
- `service_name` from Resource attributes → correct Loki stream label
- Standard fields: level, timestamp, message, logger name

### Step 5 — Graceful Shutdown (Critical for Short-Lived Jobs)

```python
def shutdown_telemetry(tracer_provider, meter_provider, log_provider):
    """
    Flush and shut down all OTel providers.

    MUST be called before process exit — BatchSpanProcessor and
    BatchLogRecordProcessor buffer records in memory. Without shutdown(),
    the last batch of spans/logs may not be exported before the process exits.
    """
    tracer_provider.shutdown()   # Flushes and exports all buffered spans
    meter_provider.shutdown()    # Final metric push
    log_provider.shutdown()      # Flushes buffered log records

# Call at the end of main():
shutdown_telemetry(tracer_provider, meter_provider, log_provider)
```

> **Why this matters:** AWS Batch Fargate containers are stopped after the entrypoint process exits. Without explicit `shutdown()`, the last 5–10 seconds of buffered telemetry (final spans, final metric step, last log lines) are silently lost. Always call `shutdown_telemetry()` in a `finally` block.

---

## 6. Build and Deploy

### 6.1 Upload Code to S3 (from CloudShell)

```bash
# In AWS CloudShell (mon-sandbox account):
# Step 1: Upload files via "Actions → Upload file" button in CloudShell
#   Upload: src/batch_job.py, src/batch_otel/requirements.txt

# Step 2: Push to S3
aws s3 cp batch_job.py \
  s3://sre-batch-telemetry-code-723346695882/python/batch_job.py \
  --region us-west-2

aws s3 cp requirements.txt \
  s3://sre-batch-telemetry-code-723346695882/python/requirements.txt \
  --region us-west-2

# Verify
aws s3 ls s3://sre-batch-telemetry-code-723346695882/python/
```

### 6.2 Deploy CloudFormation Stacks

```bash
# IAM Roles (first time only)
aws cloudformation deploy \
  --stack-name sre-batch-iam-dev \
  --template-file cloudformation/iam-roles.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-west-2

# Security Group (first time only)
aws cloudformation deploy \
  --stack-name sre-batch-sg-dev \
  --template-file cloudformation/security-groups.yaml \
  --region us-west-2

# Compute Environment (first time only)
aws cloudformation deploy \
  --stack-name sre-batch-ce-dev \
  --template-file cloudformation/batch-compute-environment.yaml \
  --region us-west-2

# Job Queue (first time only)
aws cloudformation deploy \
  --stack-name sre-batch-jq-dev \
  --template-file cloudformation/batch-job-queue.yaml \
  --region us-west-2

# Python Job Definition
aws cloudformation deploy \
  --stack-name sre-batch-python-dev-jobdef \
  --template-file cloudformation/batch-job-definition.yaml \
  --parameter-overrides \
    ExecutionRoleArn=arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-execution-role \
    JobRoleArn=arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-job-role \
  --region us-west-2
```

### 6.3 Submit and Monitor

```bash
# Submit
JOB_ID=$(aws batch submit-job \
  --job-name sre-batch-python-$(date +%Y%m%d%H%M%S) \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-python-dev-job \
  --region us-west-2 \
  --query 'jobId' --output text)

echo "Submitted: $JOB_ID"

# Monitor status
watch -n 5 "aws batch describe-jobs \
  --jobs $JOB_ID \
  --region us-west-2 \
  --query 'jobs[0].[status,statusReason]' \
  --output text"
```

Expected status sequence: `SUBMITTED → PENDING → RUNNABLE → STARTING → RUNNING → SUCCEEDED`

### 6.4 Check CloudWatch Logs (Debugging)

```bash
aws logs tail /aws/batch/sre-aws-batch-telemetry \
  --follow \
  --region us-west-2
```

Expected output:
```
TracerProvider initialised, exporting to https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318
MeterProvider initialised, exporting to https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318
LoggerProvider initialised, exporting to https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318
Starting batch job execution...
Fetched 127 items in 0.31s
Processed 127 items in 0.73s
Wrote 127 results in 0.22s
Batch job completed. status=success items_processed=127 duration=8.43s
OTel providers shut down cleanly.
```

---

## 7. Validation — All 3 Signals Confirmed

**Confirmed job:** `677348fb` — **SUCCEEDED**

### Traces in Tempo

Grafana query:
```
{service.name="sre-aws-batch-telemetry"}
```

**Confirmed:**
- ✅ Trace with 4 spans per job run
- ✅ `service.name=sre-aws-batch-telemetry` label on all spans
- ✅ Span duration and attributes correct (`data.item_count`, `data.source`)
- ✅ Parent-child hierarchy: `batch-job-execution → fetch-data / process-data / write-results`

### Metrics in Mimir

Grafana queries:
```promql
job_status{service_name="sre-aws-batch-telemetry"}
job_items_processed_total{service_name="sre-aws-batch-telemetry"}
job_duration_seconds{service_name="sre-aws-batch-telemetry"}
```

**Confirmed:**
- ✅ `job_status=0` (success) after successful run
- ✅ `job_items_processed_total` increments with each run
- ✅ All metrics carry `environment`, `region`, `account.id`, `openapm_product_name` labels

### Logs in Loki

Grafana query:
```logql
{service_name="sre-aws-batch-telemetry"}
```

**Confirmed:**
- ✅ `service_name=sre-aws-batch-telemetry` stream label — correct
- ✅ Log lines contain `traceId` and `spanId` from active OTel context
- ✅ Structured log lines with timestamp, level, logger name
- ✅ Clicking a trace span in Tempo → "Logs for this span" navigates to correlated Loki logs

---

## 8. Blockers Encountered and Resolutions

### Blocker 1 — VPC Endpoint SG Had No Inbound Rules

**Symptom:** Job completed (exit code 0), CloudWatch logs showed correct output, but **zero telemetry in Grafana**. All OTLP calls timed out silently.

**Root cause:** The shared VPC Endpoint Security Group had no inbound rules. Batch tasks in the VPC sent OTLP traffic to the endpoint ENIs, but all connections were rejected.

**Debugging steps:**
1. Checked CloudWatch logs — job logic ran correctly ✅
2. Checked Grafana — no traces, metrics, or logs ❌
3. Added `print(response)` to OTel exporters — saw `ConnectTimeout`
4. Checked VPC Endpoint SG — **zero inbound rules** found

**Fix:**
```
VPC Endpoint SG: added inbound rules
  TCP 4318 from 10.0.0.0/21
  TCP 443  from 10.0.0.0/21
```

**Time to diagnose:** ~1 day (initially suspected app code issues).

---

### Blocker 2 — gRPC (Port 4317) Not Supported

**Symptom:** OTel exporters returned `StatusCode.UNAVAILABLE` with `Failed to connect to apm-na1...`.

**Root cause:** The VPC Endpoint is HTTP/1.1-only. gRPC requires HTTP/2 multiplexing, which this endpoint does not support.

**Fix:** Switched all exporters to OTLP/HTTP:
```python
# Before:
OTEL_EXPORTER_OTLP_PROTOCOL=grpc   # port 4317 — FAILS

# After:
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf   # port 4318 — WORKS
```

---

### Blocker 3 — Missing `OTEL_EXPORTER_OTLP_PROTOCOL` Env Var

**Symptom:** Even after fixing the SG, metrics failed to export but traces worked.

**Root cause:** Some exporters defaulted to gRPC when the env var was not set.

**Fix:** Always explicitly set:
```bash
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```
in the job definition, not just in code.

---

### Non-Blocker — ECR Access Denied

**Symptom:** Attempted to push a custom Docker image — `AccessDenied: ecr:GetAuthorizationToken`.

**Root cause:** Org IAM policy has explicit `Deny` on ECR in mon-sandbox.

**Workaround:** Used `public.ecr.aws/docker/library/python:3.11-slim` and fetched all code from S3 at container startup. No ECR push needed.

---

## 9. Full Code Listing

### `src/batch_job.py`

```python
"""
AWS Batch job with OpenTelemetry instrumentation.
Sends Traces, Metrics, and Logs to OpenAPM via OTLP/HTTP (port 4318).
"""
import logging, os, random, sys, time

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stdout)
logger = logging.getLogger("batch_job")

# ── Configuration from environment variables ──────────────────────────
ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT",
                           "https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318")
SVC_NAME = os.environ.get("OTEL_SERVICE_NAME", "sre-aws-batch-telemetry")
RES_ATTRS_RAW = os.environ.get("OTEL_RESOURCE_ATTRIBUTES",
    "environment=mon-sandbox,account.id=723346695882,"
    "openapm_product_name=sre-batch-telemetry,"
    "service_name=sre-aws-batch-telemetry,region=us-west-2")

def _parse_attrs(raw: str) -> dict:
    attrs = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            attrs[k.strip()] = v.strip()
    return attrs

# ── Resource: shared identity for all 3 signals ───────────────────────
resource = Resource.create({"service.name": SVC_NAME, **_parse_attrs(RES_ATTRS_RAW)})

# ── Traces ─────────────────────────────────────────────────────────────
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT + "/v1/traces")))
trace.set_tracer_provider(tracer_provider)

# ── Metrics ────────────────────────────────────────────────────────────
meter_provider = MeterProvider(resource=resource, metric_readers=[
    PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=ENDPOINT + "/v1/metrics"),
        export_interval_millis=5_000)])
metrics.set_meter_provider(meter_provider)

# ── Logs ───────────────────────────────────────────────────────────────
log_provider = LoggerProvider(resource=resource)
log_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=ENDPOINT + "/v1/logs")))
set_logger_provider(log_provider)
logging.getLogger().addHandler(LoggingHandler(level=logging.INFO,
                                               logger_provider=log_provider))

# ── Instruments ────────────────────────────────────────────────────────
tracer = trace.get_tracer(SVC_NAME)
meter  = metrics.get_meter(SVC_NAME)
job_duration  = meter.create_histogram("job_duration_seconds")
items_counter = meter.create_counter("job_items_processed_total")
job_status    = meter.create_up_down_counter("job_status")

# ── Simulated batch work ───────────────────────────────────────────────
def fetch_data() -> int:
    with tracer.start_as_current_span("fetch-data") as span:
        count = random.randint(50, 200)
        time.sleep(random.uniform(0.1, 0.5))
        span.set_attribute("data.item_count", count)
        span.set_attribute("data.source", "s3")
        logger.info("Fetched %d items", count)
        return count

def process_data(count: int) -> int:
    with tracer.start_as_current_span("process-data") as span:
        time.sleep(random.uniform(0.2, 1.0))
        span.set_attribute("data.items_processed", count)
        logger.info("Processed %d items", count)
        return count

def write_results(count: int) -> None:
    with tracer.start_as_current_span("write-results") as span:
        time.sleep(random.uniform(0.1, 0.5))
        span.set_attribute("data.records_written", count)
        logger.info("Wrote %d results", count)

# ── Main ───────────────────────────────────────────────────────────────
def main():
    start = time.time()
    logger.info("Starting batch job")

    try:
        with tracer.start_as_current_span("batch-job-execution") as root:
            count   = fetch_data()
            count   = process_data(count)
            write_results(count)

            duration = time.time() - start
            job_duration.record(duration)
            items_counter.add(count)
            job_status.add(0)  # success

            root.set_attribute("job.items_processed", count)
            root.set_attribute("job.status", "success")
            logger.info("Job complete. duration=%.2fs items=%d", duration, count)

    except Exception as exc:
        job_status.add(1)  # error
        logger.error("Job failed: %s", exc)
        sys.exit(1)

    finally:
        tracer_provider.shutdown()
        meter_provider.shutdown()
        log_provider.shutdown()

if __name__ == "__main__":
    main()
```

### `src/batch_otel/requirements.txt`

```
opentelemetry-sdk==1.24.0
opentelemetry-exporter-otlp-proto-http==1.24.0
opentelemetry-semantic-conventions==0.45b0
```

---

## 10. Reusable `batch_otel` Library

The `src/batch_otel/` directory contains a reusable Python library that wraps the OTel setup so other teams don't need to repeat it:

### Public API (`src/batch_otel/__init__.py`)

```python
from batch_otel.instrumentation import init_telemetry, shutdown_telemetry, OTelContext
__all__ = ["init_telemetry", "shutdown_telemetry", "OTelContext"]
```

### Usage (Minimal — 4 lines)

```python
from batch_otel import init_telemetry, shutdown_telemetry

def main():
    otel = init_telemetry()    # reads all OTEL_* env vars automatically

    with otel.tracer.start_as_current_span("my-job"):
        otel.logger.info("Processing started")
        otel.meter.create_counter("records.processed").add(100)
        otel.logger.info("Processing complete")

    shutdown_telemetry(otel)   # flushes everything before exit
```

### `OTelContext` Object Fields

```python
otel.tracer          # opentelemetry.trace.Tracer
otel.meter           # opentelemetry.metrics.Meter
otel.logger          # logging.Logger (with OTel handler attached)
otel.tracer_provider # TracerProvider (for manual shutdown)
otel.meter_provider  # MeterProvider (for manual shutdown)
otel.logger_provider # LoggerProvider (for manual shutdown)
otel.service_name    # str — resolved service name
otel.endpoint        # str — resolved OTLP endpoint
```

---

## 11. What Is Still Pending

| Item | Description | Priority |
|------|-------------|----------|
| **Library packaging** | `src/batch_otel/` needs `pyproject.toml`, version, and publishing to NICE internal PyPI | Medium |
| **pip install from artifact registry** | Teams should `pip install nice-batch-otel` instead of copying files | Medium |
| **Unit tests** | `tests/test_batch_job.py` exists but coverage is minimal | Low |
| **Multi-region** | Only tested in `us-west-2` — needs validation per region | Low |
| **Grafana dashboard** | No standard Python Batch job dashboard yet | Medium |

---

*Related documents:*
- [01-AWS-BATCH-OPENAPM-GUIDELINES.md](01-AWS-BATCH-OPENAPM-GUIDELINES.md) — Standards
- [02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md](02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md) — Developer reference
- [03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md](03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md) — Architecture
- [05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md](05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md) — Java Firelens POC

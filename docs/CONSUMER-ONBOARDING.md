# Consumer Onboarding Guide: Sending Telemetry from AWS Batch to OpenAPM

## Overview

This guide enables any AWS Batch application to send **traces**, **metrics**, and **logs** to OpenAPM (Grafana Tempo / Mimir / Loki) with minimal code changes.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  AWS Batch (Fargate)                                    │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Consumer Job Container                           │  │
│  │  ┌─────────────┐    ┌──────────────────────────┐ │  │
│  │  │ Your Code   │───▶│ batch_otel (shared lib)  │ │  │
│  │  └─────────────┘    └──────────┬───────────────┘ │  │
│  └────────────────────────────────┼─────────────────┘  │
│                                   │ OTLP/HTTP :4318    │
│                                   ▼                    │
│  ┌────────────────────────────────────────────────┐    │
│  │  VPC Endpoint (PrivateLink)                    │    │
│  └────────────────────────────────────────────────┘    │
└───────────────────────────────────┼─────────────────────┘
                                    ▼
                    ┌──────────────────────────┐
                    │  OpenAPM Collector       │
                    │  /v1/traces  → Tempo     │
                    │  /v1/metrics → Mimir     │
                    │  /v1/logs    → Loki      │
                    └──────────────────────────┘
```

## Quick Start (3 Steps)

### Step 1: Install the library

Add to your `requirements.txt`:
```
opentelemetry-sdk==1.24.0
opentelemetry-exporter-otlp-proto-http==1.24.0
opentelemetry-semantic-conventions==0.45b0
```

Copy `src/batch_otel/` into your project, or install from S3:
```bash
pip install -r https://sre-batch-telemetry-code-723346695882.s3.us-west-2.amazonaws.com/batch_otel/requirements.txt
aws s3 cp s3://sre-batch-telemetry-code-723346695882/batch_otel/ ./batch_otel/ --recursive
```

### Step 2: Add 4 lines to your job

```python
from batch_otel import init_telemetry, shutdown_telemetry

def main():
    otel = init_telemetry()  # reads config from env vars

    with otel.tracer.start_as_current_span("my-job-name"):
        # ... your existing job code ...
        pass

    shutdown_telemetry(otel)  # flushes all data before exit
```

### Step 3: Deploy with OTel environment variables

Use the provided CloudFormation template (`cloudformation/consumer-job-definition.yaml`):

```bash
aws cloudformation deploy \
    --stack-name "my-app-dev-jobdef" \
    --template-file consumer-job-definition.yaml \
    --parameter-overrides \
        ServiceName="my-batch-app" \
        EnvironmentName="dev" \
        ContainerImage="public.ecr.aws/docker/library/python:3.11-slim" \
        ExecutionRoleArn="arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-execution-role" \
        JobRoleArn="arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-job-role" \
        OpenapmProductName="my-product" \
    --region us-west-2
```

## Environment Variables Reference

| Variable | Required | Example | Description |
|----------|----------|---------|-------------|
| `OTEL_SERVICE_NAME` | Yes | `my-batch-app` | Service name in Grafana |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` | OpenAPM endpoint (default provided) |
| `OTEL_RESOURCE_ATTRIBUTES` | Yes | `environment=dev,openapm_product_name=my-product,...` | Comma-separated labels |

### Required Resource Attributes (for Grafana visibility)

| Attribute | Purpose | Example |
|-----------|---------|---------|
| `openapm_product_name` | Grafana product filter | `my-product` |
| `service_name` | Grafana service filter | `my-batch-app` |
| `environment` | Environment label | `dev`, `staging`, `prod` |
| `region` | AWS region | `us-west-2` |
| `account.id` | AWS account ID | `723346695882` |

## Integration Patterns

### Pattern A: New Job (recommended)

```python
from batch_otel import init_telemetry, shutdown_telemetry

def main():
    otel = init_telemetry()

    with otel.tracer.start_as_current_span("data-pipeline") as span:
        records = fetch_data()
        span.set_attribute("records.fetched", len(records))

        processed = transform(records)
        otel.meter.create_counter("records.processed").add(len(processed))

        load(processed)
        otel.logger.info("Pipeline complete: %d records", len(processed))

    shutdown_telemetry(otel)

if __name__ == "__main__":
    main()
```

### Pattern B: Retrofit Existing Job (minimal changes)

```python
# Add these lines at the top and bottom of your existing main()
from batch_otel import init_telemetry, shutdown_telemetry

# At the start of main():
otel = init_telemetry()

# Wrap your logic:
with otel.tracer.start_as_current_span("existing-job"):
    # ... all your existing code, unchanged ...
    pass

# At the end:
shutdown_telemetry(otel)
```

### Pattern C: S3 Code Fetch (no Docker build needed)

For teams that can't build/push Docker images, use the S3 code fetch approach:

1. Upload your code to S3:
```bash
aws s3 cp my_job.py s3://sre-batch-telemetry-code-723346695882/my-team/my_job.py
aws s3 cp requirements.txt s3://sre-batch-telemetry-code-723346695882/my-team/requirements.txt
```

2. Set container command in job definition:
```yaml
Command:
  - sh
  - -c
  - |
    pip install awscli -q &&
    aws s3 cp s3://sre-batch-telemetry-code-723346695882/my-team/ . --recursive &&
    pip install -r requirements.txt &&
    python3 my_job.py
```

## Network Prerequisites

Your Batch job must run in a VPC with:
- **VPC Endpoint** for OpenAPM PrivateLink (`vpce-07577bcf5fb4edc78` in mon-sandbox)
- **Security Group** allowing:
  - Egress TCP 4318 (OTLP/HTTP)
  - Inbound TCP 4318 on the VPC Endpoint SG from VPC CIDR
- **Private subnets** with the VPC Endpoint attached

Existing SG to use: `sg-0cf31b827483e31fc`

## Grafana Queries

### Traces (Tempo)
```
{service.name="my-batch-app"}
```

### Metrics (Mimir/Prometheus)
```
job_duration_seconds{service_name="my-batch-app", environment="dev"}
job_items_processed_total{openapm_product_name="my-product"}
```

### Logs (Loki)
```
{service_name="my-batch-app"} |= "error"
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `ConnectTimeout` | SG missing inbound rule on VPC Endpoint | Add TCP 4318 inbound from VPC CIDR |
| `404 page not found` | Wrong port or missing path | Use port 4318, library handles paths |
| No data in Grafana | Missing `openapm_product_name` label | Set in `OTEL_RESOURCE_ATTRIBUTES` |
| `UNAVAILABLE` (gRPC) | gRPC not supported through this endpoint | Use HTTP exporter (port 4318) |
| Duplicate spans | `main()` called twice | Ensure single entry point |

## File Structure

```
src/batch_otel/
├── __init__.py              # Public API: init_telemetry, shutdown_telemetry
├── instrumentation.py       # Core OTel setup (traces, metrics, logs)
└── requirements.txt         # OTel dependencies

cloudformation/
├── consumer-job-definition.yaml  # Generic job def template for consumers
├── iam-roles.yaml                # Shared IAM roles
└── security-groups.yaml          # Shared SG with OTel ports

examples/
├── simple_job.py                 # New job example
└── existing_job_retrofit.py      # Retrofit existing job
```

# POC: Java Dual-Container with Firelens — Sending Logs, Metrics & Traces to OpenAPM via OpenTelemetry

> **Document Type:** Proof of Concept — Technical Deep-Dive  
> **Branch:** `poc/java-firelens-architect-approach`  
> **Approach:** Dual container (app + Firelens log_router), Java 17 / Spring Boot 3.3, Micrometer + Fluent Bit sidecar  
> **Architect Recommendation:** Architect's recommended pattern (consistent with ECS microservices)  
> **Status:** ✅ All 3 signals confirmed working with correct `service_name`  
> **Confirmed Jobs:** `91ef7036`, `7dd4a30b` — SUCCEEDED  
> **Account:** mon-sandbox (`723346695882`) | **Region:** `us-west-2`

---

## Table of Contents

1. [Objective](#1-objective)
2. [Why This Approach? — Architect's Recommendation](#2-why-this-approach--architects-recommendation)
3. [Architecture — Dual Container (Firelens)](#3-architecture--dual-container-firelens)
4. [What We Built](#4-what-we-built)
5. [Step-by-Step Implementation](#5-step-by-step-implementation)
6. [The Key Challenge: Getting `service_name` Right in Loki](#6-the-key-challenge-getting-service_name-right-in-loki)
7. [Custom Fluent Bit Configuration](#7-custom-fluent-bit-configuration)
8. [CloudFormation Job Definition — Dual Container](#8-cloudformation-job-definition--dual-container)
9. [Build and Deploy](#9-build-and-deploy)
10. [Validation — All 3 Signals Confirmed](#10-validation--all-3-signals-confirmed)
11. [Blockers Encountered — Full Timeline](#11-blockers-encountered--full-timeline)
12. [Firelens vs Direct OTel — Side-by-Side Comparison](#12-firelens-vs-direct-otel--side-by-side-comparison)
13. [Files in This POC](#13-files-in-this-poc)

---

## 1. Objective

Implement Architect's recommended **Firelens sidecar pattern** for AWS Batch log routing and confirm it produces all three OTel signals — Traces, Metrics, and Logs — with the correct `service_name` label in OpenAPM Grafana. This approach mirrors how ECS microservices route logs at NICE.

---

## 2. Why This Approach? — Architect's Recommendation

After reviewing the initial Python and Java direct-OTel POCs, Architect recommended standardising on the **Firelens sidecar pattern** for log routing, citing:

1. **ECS consistency** — All NICE ECS microservices already use Firelens for log routing. Batch jobs should follow the same pattern.
2. **Infrastructure-level routing** — Logs should not require application code changes. The sidecar handles routing transparently.
3. **AWS Batch multi-container support** — AWS added multi-container support to Batch in February 2024 and Firelens support in April 2025, making this pattern fully supported.

**References Architect shared:**

| Reference | Description |
|-----------|-------------|
| [AWS Batch multi-container support (Feb 2024)](https://aws.amazon.com/about-aws/whats-new/2024/02/aws-batch-multi-container-jobs/) | AWS announcement enabling sidecar containers in Batch |
| [Firelens support for AWS Batch (Apr 2025)](https://aws.amazon.com/about-aws/whats-new/2025/04/aws-batch-amazon-elastic-container-service-exec-firelens-log-router/) | Firelens log router officially supported in Batch |
| [Open APM Logs Migration Guide (internal)](https://nice-ce-cxone-prod.atlassian.net/wiki/spaces/WFM/pages/3188392157) | NICE internal guide: Log Routing with FireLens |

---

## 3. Architecture — Dual Container (Firelens)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  AWS Account: mon-sandbox (723346695882)                                 │
│  VPC: shared_eks (10.0.0.0/21)  |  Region: us-west-2                   │
│                                                                          │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │  AWS Batch Fargate Task  (Total: 1 vCPU / 2048 MiB)               │  │
│  │                                                                    │  │
│  │  ┌──────────────────────────────┐  ┌────────────────────────────┐ │  │
│  │  │  app container               │  │  log_router container       │ │  │
│  │  │  (0.75 vCPU / 1920 MiB)     │  │  (0.25 vCPU / 128 MiB)    │ │  │
│  │  │  amazoncorretto:17           │  │  aws-for-fluent-bit:init-  │ │  │
│  │  │                              │  │  3.2.4                     │ │  │
│  │  │  Spring Boot 3.3.13          │  │                            │ │  │
│  │  │  CommandLineRunner           │  │  On startup:               │ │  │
│  │  │                              │  │  Downloads custom conf     │ │  │
│  │  │  ┌──────────────────────┐    │  │  from S3 via              │ │  │
│  │  │  │Micrometer Tracing    ├────┼──┼──────────────────────────►  OTLP /v1/traces
│  │  │  │(OTel bridge)         │    │  │  aws_fluent_bit_init_s3_1  │ │  │
│  │  │  └──────────────────────┘    │  │                            │ │  │
│  │  │  ┌──────────────────────┐    │  │  [FILTER record_modifier]  │ │  │
│  │  │  │Micrometer OTLP Reg.  ├────┼──┼──────────────────────────►  OTLP /v1/metrics
│  │  │  │(push every 10s)      │    │  │  + service_name            │ │  │
│  │  │  └──────────────────────┘    │  │  + openapm_product_name    │ │  │
│  │  │                              │  │  + region                  │ │  │
│  │  │  stdout (JSON logs) ─────────┼─►│                            │ │  │
│  │  │  [Firelens Unix socket]      │  │  [OUTPUT opentelemetry]    │ │  │
│  │  │                              │  │  logs_body_key_attributes  │ │  │
│  │  │                              │  │  true ────────────────────►  OTLP /v1/logs
│  │  │                              │  │                            │ │  │
│  │  │                              │  │  [OUTPUT cloudwatch_logs]  │ │  │
│  │  │                              │  │  ────────────────────────► CloudWatch │  │
│  │  └──────────────────────────────┘  └────────────────────────────┘ │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                          │  OTLP/HTTP  port 4318                        │
│                          ▼                                              │
│          VPC Endpoint (PrivateLink)  vpce-07577bcf5fb4edc78            │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │ AWS PrivateLink
                           ▼
        ┌────────────────────────────────────────────────────┐
        │  OpenAPM Collector                                  │
        │  Traces  → Tempo   {service.name="..."}            │
        │  Metrics → Mimir   {service_name="..."}            │
        │  Logs    → Loki    {service_name="..."} ✅         │
        └────────────────────────────────────────────────────┘
```

**Critical sizing note:** The total task must equal a valid Fargate task size:
- App container: `0.75 vCPU / 1920 MiB`
- log_router: `0.25 vCPU / 128 MiB`
- **Total: 1 vCPU / 2048 MiB** ← valid Fargate size

> `1.0 + 0.25 = 1.25 vCPU` would be **invalid** and CloudFormation deployment would fail with `ClientException`.

---

## 4. What We Built

### Repository Files (Firelens POC)

```
sre-aws-batch-telemetry-openapm/
├── java-poc/
│   ├── pom.xml                                     ← Spring Boot 3.3.13 + Micrometer + OTel
│   └── src/main/
│       ├── java/com/nice/sre/batch/
│       │   └── BatchTelemetryApplication.java       ← CommandLineRunner (Firelens branch)
│       └── resources/
│           ├── application.yml                      ← Micrometer tracing/metrics config
│           └── logback-spring.xml                   ← CONSOLE appender only (no OTel appender)
├── docker/fluent-bit/
│   └── batch-fluent-bit.conf                        ← Custom Fluent Bit config (uploaded to S3)
├── cloudformation/
│   └── batch-job-definition-java-firelens.yaml      ← Multi-container CF template
└── scripts/
    └── build-and-deploy-java.sh                     ← Build JAR + upload to S3
```

### Key Difference vs Direct OTel Approach

| Aspect | Direct OTel Branch | Firelens Branch |
|--------|-------------------|-----------------|
| Log routing | `OtlpHttpLogRecordExporter` in-app | Firelens Unix socket → Fluent Bit sidecar |
| `logback-spring.xml` | `OpenTelemetryAppender` | CONSOLE appender only |
| `SdkLoggerProvider` | Custom built from env vars | Not needed — sidecar handles logs |
| Container count | 1 | 2 |
| App vCPU | 1.0 | 0.75 |
| App memory | 2048 MiB | 1920 MiB |

---

## 5. Step-by-Step Implementation

### Step 1 — Java Application (No Log Wiring Needed)

On the Firelens branch, the app does **not** need an OTel log appender. Logs are written to stdout as JSON and Firelens captures them automatically.

`logback-spring.xml` — CONSOLE only:

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
        <!-- No OpenTelemetryAppender here — Firelens handles log routing -->
    </root>
</configuration>
```

### Step 2 — Traces via Micrometer Tracing (Same as Direct Approach)

`pom.xml` dependencies:
```xml
<dependency>
    <groupId>io.micrometer</groupId>
    <artifactId>micrometer-tracing-bridge-otel</artifactId>
</dependency>
<dependency>
    <groupId>io.opentelemetry</groupId>
    <artifactId>opentelemetry-exporter-otlp</artifactId>
    <version>1.39.0</version>
</dependency>
```

`application.yml`:
```yaml
management:
  tracing:
    sampling:
      probability: 1.0
  otlp:
    tracing:
      endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces
```

Job logic — create root span and child spans:

```java
Span rootSpan = tracer.nextSpan().name("batch-job-execution").start();
try (Tracer.SpanInScope ws = tracer.withSpan(rootSpan)) {
    fetchData();
    processData(count);
    writeResults(count);
} finally {
    rootSpan.end();
}
```

Micrometer automatically injects `traceId` and `spanId` into MDC — so all `log.info()` lines include them. This enables Firelens to forward those fields in the log JSON, making trace-log correlation work in Grafana.

### Step 3 — Metrics via Micrometer OTLP Registry (Same as Direct Approach)

`pom.xml`:
```xml
<dependency>
    <groupId>io.micrometer</groupId>
    <artifactId>micrometer-registry-otlp</artifactId>
</dependency>
```

`application.yml`:
```yaml
management:
  otlp:
    metrics:
      export:
        url: ${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics
        step: 10s
        resource-attributes:
          service.name:          ${OTEL_SERVICE_NAME}
          service_name:          ${OTEL_SERVICE_NAME}
          environment:           ${ENVIRONMENT:mon-sandbox}
          openapm_product_name:  ${OPENAPM_PRODUCT_NAME}
          region:                ${AWS_REGION:us-west-2}
          account.id:            ${AWS_ACCOUNT_ID}
```

**Metrics flush strategy** — job must sleep 10s before exit:

```java
// Close MeterRegistry → triggers final metric push at step boundary
if (meterRegistry instanceof AutoCloseable c) {
    c.close();
}
// Sleep 10–15s to allow BatchSpanProcessor + final metric push to complete
Thread.sleep(15_000);
```

### Step 4 — Firelens Sidecar Captures Logs

With Firelens configured, the app container's stdout is automatically intercepted by the `log_router` container via a Unix socket at `/var/run/fluent.sock`. No code change needed in the app.

The `log_router` sidecar:
1. Receives log lines from the app stdout via Firelens
2. Applies `record_modifier` filter to add `service_name`, `openapm_product_name`, `region`
3. Sends to OpenAPM via OTLP/HTTP with `logs_body_key_attributes true`
4. Also forwards to CloudWatch as a backup

---

## 6. The Key Challenge: Getting `service_name` Right in Loki

This was the most complex and time-consuming aspect of the Firelens approach. It took multiple failed attempts before the correct pattern was found.

### Why It Fails Without the R&D Team Pattern

The OpenAPM OTel Collector maps the OTLP **resource attribute** `service.name` to the Loki stream label `service_name`. 

Standard Firelens sends app stdout as raw log records with **empty OTLP resource attributes**. This means logs arrive in Loki under `{service_name="unknown_service"}` regardless of what labels you set on the ECS task.

### Attempts That Failed

| Attempt # | What Was Tried | Why It Failed |
|-----------|---------------|---------------|
| **1** | `add_label: "service.name sre-batch-telemetry-java"` in ECS Firelens options | ECS rejects label keys containing dots (`.`) — JSON parsing error |
| **2** | `add_label: "service_name sre-batch-telemetry-java"` | Sets an **HTTP-level Loki stream label**, not an OTLP resource attribute. The OpenAPM Collector ignores it for `service_name` mapping |
| **3** | `OTEL_SERVICE_NAME` env var on app container | The Fluent Bit sidecar has no access to the app container's environment variables |
| **4** | `aws-for-fluent-bit:stable` image | Fluent Bit **1.9.x** ships with this tag. The `opentelemetry` output plugin in 1.9.x has **no log support** — logs are silently dropped (metrics only) |
| **5** | `aws-for-fluent-bit:3.3.0` (Fluent Bit 5.0.3) | Logs started arriving but `service_name` was still `unknown_service` — no `logs_body_key_attributes` support found in standard config |
| **6** | `FirelensConfiguration` S3 config source | ECS infrastructure feature — **not available on Fargate**. Config is only applied when running on ECS EC2 |

### The Breakthrough — R&D Team Pattern

The solution was found by studying how the R&D team's `saas-platform-ms-notification-manager-dynamic-routed` service solved the identical problem for ECS Firelens.

**The pattern (4 components working together):**

```
Component 1: aws-for-fluent-bit:init-3.2.4 image
  The "init" variant downloads a custom Fluent Bit config from S3 at
  CONTAINER startup — this is a container-level operation, NOT ECS-infra.
  Works on Fargate ✅

Component 2: aws_fluent_bit_init_s3_1 env var (ARN format)
  Value: arn:aws:s3:::sre-batch-telemetry-code-723346695882/fluent-bit/batch-fluent-bit.conf
  ↑ Must be ARN format (not s3:// URI) — the init image requires ARN

Component 3: record_modifier filter in custom config
  Adds service_name, openapm_product_name, region as Fluent Bit RECORD FIELDS

Component 4: logs_body_key_attributes true in opentelemetry output
  This flag promotes Fluent Bit record fields to OTLP log RESOURCE ATTRIBUTES
  OpenAPM Collector reads resource attribute service_name → correct Loki label ✅
```

### Fluent Bit Version Comparison

| Image Tag | Fluent Bit Version | Log Support | S3 Config (Fargate) | Use? |
|-----------|-------------------|-------------|--------------------|----|
| `:stable` | 1.9.10 | ❌ Metrics only | ❌ No | ❌ |
| `:3.3.0` | 5.0.3 | ✅ Yes | ❌ No (ECS-infra only) | ❌ |
| `:init-3.2.4` | 4.x | ✅ Yes | ✅ Yes (container-level) | ✅ **Use this** |

---

## 7. Custom Fluent Bit Configuration

Stored at: `s3://sre-batch-telemetry-code-723346695882/fluent-bit/batch-fluent-bit.conf`  
ARN: `arn:aws:s3:::sre-batch-telemetry-code-723346695882/fluent-bit/batch-fluent-bit.conf`

```ini
[SERVICE]
  Log_Level  warn
  Flush      0.1

# ── INPUT ──────────────────────────────────────────────────────────────
# Receives app container stdout via Firelens Unix socket
[INPUT]
  Name           forward
  unix_path      /var/run/fluent.sock
  Mem_Buf_Limit  17M

# ── FILTER ─────────────────────────────────────────────────────────────
# record_modifier adds service_name etc. as Fluent Bit record fields.
# These are then promoted to OTLP resource attributes by logs_body_key_attributes true.
# This is the pattern from R&D team (notification-manager-dynamic-routed).
[FILTER]
  Name    record_modifier
  Match   *
  Record  service_name         ${SERVICE_NAME}
  Record  openapm_product_name ${PRODUCT_NAME}
  Record  region               ${REGION}

# ── OUTPUT: OpenTelemetry (OpenAPM) ────────────────────────────────────
# logs_body_key: the 'log' field from Firelens becomes the OTLP log record body
# logs_body_key_attributes true: ALL other record fields (service_name, etc.)
#   are promoted to OTLP log resource attributes.
#   The OpenAPM Collector reads service_name as an OTLP resource attribute
#   → maps it to the correct Loki stream label.
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

# ── OUTPUT: CloudWatch (fallback / backup) ─────────────────────────────
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

**How the OTLP resource attribute flow works:**

```
App stdout → Fluent Bit receives record:
  {
    "log": "2026-05-01 INFO BatchTelemetryApplication - Batch job complete",
    "container_name": "app",
    "source": "stdout"
  }

After record_modifier filter:
  {
    "log": "...",
    "container_name": "app",
    "source": "stdout",
    "service_name": "sre-batch-telemetry-java",      ← added by filter
    "openapm_product_name": "sre-batch-telemetry",   ← added by filter
    "region": "us-west-2"                            ← added by filter
  }

After opentelemetry output (logs_body_key_attributes true):
  OTLP LogRecord {
    body: "2026-05-01 INFO BatchTelemetryApplication - Batch job complete"
    resourceLogs.resource.attributes: {
      service_name = "sre-batch-telemetry-java"      ← promoted to OTLP resource attr
      openapm_product_name = "sre-batch-telemetry"
      region = "us-west-2"
    }
  }

OpenAPM Collector receives OTLP resource attr service_name:
  → Maps to Loki stream label service_name = "sre-batch-telemetry-java" ✅
```

---

## 8. CloudFormation Job Definition — Dual Container

Key excerpts from `cloudformation/batch-job-definition-java-firelens.yaml`:

```yaml
# ── Task-level resource allocation ──────────────────────────────────────
# MUST equal a valid Fargate task size (1 vCPU / 2048 MiB)
# app (0.75) + log_router (0.25) = 1.0 vCPU total ✅
PlatformCapabilities: [FARGATE]
NetworkConfiguration:
  AssignPublicIp: DISABLED

# ── App container ────────────────────────────────────────────────────────
ContainerProperties:
  - Name: app
    Image: public.ecr.aws/amazoncorretto/amazoncorretto:17
    ResourceRequirements:
      - { Type: VCPU,   Value: "0.75"  }    # ← NOT 1.0
      - { Type: MEMORY, Value: "1920"  }    # ← NOT 2048
    Command:
      - sh
      - -c
      - "aws s3 cp s3://sre-batch-telemetry-code-723346695882/java/batch-telemetry.jar
         /app.jar --region us-west-2 && java -jar /app.jar"
    Environment:
      - { Name: OTEL_SERVICE_NAME,              Value: !Ref ServiceName }
      - { Name: OTEL_EXPORTER_OTLP_ENDPOINT,   Value: !Ref OtelEndpoint }
      - { Name: OTEL_EXPORTER_OTLP_PROTOCOL,   Value: http/protobuf }
      - { Name: OTEL_RESOURCE_ATTRIBUTES,       Value: !Sub "..." }
    LogConfiguration:
      LogDriver: awsfirelens     # ← Routes stdout to log_router sidecar

# ── log_router sidecar ───────────────────────────────────────────────────
  - Name: log_router
    Image: public.ecr.aws/aws-observability/aws-for-fluent-bit:init-3.2.4
    Essential: false             # Task continues if sidecar exits
    ResourceRequirements:
      - { Type: VCPU,   Value: "0.25" }
      - { Type: MEMORY, Value: "128"  }
    FirelensConfiguration:
      Type: fluentbit
    Environment:
      - Name: aws_fluent_bit_init_s3_1
        # ← ARN format (NOT s3:// URI — init image requires ARN)
        Value: arn:aws:s3:::sre-batch-telemetry-code-723346695882/fluent-bit/batch-fluent-bit.conf
      - { Name: SERVICE_NAME, Value: !Ref ServiceName }
      - { Name: PRODUCT_NAME, Value: !Ref OpenapmProductName }
      - { Name: REGION,       Value: !Ref AWS::Region }
      - { Name: APM_HOST,     Value: apm-na1.mon-sandbox.nicecxone-sbx.com }
```

---

## 9. Build and Deploy

### 9.1 Build the Fat JAR

```bash
cd java-poc
mvn clean package -DskipTests
# → target/batch-telemetry.jar (~23 MB)
```

### 9.2 Upload to S3 via CloudShell

```bash
# In CloudShell: use "Actions → Upload file" to upload batch-telemetry.jar
# Then push to S3:
aws s3 cp ~/batch-telemetry.jar \
  s3://sre-batch-telemetry-code-723346695882/java/batch-telemetry.jar \
  --region us-west-2

# Upload Fluent Bit config
aws s3 cp batch-fluent-bit.conf \
  s3://sre-batch-telemetry-code-723346695882/fluent-bit/batch-fluent-bit.conf \
  --region us-west-2
```

> **Why CloudShell upload instead of `curl`?**  
> GitHub CDN (`raw.githubusercontent.com`) caches responses. Downloading templates via `curl` can serve stale content even after a commit. The CloudShell upload button bypasses CDN entirely.

### 9.3 Deploy CloudFormation (Firelens Job Definition)

```bash
# Ensure shared stacks already deployed:
# sre-batch-iam-dev, sre-batch-sg-dev, sre-batch-ce-dev, sre-batch-jq-dev

# Get role ARNs
EXEC_ROLE=$(aws cloudformation describe-stacks \
  --stack-name sre-batch-iam-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue" \
  --output text --region us-west-2)

JOB_ROLE=$(aws cloudformation describe-stacks \
  --stack-name sre-batch-iam-dev \
  --query "Stacks[0].Outputs[?OutputKey=='JobRoleArn'].OutputValue" \
  --output text --region us-west-2)

# Deploy Firelens job definition
aws cloudformation deploy \
  --stack-name sre-batch-java-firelens-jobdef \
  --template-file cloudformation/batch-job-definition-java-firelens.yaml \
  --parameter-overrides \
    ExecutionRoleArn=$EXEC_ROLE \
    JobRoleArn=$JOB_ROLE \
    ServiceName=sre-batch-telemetry-java \
    OpenapmProductName=sre-batch-telemetry \
  --region us-west-2
```

### 9.4 Submit and Monitor

```bash
JOB_ID=$(aws batch submit-job \
  --job-name sre-batch-java-firelens-$(date +%Y%m%d%H%M%S) \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-java-dev-firelens-job \
  --region us-west-2 \
  --query 'jobId' --output text)

echo "Submitted: $JOB_ID"

# Watch status
watch -n 10 "aws batch describe-jobs \
  --jobs $JOB_ID --region us-west-2 \
  --query 'jobs[0].[status,statusReason]' --output text"
```

---

## 10. Validation — All 3 Signals Confirmed

**Confirmed jobs:**
- `91ef7036` — init image + ARN fix + IAM fix — **SUCCEEDED** ✅
- `7dd4a30b` — metrics step boundary fix — **SUCCEEDED** ✅

### Traces in Tempo

```
Query: {service.name="sre-batch-telemetry-java"}
```

**Confirmed:**
- ✅ 4 spans per job run
- ✅ `service.name=sre-batch-telemetry-java` on all spans
- ✅ Correct hierarchy: `batch-job-execution → fetch-data / process-data / write-results`
- ✅ Span attributes: `job.items_processed`, `job.status`

### Metrics in Mimir

```promql
job_items_processed_total{service_name="sre-batch-telemetry-java"}
job_duration_seconds_sum{service_name="sre-batch-telemetry-java"}
job_status{service_name="sre-batch-telemetry-java"}
```

**Confirmed:**
- ✅ All three metrics present with correct labels
- ✅ `job_status=0` (success)
- ✅ `openapm_product_name=sre-batch-telemetry` label present

### Logs in Loki

```logql
{service_name="sre-batch-telemetry-java"}
```

**Confirmed:**
- ✅ `service_name=sre-batch-telemetry-java` — correct Loki stream label
- ✅ `openapm_product_name`, `region` labels indexed correctly
- ✅ Log body contains `traceId` and `spanId` from Micrometer MDC
- ✅ `container_name=app`, `source=stdout` fields present

---

## 11. Blockers Encountered — Full Timeline

### Stage 1 — Initial Setup Blockers

**B1: VPC Endpoint SG had no inbound rules**
- Symptom: Job SUCCEEDED, zero telemetry in Grafana
- Fix: Added TCP 4318 inbound from VPC CIDR to VPC Endpoint SG

**B2: gRPC unsupported through VPC Endpoint**
- Symptom: `UNAVAILABLE` on OTLP exporter
- Fix: `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, port 4318

---

### Stage 2 — Firelens `service_name` Challenge

**B3: `add_label` with dot in key rejected**
- Tried: `add_label: "service.name sre-batch-telemetry-java"`
- Error: ECS/JSON rejected key containing `.`
- Learned: ECS Firelens options are parsed as JSON; dots in keys are invalid

**B4: `add_label` with underscore ignored by Collector**
- Tried: `add_label: "service_name sre-batch-telemetry-java"`
- Result: Logs arrived in Loki under `unknown_service`
- Root cause: `add_label` sets an HTTP-level Loki stream label. The OpenAPM Collector maps OTLP **resource attributes** to Loki labels — it ignores HTTP stream labels for `service_name`.

**B5: `OTEL_SERVICE_NAME` on app container — sidecar can't read it**
- Tried: Setting `OTEL_SERVICE_NAME` as env var on app container, hoping Firelens picks it up
- Result: Firelens sidecar has no access to app container env vars

**B6: `aws-for-fluent-bit:stable` silently drops logs**
- Symptom: No logs in Loki at all
- Root cause: `:stable` ships Fluent Bit 1.9.x. The `opentelemetry` output plugin in 1.9.x handles metrics only — no log support. Logs were silently discarded.
- Discovery: Checked Fluent Bit changelog and plugin documentation

**B7: `FirelensConfiguration` S3 config blocked on Fargate**
- Tried: Adding S3 config source to `FirelensConfiguration` block in task definition
- Error: CloudFormation deployed but config was never downloaded
- Root cause: The `FirelensConfiguration.Options.config-file-type=s3` is an ECS infrastructure feature that works only on ECS EC2, not Fargate.

**B8: Breakthrough — `init-3.2.4` + R&D team pattern**
- Found: `aws-for-fluent-bit:init-3.2.4` downloads config via container-level env var `aws_fluent_bit_init_s3_1` at startup — not ECS-infra level → works on Fargate
- Found: `record_modifier` filter + `logs_body_key_attributes true` promotes record fields to OTLP resource attributes → OpenAPM Collector maps correctly to Loki label
- Source: R&D team `saas-platform-ms-notification-manager-dynamic-routed` service

---

### Stage 3 — CloudFormation Deploy Errors

**B9: `Could not parse arn: s3://...`**
- Cause: Init image env var `aws_fluent_bit_init_s3_1` requires **ARN format**, not S3 URI
- Fix: Changed `s3://bucket/key` → `arn:aws:s3:::bucket/key`

**B10: `s3:GetBucketLocation AccessDenied`**
- Cause: Job role missing this S3 permission
- Fix: Added `s3:GetBucketLocation` to IAM Job Role policy

**B11: `CreateLogStream AccessDenied`**
- Cause: Job role missing CloudWatch Logs permissions
- Fix: Added `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents` to Job Role

**B12: `EarlyValidation::ResourceExistenceCheck`**
- Cause: CloudWatch log group already existed from a different CloudFormation stack
- Fix: Removed `AWS::Logs::LogGroup` resource from template — CloudWatch auto-creates log groups

**B13: `Unresolved resource dependencies [FirelensLogGroup]`**
- Cause: After removing the log group resource, Outputs block still referenced `!Ref FirelensLogGroup`
- Fix: Replaced with hardcoded literal string

**B14: `Fargate resource requirements (1.25 vCPU) not valid`**
- Cause: app (1.0 vCPU) + sidecar (0.25 vCPU) = 1.25 — not a valid Fargate task size
- Fix: Set app to `0.75 vCPU / 1920 MiB` → total = exactly `1 vCPU / 2048 MiB`

**B15: `!Sub` not evaluated by EarlyValidation hook**
- Cause: CF EarlyValidation hook (org-wide) does not resolve intrinsic functions
- Fix: Hardcoded log group name as a literal string

**B16: GitHub CDN cache served stale template**
- Cause: `curl https://raw.githubusercontent.com/.../template.yaml` returned a cached version after file was updated
- Fix: Used CloudShell "Actions → Upload file" button to upload the file directly

---

**Total blockers resolved in Firelens POC: 16**

---

## 12. Firelens vs Direct OTel — Side-by-Side Comparison

| Dimension | Direct OTel (Approach A) | Firelens / Architect's (Approach B) |
|-----------|--------------------------|----------------------------------|
| **`service_name` in Loki** | ✅ Correct | ✅ Correct |
| **Trace-log correlation** | ✅ `traceId`+`spanId` in every log | ✅ `traceId`+`spanId` via MDC |
| **ECS microservice consistency** | ❌ App-level change | ✅ Same pattern as ECS microservices |
| **Containers** | 1 | 2 |
| **App code change needed for logs** | Yes (add `SdkLoggerProvider`) | No — stdout captured automatically |
| **Infrastructure complexity** | Low | Medium (sidecar, S3 config, IAM) |
| **Fluent Bit version dependency** | None | `init-3.2.4` required specifically |
| **Spring Boot 3.3 quirks** | Yes (no-op log provider) | No — log routing is outside the app |
| **Additional IAM permissions** | None | `s3:GetBucketLocation`, CloudWatch Logs |
| **Fargate task sizing constraint** | 1 vCPU / 2048 MiB | 0.75+0.25 vCPU / 1920+128 MiB |
| **Recommended for** | New services, simplicity preferred | ECS-aligned teams, infrastructure log routing |

---

## 13. Files in This POC

| File | Purpose |
|------|---------|
| `java-poc/pom.xml` | Spring Boot 3.3.13 + Micrometer + OTel dependencies (no log appender dep here) |
| `java-poc/src/main/java/.../BatchTelemetryApplication.java` | CommandLineRunner with traces + metrics (stdout logs only) |
| `java-poc/src/main/resources/application.yml` | Micrometer tracing + metrics OTLP config |
| `java-poc/src/main/resources/logback-spring.xml` | CONSOLE appender only — no OTelAppender |
| `docker/fluent-bit/batch-fluent-bit.conf` | Custom Fluent Bit config (record_modifier + opentelemetry output) |
| `cloudformation/batch-job-definition-java-firelens.yaml` | Multi-container CF template (app + log_router) |
| `cloudformation/iam-roles.yaml` | IAM roles with S3+CloudWatch Logs permissions |
| `docs/FIRELENS-EVIDENCE.md` | Full evidence doc with confirmed job IDs and Grafana results |

---

*Related documents:*
- [01-AWS-BATCH-OPENAPM-GUIDELINES.md](01-AWS-BATCH-OPENAPM-GUIDELINES.md) — Standards & constraints
- [02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md](02-AWS-BATCH-OPENAPM-DEVELOPER-REFERENCE.md) — Developer reference
- [03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md](03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md) — Architecture overview
- [04-POC-PYTHON-SINGLE-CONTAINER.md](04-POC-PYTHON-SINGLE-CONTAINER.md) — Python POC
- [FIRELENS-EVIDENCE.md](FIRELENS-EVIDENCE.md) — Grafana evidence screenshots and confirmed job IDs

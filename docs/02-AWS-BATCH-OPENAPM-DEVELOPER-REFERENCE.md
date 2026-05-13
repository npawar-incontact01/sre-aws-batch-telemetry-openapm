# AWS Batch — OpenAPM Integration: Developer Quick-Reference

> **Document Type:** Developer Reference  
> **Audience:** Application Developers, Data Engineers integrating Batch jobs with OpenAPM  
> **Scope:** Java and Python AWS Batch jobs on Fargate  
> **Prerequisites:** Read [01-AWS-BATCH-OPENAPM-GUIDELINES.md](01-AWS-BATCH-OPENAPM-GUIDELINES.md) for standards  
> **Status:** Validated — May 2026

---

## Table of Contents

1. [5-Minute Quick Start](#1-5-minute-quick-start)
2. [Java Integration — Step by Step](#2-java-integration--step-by-step)
3. [Python Integration — Step by Step](#3-python-integration--step-by-step)
4. [CloudFormation Job Definition Templates](#4-cloudformation-job-definition-templates)
5. [Build and Deploy — Java](#5-build-and-deploy--java)
6. [Build and Deploy — Python](#6-build-and-deploy--python)
7. [Submit a Job and Validate](#7-submit-a-job-and-validate)
8. [Grafana Queries Reference](#8-grafana-queries-reference)
9. [Common Errors and Fixes](#9-common-errors-and-fixes)
10. [Environment Variable Reference](#10-environment-variable-reference)

---

## 1. 5-Minute Quick Start

### What you need

- AWS CloudShell access to your account (MFA session active)
- S3 bucket for code upload (or use `sre-batch-telemetry-code-723346695882` in mon-sandbox)
- Access to Grafana OpenAPM instance

### Choose your path

```
Are you writing Java or Python?
    ├── Python → Section 3 (4 lines of code + env vars)
    └── Java   → Section 2 (pom.xml deps + SdkLoggerProvider wiring)

Which approach?
    ├── Single container (simpler)  → Approach A (Direct OTel)
    └── Dual container / Firelens  → Approach B (Architect's pattern)
```

---

## 2. Java Integration — Step by Step

### 2.1 Maven Dependencies (`pom.xml`)

Add the Spring Boot parent and BOM:

```xml
<parent>
    <groupId>org.springframework.boot</groupId>
    <artifactId>spring-boot-starter-parent</artifactId>
    <version>3.3.13</version>
</parent>

<dependencyManagement>
    <dependencies>
        <dependency>
            <groupId>io.opentelemetry</groupId>
            <artifactId>opentelemetry-bom</artifactId>
            <version>1.39.0</version>
            <type>pom</type>
            <scope>import</scope>
        </dependency>
        <!-- CVE-2024-7254 fix — pin protobuf -->
        <dependency>
            <groupId>com.google.protobuf</groupId>
            <artifactId>protobuf-java</artifactId>
            <version>3.25.5</version>
        </dependency>
    </dependencies>
</dependencyManagement>

<dependencies>
    <dependency>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-actuator</artifactId>
    </dependency>
    <!-- Traces -->
    <dependency>
        <groupId>io.micrometer</groupId>
        <artifactId>micrometer-tracing-bridge-otel</artifactId>
    </dependency>
    <dependency>
        <groupId>io.opentelemetry</groupId>
        <artifactId>opentelemetry-exporter-otlp</artifactId>
        <version>1.39.0</version>
    </dependency>
    <!-- Metrics -->
    <dependency>
        <groupId>io.micrometer</groupId>
        <artifactId>micrometer-registry-otlp</artifactId>
    </dependency>
    <!-- Logs -->
    <dependency>
        <groupId>io.opentelemetry.instrumentation</groupId>
        <artifactId>opentelemetry-logback-appender-1.0</artifactId>
        <version>2.4.0-alpha</version>
    </dependency>
</dependencies>
```

### 2.2 `application.yml` Configuration

```yaml
spring:
  application:
    name: ${OTEL_SERVICE_NAME:your-batch-job}
  main:
    web-application-type: none   # Batch job — no web server

management:
  tracing:
    sampling:
      probability: 1.0           # 100% sampling for batch jobs
  otlp:
    tracing:
      endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces
    metrics:
      export:
        url: ${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics
        step: 10s
        resource-attributes:
          service.name:          ${OTEL_SERVICE_NAME:your-batch-job}
          service_name:          ${OTEL_SERVICE_NAME:your-batch-job}
          environment:           ${ENVIRONMENT:mon-sandbox}
          openapm_product_name:  ${OPENAPM_PRODUCT_NAME:your-product}
          region:                ${AWS_REGION:us-west-2}
          account.id:            ${AWS_ACCOUNT_ID:723346695882}
```

### 2.3 `logback-spring.xml`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <appender name="CONSOLE" class="ch.qos.logback.core.ConsoleAppender">
        <encoder>
            <pattern>%d{ISO8601} %-5level [%thread] %logger{36} - %msg%n</pattern>
        </encoder>
    </appender>

    <appender name="OpenTelemetry"
              class="io.opentelemetry.instrumentation.logback.appender.v1_0.OpenTelemetryAppender">
        <captureExperimentalAttributes>true</captureExperimentalAttributes>
        <captureCodeAttributes>true</captureCodeAttributes>
        <captureMdcAttributes>traceId,spanId</captureMdcAttributes>
    </appender>

    <root level="INFO">
        <appender-ref ref="CONSOLE"/>
        <appender-ref ref="OpenTelemetry"/>
    </root>
</configuration>
```

### 2.4 Main Application Class — Critical Log Wiring

> **Why this is needed:** Spring Boot 3.3's `OpenTelemetry` auto-configured bean has **no `SdkLoggerProvider`** — it silently drops all log records. You must build your own before Spring starts logging.

```java
package com.nice.sre.batch;

import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.common.AttributesBuilder;
import io.opentelemetry.exporter.otlp.http.logs.OtlpHttpLogRecordExporter;
import io.opentelemetry.instrumentation.logback.appender.v1_0.OpenTelemetryAppender;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.logs.SdkLoggerProvider;
import io.opentelemetry.sdk.logs.export.BatchLogRecordProcessor;
import io.opentelemetry.sdk.resources.Resource;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.micrometer.tracing.Tracer;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

import java.util.concurrent.atomic.AtomicInteger;

@SpringBootApplication
public class BatchTelemetryApplication implements CommandLineRunner {

    private final Tracer tracer;
    private final MeterRegistry meterRegistry;
    private final SdkLoggerProvider loggerProvider;
    private static final org.slf4j.Logger log =
        org.slf4j.LoggerFactory.getLogger(BatchTelemetryApplication.class);

    // ── IMPORTANT: install the log appender BEFORE Spring starts ──
    static {
        SdkLoggerProvider lp = buildLoggerProvider();
        OpenTelemetryAppender.install(
            OpenTelemetrySdk.builder().setLoggerProvider(lp).build()
        );
    }

    public BatchTelemetryApplication(Tracer tracer, MeterRegistry meterRegistry) {
        this.tracer = tracer;
        this.meterRegistry = meterRegistry;
        this.loggerProvider = buildLoggerProvider();
    }

    public static void main(String[] args) {
        SpringApplication.run(BatchTelemetryApplication.class, args);
    }

    @Override
    public void run(String... args) throws Exception {
        // Metrics setup
        Timer jobTimer = Timer.builder("job_duration_seconds")
            .description("Total job execution time").register(meterRegistry);
        Counter itemsCounter = Counter.builder("job_items_processed_total")
            .description("Items processed").register(meterRegistry);
        AtomicInteger jobStatus = new AtomicInteger(1); // 1=error until success
        meterRegistry.gauge("job_status", jobStatus);

        // Root span for full job
        var span = tracer.nextSpan().name("batch-job-execution").start();
        try (var ws = tracer.withSpan(span)) {
            jobTimer.record(() -> {
                runPhase("fetch-data", 5_000);
                runPhase("process-data", 5_000);
                runPhase("write-results", 5_000);
                itemsCounter.increment(100);
            });
            jobStatus.set(0); // success
            log.info("Batch job completed successfully. items_processed=100");
        } finally {
            span.end();
            flushTelemetry();
            Thread.sleep(15_000); // Allow BatchSpanProcessor to complete final flush
        }
    }

    private void runPhase(String name, long sleepMs) {
        var span = tracer.nextSpan().name(name).start();
        try (var ws = tracer.withSpan(span)) {
            Thread.sleep(sleepMs);
            log.info("Phase '{}' completed", name);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            span.end();
        }
    }

    private void flushTelemetry() {
        try {
            if (meterRegistry instanceof AutoCloseable c) c.close();
        } catch (Exception e) {
            log.warn("Error closing MeterRegistry", e);
        }
        loggerProvider.forceFlush();
    }

    /**
     * Build a dedicated SdkLoggerProvider from OTEL_* environment variables.
     *
     * Required because Spring Boot 3.3 auto-configured OpenTelemetry bean
     * does NOT include a SdkLoggerProvider — it is a no-op for logs.
     * Fixed in Spring Boot 3.4+.
     */
    private static SdkLoggerProvider buildLoggerProvider() {
        String endpoint = System.getenv()
            .getOrDefault("OTEL_EXPORTER_OTLP_ENDPOINT",
                          "https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318");
        String serviceName = System.getenv()
            .getOrDefault("OTEL_SERVICE_NAME", "unknown-batch-job");

        AttributesBuilder attrs = Attributes.builder()
            .put(AttributeKey.stringKey("service.name"), serviceName);

        String rawAttrs = System.getenv("OTEL_RESOURCE_ATTRIBUTES");
        if (rawAttrs != null && !rawAttrs.isBlank()) {
            for (String pair : rawAttrs.split(",")) {
                String[] kv = pair.split("=", 2);
                if (kv.length == 2) {
                    attrs.put(AttributeKey.stringKey(kv[0].trim()), kv[1].trim());
                }
            }
        }

        return SdkLoggerProvider.builder()
            .setResource(Resource.getDefault()
                .merge(Resource.create(attrs.build())))
            .addLogRecordProcessor(
                BatchLogRecordProcessor.builder(
                    OtlpHttpLogRecordExporter.builder()
                        .setEndpoint(endpoint + "/v1/logs")
                        .build()
                ).build()
            )
            .build();
    }
}
```

### 2.5 Custom Metrics Patterns

```java
// Timer — measures duration
Timer jobTimer = Timer.builder("job_duration_seconds")
    .description("Total job execution time")
    .register(meterRegistry);
jobTimer.record(() -> { /* job logic */ });

// Counter — counts events
Counter itemsCounter = Counter.builder("job_items_processed_total")
    .description("Number of items processed")
    .register(meterRegistry);
itemsCounter.increment(numberOfItems);

// Gauge — current value (IMPORTANT: use AtomicInteger to prevent GC)
AtomicInteger jobStatus = new AtomicInteger(1); // 1=error by default
meterRegistry.gauge("job_status", jobStatus);
jobStatus.set(0); // set to 0 on success
```

> **Why `AtomicInteger` for gauge?** Micrometer stores gauge values via `WeakReference`. If you pass a regular `int` or `Integer`, the GC collects it and the gauge stops reporting. `AtomicInteger` prevents this.

---

## 3. Python Integration — Step by Step

### 3.1 Requirements

Add to `requirements.txt`:
```
opentelemetry-sdk==1.24.0
opentelemetry-exporter-otlp-proto-http==1.24.0
opentelemetry-semantic-conventions==0.45b0
```

### 3.2 Using the `batch_otel` Library (Recommended)

Copy `src/batch_otel/` into your project, then:

```python
from batch_otel import init_telemetry, shutdown_telemetry
import logging

logger = logging.getLogger("my_job")

def main():
    # Reads all OTEL_* env vars automatically
    otel = init_telemetry()

    with otel.tracer.start_as_current_span("my-job-execution") as span:
        records = fetch_data(otel.tracer, span)
        processed = process_data(otel.tracer, records)

        # Metrics
        otel.meter.create_counter("job_items_processed_total").add(len(processed))

        # Logs with automatic traceId/spanId injection
        otel.logger.info("Job complete: processed %d items", len(processed))
        span.set_attribute("job.items_processed", len(processed))
        span.set_attribute("job.status", "success")

    shutdown_telemetry(otel)   # ← ALWAYS call this — flushes all signals before exit

if __name__ == "__main__":
    main()
```

### 3.3 Manual Setup (Without `batch_otel` Library)

```python
import os, logging, time, random
from opentelemetry import trace, metrics
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource

ENDPOINT  = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT",
                            "https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318")
SVC_NAME  = os.environ.get("OTEL_SERVICE_NAME", "my-batch-job")
RES_ATTRS = os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")

# Parse resource attributes
attrs = {"service.name": SVC_NAME}
for pair in RES_ATTRS.split(","):
    if "=" in pair:
        k, _, v = pair.partition("=")
        attrs[k.strip()] = v.strip()

resource = Resource.create(attrs)

# Traces
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=ENDPOINT + "/v1/traces")))
trace.set_tracer_provider(tracer_provider)

# Metrics
meter_provider = MeterProvider(
    resource=resource,
    metric_readers=[PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=ENDPOINT + "/v1/metrics"),
        export_interval_millis=10_000)])
metrics.set_meter_provider(meter_provider)

# Logs
log_provider = LoggerProvider(resource=resource)
log_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=ENDPOINT + "/v1/logs")))
set_logger_provider(log_provider)
logging.getLogger().addHandler(LoggingHandler(logger_provider=log_provider))

tracer = trace.get_tracer(SVC_NAME)
meter  = metrics.get_meter(SVC_NAME)

# ── Your job logic here ──

# Shutdown — flush everything before exit
tracer_provider.shutdown()
meter_provider.shutdown()
log_provider.shutdown()
```

### 3.4 Retrofit Pattern (Minimal Changes to Existing Jobs)

```python
# Add at the very top of your existing main() function:
from batch_otel import init_telemetry, shutdown_telemetry
otel = init_telemetry()

# Wrap your existing code in a root span:
with otel.tracer.start_as_current_span("your-existing-job"):
    # ← paste your existing code here, unchanged ←
    pass

# Add at the very bottom:
shutdown_telemetry(otel)
```

---

## 4. CloudFormation Job Definition Templates

### 4.1 Java — Single Container (Approach A)

Key parameters for `cloudformation/batch-job-definition-java.yaml`:

```yaml
Parameters:
  ServiceName: sre-batch-telemetry-java
  EnvironmentName: mon-sandbox
  ContainerImage: public.ecr.aws/amazoncorretto/amazoncorretto:17
  ExecutionRoleArn: arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-execution-role
  JobRoleArn: arn:aws:iam::723346695882:role/sre-aws-batch-telemetry-dev-job-role
  S3JarBucket: sre-batch-telemetry-code-723346695882
  S3JarKey: java/batch-telemetry.jar
  OpenapmProductName: sre-batch-telemetry
  JobVcpu: "1"           # Must be 1 for single-container Fargate task
  JobMemory: "2048"      # Must be 2048 for 1 vCPU Fargate task
```

Key container environment variables set by the template:
```yaml
Environment:
  - Name: OTEL_SERVICE_NAME
    Value: !Ref ServiceName
  - Name: OTEL_EXPORTER_OTLP_ENDPOINT
    Value: https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318
  - Name: OTEL_EXPORTER_OTLP_PROTOCOL
    Value: http/protobuf
  - Name: OTEL_RESOURCE_ATTRIBUTES
    Value: !Sub >-
      environment=${EnvironmentName},
      account.id=${AWS::AccountId},
      openapm_product_name=${OpenapmProductName},
      service_name=${ServiceName},
      region=${AWS::Region}
```

Container command (downloads JAR from S3, no ECR needed):
```yaml
Command:
  - sh
  - -c
  - !Sub >-
    aws s3 cp s3://${S3JarBucket}/${S3JarKey} /app.jar --region ${AWS::Region}
    && java -jar /app.jar
```

### 4.2 Java — Dual Container / Firelens (Approach B)

Critical sizing constraint — total must equal a valid Fargate task size:

```yaml
# Task-level resources
ResourceRequirements:
  - Type: VCPU
    Value: "1"         # Total task = 1 vCPU / 2048 MiB
  - Type: MEMORY
    Value: "2048"

# App container
AppContainer:
  ResourceRequirements:
    - Type: VCPU
      Value: "0.75"    # 0.75 for app, leaving 0.25 for sidecar
    - Type: MEMORY
      Value: "1920"

# log_router sidecar
LogRouterContainer:
  Image: public.ecr.aws/aws-observability/aws-for-fluent-bit:init-3.2.4
  ResourceRequirements:
    - Type: VCPU
      Value: "0.25"
    - Type: MEMORY
      Value: "128"
  Environment:
    - Name: aws_fluent_bit_init_s3_1
      Value: arn:aws:s3:::sre-batch-telemetry-code-723346695882/fluent-bit/batch-fluent-bit.conf
    - Name: SERVICE_NAME
      Value: !Ref ServiceName
    - Name: PRODUCT_NAME
      Value: !Ref OpenapmProductName
    - Name: REGION
      Value: !Ref AWS::Region
    - Name: APM_HOST
      Value: apm-na1.mon-sandbox.nicecxone-sbx.com
```

> ⚠️ **Fargate task sizing rule:** `app_vcpu + sidecar_vcpu` must equal a valid Fargate size (`0.25`, `0.5`, `1`, `2`, `4`). `1.0 + 0.25 = 1.25` is **not valid** and CloudFormation will fail with `ClientException`. Use `0.75 + 0.25 = 1.0`.

---

## 5. Build and Deploy — Java

### 5.1 Build Fat JAR Locally

```bash
cd java-poc
mvn clean package -DskipTests
# Output: target/batch-telemetry.jar (~23 MB)
```

### 5.2 Upload to CloudShell and Push to S3

```bash
# In CloudShell — after uploading the JAR via "Actions → Upload file":
aws s3 cp ~/batch-telemetry.jar \
  s3://sre-batch-telemetry-code-723346695882/java/batch-telemetry.jar \
  --region us-west-2
```

> **Why CloudShell?** ECR is blocked and all AWS CLI operations require MFA. CloudShell already has MFA via console login.

> **Why "Actions → Upload file" instead of `curl`?** Downloading from `raw.githubusercontent.com` is cached by the GitHub CDN — you may get a stale version of the template. Use the CloudShell upload button for template files.

### 5.3 Deploy CloudFormation Stacks (In Order)

```bash
# 1. IAM Roles
aws cloudformation deploy \
  --stack-name sre-batch-iam-dev \
  --template-file cloudformation/iam-roles.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-west-2

# 2. Security Group
aws cloudformation deploy \
  --stack-name sre-batch-sg-dev \
  --template-file cloudformation/security-groups.yaml \
  --region us-west-2

# 3. Compute Environment
aws cloudformation deploy \
  --stack-name sre-batch-ce-dev \
  --template-file cloudformation/batch-compute-environment.yaml \
  --region us-west-2

# 4. Job Queue
aws cloudformation deploy \
  --stack-name sre-batch-jq-dev \
  --template-file cloudformation/batch-job-queue.yaml \
  --region us-west-2

# 5. Java Job Definition (Approach A — single container)
aws cloudformation deploy \
  --stack-name sre-batch-java-dev-jobdef \
  --template-file cloudformation/batch-job-definition-java.yaml \
  --parameter-overrides \
    ExecutionRoleArn=$(aws cloudformation describe-stacks \
      --stack-name sre-batch-iam-dev \
      --query "Stacks[0].Outputs[?OutputKey=='ExecutionRoleArn'].OutputValue" \
      --output text --region us-west-2) \
    JobRoleArn=$(aws cloudformation describe-stacks \
      --stack-name sre-batch-iam-dev \
      --query "Stacks[0].Outputs[?OutputKey=='JobRoleArn'].OutputValue" \
      --output text --region us-west-2) \
  --region us-west-2
```

---

## 6. Build and Deploy — Python

### 6.1 Upload Code to S3

```bash
# From CloudShell (after uploading files via "Actions → Upload file"):
aws s3 cp batch_job.py \
  s3://sre-batch-telemetry-code-723346695882/python/batch_job.py
aws s3 cp requirements.txt \
  s3://sre-batch-telemetry-code-723346695882/python/requirements.txt
```

### 6.2 Deploy CloudFormation

```bash
aws cloudformation deploy \
  --stack-name sre-batch-python-dev-jobdef \
  --template-file cloudformation/batch-job-definition.yaml \
  --parameter-overrides \
    ExecutionRoleArn=<arn> \
    JobRoleArn=<arn> \
  --region us-west-2
```

The Python job definition uses a container command like:
```bash
sh -c "pip install awscli -q &&
       aws s3 cp s3://sre-batch-telemetry-code-723346695882/python/ . --recursive &&
       pip install -r requirements.txt -q &&
       python3 batch_job.py"
```

---

## 7. Submit a Job and Validate

### 7.1 Submit

```bash
# Java (Approach A)
aws batch submit-job \
  --job-name sre-batch-java-test \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-java-dev-job \
  --region us-west-2

# Python
aws batch submit-job \
  --job-name sre-batch-python-test \
  --job-queue sre-aws-batch-telemetry-dev-queue \
  --job-definition sre-batch-telemetry-python-dev-job \
  --region us-west-2
```

### 7.2 Monitor Status

```bash
# Watch job transitions: SUBMITTED → PENDING → RUNNABLE → STARTING → RUNNING → SUCCEEDED
aws batch describe-jobs \
  --jobs <JOB_ID> \
  --region us-west-2 \
  --query 'jobs[0].[status,statusReason]' \
  --output text
```

### 7.3 Check CloudWatch Logs (Fallback Debugging)

```bash
aws logs tail /aws/batch/sre-batch-telemetry-java/app \
  --follow \
  --region us-west-2
```

---

## 8. Grafana Queries Reference

### Traces (Explore → Tempo)
```
{service.name="sre-batch-telemetry-java"}
```

### Metrics (Explore → Mimir)
```promql
# Job success/failure (0=success, 1=error)
job_status{service_name="sre-batch-telemetry-java"}

# Total items processed
job_items_processed_total{service_name="sre-batch-telemetry-java"}

# Job duration (last run)
job_duration_seconds_sum{service_name="sre-batch-telemetry-java"}
  / job_duration_seconds_count{service_name="sre-batch-telemetry-java"}

# Filter by product
{openapm_product_name="sre-batch-telemetry"}
```

### Logs (Explore → Loki)
```logql
# All logs for service
{service_name="sre-batch-telemetry-java"}

# Errors only
{service_name="sre-batch-telemetry-java"} |= "error"

# Correlate with a specific trace (copy traceId from Tempo)
{service_name="sre-batch-telemetry-java"} |= "abc123traceId"
```

### Trace → Log Correlation

In Grafana Explore (Tempo):
1. Open a trace
2. Click any span
3. Click **"Logs for this span"** (requires Loki datasource linked)
4. Grafana automatically queries Loki for `traceId=<that-trace-id>`

---

## 9. Common Errors and Fixes

| Error / Symptom | Root Cause | Fix |
|----------------|-----------|-----|
| Job SUCCEEDED but zero telemetry in Grafana | VPC Endpoint SG has no inbound rules | Add TCP 4318 inbound from VPC CIDR `10.0.0.0/21` to VPC Endpoint SG |
| `UNAVAILABLE` on OTLP exporter | gRPC (port 4317) not supported | Set `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, use port 4318 |
| Java logs `service_name=unknown_service` | Spring Boot 3.3 no-op `SdkLoggerProvider` | Build dedicated `SdkLoggerProvider` from env vars (see Section 2.4) |
| Firelens logs `service_name=unknown_service` | `add_label` sets HTTP label, not OTLP resource attr | Use `init-3.2.4` image + `record_modifier` + `logs_body_key_attributes true` |
| `Fargate resource 1.25 vCPU not valid` (CF) | app 1.0 + sidecar 0.25 = 1.25, not valid | Set app to `0.75 vCPU / 1920 MiB` → total = 1 vCPU / 2048 MiB |
| `AccessDenied: ecr:GetAuthorizationToken` | ECR blocked by org IAM policy | Use `public.ecr.aws/amazoncorretto/amazoncorretto:17` + S3 JAR download |
| `s3:GetBucketLocation AccessDenied` | Job role missing permission (Firelens init) | Add `s3:GetBucketLocation` to Job Role IAM policy |
| `Could not parse arn: s3://...` | Firelens init requires ARN format | Change `s3://bucket/key` → `arn:aws:s3:::bucket/key` |
| CF `EarlyValidation::ResourceExistenceCheck` | Log group exists in another stack | Remove log group resource from template — CloudWatch auto-creates |
| Metrics show 0 or missing | Job exits before metric step boundary | Call `meterRegistry.close()` + sleep 10s before process exit |
| Logs not reaching Loki | `OpenTelemetryAppender.install()` not called before Spring logging starts | Move `install()` to a `static {}` block (runs before Spring context) |

---

## 10. Environment Variable Reference

| Variable | Required | Example Value | Description |
|---------|---------|--------------|-------------|
| `OTEL_SERVICE_NAME` | **Yes** | `sre-batch-telemetry-java` | Service name shown in all Grafana backends |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | **Yes** | `https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318` | OpenAPM OTLP endpoint (port 4318 only) |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | **Yes** | `http/protobuf` | Must be HTTP, not gRPC |
| `OTEL_RESOURCE_ATTRIBUTES` | **Yes** | `environment=mon-sandbox,account.id=723346695882,openapm_product_name=sre-batch-telemetry,service_name=sre-batch-telemetry-java,region=us-west-2` | Labels applied to all signals |
| `ENVIRONMENT` | No | `mon-sandbox` | Read by Java `application.yml` |
| `OPENAPM_PRODUCT_NAME` | No | `sre-batch-telemetry` | Read by Java `application.yml` |
| `AWS_REGION` | No | `us-west-2` | Read by Java `application.yml` |
| `AWS_ACCOUNT_ID` | No | `723346695882` | Read by Java `application.yml` |

---

*Related documents:*
- [01-AWS-BATCH-OPENAPM-GUIDELINES.md](01-AWS-BATCH-OPENAPM-GUIDELINES.md) — Standards
- [03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md](03-AWS-BATCH-OPENAPM-ARCHITECTURE-ONE-PAGER.md) — Architecture
- [04-POC-PYTHON-SINGLE-CONTAINER.md](04-POC-PYTHON-SINGLE-CONTAINER.md) — Python POC
- [05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md](05-POC-JAVA-DUAL-CONTAINER-FIRELENS.md) — Java Firelens POC

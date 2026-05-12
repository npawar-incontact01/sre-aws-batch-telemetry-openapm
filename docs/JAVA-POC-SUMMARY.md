# Java AWS Batch Telemetry POC — What We Built and How

## Objective

Prove that a **Java Spring Boot AWS Batch job running on Fargate** can send all three OpenTelemetry signals — traces, metrics, and logs — to **OpenAPM** (Grafana Tempo / Mimir / Loki) with correct `service_name` labels, without ECR push. Two approaches were implemented and both confirmed working.

---

## What We Achieved

### Approach A — Direct OTLP (`poc/java-aws-batch`)

| Signal | Library | Destination | Label in Grafana | Status |
|--------|---------|-------------|-----------------|--------|
| Traces | Micrometer Tracing (OTel bridge) | Tempo | `service.name=sre-batch-telemetry-java` | ✅ Working |
| Metrics | Micrometer OTLP Registry | Mimir | `service_name=sre-batch-telemetry-java` | ✅ Working |
| Logs | OTel Logback Appender + custom `SdkLoggerProvider` | Loki | `service_name=sre-batch-telemetry-java` | ✅ Working |

### Approach B — Firelens (`poc/java-firelens-brian-approach`) — Brian's Recommended Approach

| Signal | Library | Destination | Label in Grafana | Status |
|--------|---------|-------------|-----------------|--------|
| Traces | Micrometer Tracing (OTel bridge) | Tempo | `service.name=sre-batch-telemetry-java` | ✅ Working |
| Metrics | Micrometer OTLP Registry | Mimir | `service_name=sre-batch-telemetry-java` | ✅ Working |
| Logs | Firelens sidecar (`aws-for-fluent-bit:init-3.2.4`) | Loki | `service_name=sre-batch-telemetry-java` | ✅ Working |

Both approaches confirmed with correct `service_name` labels. See [FIRELENS-EVIDENCE.md](FIRELENS-EVIDENCE.md) for the Firelens journey and resolution.

Additional log fields captured automatically:
- `code_filepath`, `code_function`, `code_lineno`, `code_namespace` (from `captureCodeAttributes=true`)
- `traceId`, `spanId` (from Micrometer MDC injection)
- `telemetry_sdk_language=java`, `telemetry_sdk_name=opentelemetry`, `telemetry_sdk_version=1.39.0`

---

## Architecture

```
AWS Batch Fargate Task (1 vCPU / 2048 MiB)
└── Java container (amazoncorretto:17)
    │
    ├─ Entrypoint: download batch-telemetry.jar from S3, then: java -jar /app.jar
    │
    ├─ Spring Boot 3.3.13 (CommandLineRunner — no web server)
    │     ├─ Micrometer Tracing Bridge (OTel)
    │     │     → BatchSpanProcessor → OTLPSpanExporter → /v1/traces → Tempo
    │     │
    │     ├─ Micrometer OTLP Registry (push every 10s)
    │     │     → OTLPMetricExporter → /v1/metrics → Mimir
    │     │
    │     └─ Logback OpenTelemetryAppender
    │           → custom SdkLoggerProvider (built from OTEL_* env vars)
    │           → OtlpHttpLogRecordExporter → /v1/logs → Loki
    │
    └─ stdout → CloudWatch Logs (/aws/batch/sre-batch-telemetry-java/app)

All OTLP/HTTP calls → VPC Endpoint (PrivateLink) → OpenAPM :4318
```

---

## How We Built It

### Step 1 — Set Up Spring Boot Project

Created `java-poc/` with:
- **Spring Boot 3.3.13** parent
- **Java 17**, Maven build → fat JAR (`batch-telemetry.jar`)
- `spring.main.web-application-type=none` — batch job exits when `CommandLineRunner.run()` completes

### Step 2 — Traces via Micrometer Tracing (OTel Bridge)

Added to `pom.xml`:
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

Configured in `application.yml`:
```yaml
management:
  tracing:
    sampling:
      probability: 1.0
  otlp:
    tracing:
      endpoint: ${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces
```

Spring Boot auto-configures a `Tracer` bean. Injected into `BatchTelemetryApplication` via constructor.

Each job phase (fetch / process / write) is wrapped in a child span:
```java
Span span = tracer.nextSpan().name("fetch-data").start();
try (Tracer.SpanInScope ws = tracer.withSpan(span)) {
    // ... job logic ...
} finally {
    span.end();
}
```

Micrometer automatically injects `traceId` and `spanId` into MDC — so every `log.info()` call includes them.

### Step 3 — Metrics via Micrometer OTLP Registry

Added to `pom.xml`:
```xml
<dependency>
    <groupId>io.micrometer</groupId>
    <artifactId>micrometer-registry-otlp</artifactId>
</dependency>
```

Configured in `application.yml`:
```yaml
management:
  otlp:
    metrics:
      export:
        url: ${OTEL_EXPORTER_OTLP_ENDPOINT}/v1/metrics
        step: 10s
        resource-attributes:
          service.name: ${OTEL_SERVICE_NAME:sre-batch-telemetry-java}
          service_name: ${OTEL_SERVICE_NAME:sre-batch-telemetry-java}
          environment: ${ENVIRONMENT:mon-sandbox}
          openapm_product_name: ${OPENAPM_PRODUCT_NAME:sre-batch-telemetry}
          region: ${AWS_REGION:us-west-2}
          account.id: ${AWS_ACCOUNT_ID:723346695882}
```

Used `Timer`, `Counter`, and `AtomicInteger` gauge to record job duration, items processed, and job status (0 = success, 1 = error).

**Key insight:** Used `AtomicInteger` for the gauge to avoid GC collecting the value (Micrometer stores a `WeakReference` by default).

### Step 4 — Logs via OTel Logback Appender

#### 4a. Added `opentelemetry-logback-appender-1.0` to `pom.xml`

```xml
<dependency>
    <groupId>io.opentelemetry.instrumentation</groupId>
    <artifactId>opentelemetry-logback-appender-1.0</artifactId>
    <version>2.4.0-alpha</version>
</dependency>
```

#### 4b. Configured `logback-spring.xml`

```xml
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
```

#### 4c. Wired the appender to a real SDK (the hard part)

**Problem 1 — Spring-managed bean has no `SdkLoggerProvider`:**  
Spring Boot 3.3 auto-configures an `OpenTelemetry` bean for Micrometer Tracing but it contains only a `SdkTracerProvider` — no `SdkLoggerProvider`. Calling `OpenTelemetryAppender.install(springBean)` silently dropped all logs.  
*Spring Boot 3.4 adds `SdkLoggerProvider` auto-configuration — not available in 3.3.*

**Problem 2 — `GlobalOpenTelemetry` is a no-op:**  
Without an explicit `install()` call, the appender falls back to `GlobalOpenTelemetry.get()` which is a no-op SDK → logs reached Loki but with `service_name=unknown_service`.

**Solution:** Build a dedicated `OpenTelemetrySdk` with `SdkLoggerProvider` from `OTEL_*` env vars:

```java
private static SdkLoggerProvider buildLoggerProvider() {
    String endpoint = System.getenv().getOrDefault(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318");
    String serviceName = System.getenv().getOrDefault(
            "OTEL_SERVICE_NAME", "sre-batch-telemetry-java");

    // Parse OTEL_RESOURCE_ATTRIBUTES (e.g. "environment=dev,region=us-west-2")
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

    OtlpHttpLogRecordExporter logExporter = OtlpHttpLogRecordExporter.builder()
            .setEndpoint(endpoint + "/v1/logs")
            .build();

    return SdkLoggerProvider.builder()
            .setResource(Resource.getDefault().merge(Resource.create(attrs.build())))
            .addLogRecordProcessor(BatchLogRecordProcessor.builder(logExporter).build())
            .build();
}
```

Then in the constructor:
```java
this.loggerProvider = buildLoggerProvider();
OpenTelemetryAppender.install(
        OpenTelemetrySdk.builder().setLoggerProvider(this.loggerProvider).build());
```

### Step 5 — Deployed CloudFormation (Single Container)

Decided against Firelens (see below) and deployed `batch-job-definition-java.yaml`:
- **Single container** (no sidecar) using `amazoncorretto:17`
- `awslogs` log driver → CloudWatch `/aws/batch/sre-batch-telemetry-java/app`
- `1 vCPU / 2048 MiB` — required minimum for Fargate single-container task
- JAR fetched from S3 at startup: `aws s3 cp s3://.../java/batch-telemetry.jar /app.jar`

### Step 6 — Flush Strategy Before Exit

Batch job is short-lived. Without explicit flush, the `BatchLogRecordProcessor` may not have sent all log records before the process exits.

Solution: 15-second sleep after `loggerProvider.forceFlush()` to allow the async `BatchSpanProcessor` to also complete its final export:

```java
private void flushTelemetry() {
    // Close MeterRegistry → triggers final metric push
    if (meterRegistry instanceof AutoCloseable c) c.close();
    // Force flush buffered log records
    loggerProvider.forceFlush();
}

// Then in run():
flushTelemetry();
sleep(15_000);  // Allow BatchSpanProcessor to complete
```

---

## Problems Encountered & Solutions

| Problem | Root Cause | Solution |
|---|---|---|
| Firelens `opentelemetry` plugin handles logs | Plugin v1.9.x is metrics-only | Switched to OTel logback appender |
| Firelens with S3 custom config blocked | Fargate `FirelensConfiguration` doesn't support S3 config | Not applicable after switching approach |
| `service_name=unknown_service` in Loki (attempt 1) | `GlobalOpenTelemetry` is no-op; Firelens `add_label` can't set OTLP resource attrs | Switched to OTel logback appender |
| `service_name=unknown_service` in Loki (attempt 2) | Spring Boot 3.3's `OpenTelemetry` bean has no `SdkLoggerProvider` | Built dedicated `SdkLoggerProvider` from env vars |
| Logs silently dropped | `install(springBean)` gave appender an SDK with no logger provider | Verified with `OtlpHttpLogRecordExporter` builder pattern |
| Fargate `ClientException` on deploy | `0.75 vCPU / 1920 MiB` is invalid for single-container task | Changed to `1 vCPU / 2048 MiB` |
| CVE-2024-7254 | `protobuf-java:3.23.4` bundled with OTel BOM 1.39.0 | Pinned `protobuf-java:3.25.5` in `<dependencyManagement>` |

---

## Why Not Firelens?

We evaluated Firelens extensively before switching approaches:

| Firelens Issue | Detail |
|---|---|
| Fluent Bit 1.9.x `opentelemetry` plugin | Handles metrics only — no log support |
| S3 custom config | Blocked by Fargate (only ECS can mount external config) |
| `add_label` in ECS options | Adds Loki stream labels but cannot set OTLP resource attributes — `service_name` stays `unknown_service` |
| Fluent Bit 3.3.0 upgrade | Logs started working but `service_name` problem remained |

**Conclusion:** Firelens cannot set `service.name` OTLP resource attributes from inline ECS config. The OTel logback appender sends logs directly from the app with correct resource attributes — simpler and more reliable.

---

## Files Added / Modified in This POC

| File | Purpose |
|---|---|
| `java-poc/pom.xml` | Spring Boot 3.3.13 + Micrometer + OTel deps |
| `java-poc/src/main/java/.../BatchTelemetryApplication.java` | Main job with traces, metrics, log wiring |
| `java-poc/src/main/resources/application.yml` | Micrometer tracing + metrics config |
| `java-poc/src/main/resources/logback-spring.xml` | CONSOLE + OpenTelemetryAppender |
| `cloudformation/batch-job-definition-java.yaml` | Job definition (1 vCPU / 2048 MiB, single container) |
| `scripts/build-and-deploy-java.sh` | Build + S3 upload helper |
| `docs/JAVA-POC-SUMMARY.md` | This document |

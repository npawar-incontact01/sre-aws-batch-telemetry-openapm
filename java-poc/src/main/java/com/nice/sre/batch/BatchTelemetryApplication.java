package com.nice.sre.batch;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.micrometer.tracing.Span;
import io.micrometer.tracing.Tracer;
import io.opentelemetry.api.common.AttributeKey;
import io.opentelemetry.api.common.Attributes;
import io.opentelemetry.api.common.AttributesBuilder;
import io.opentelemetry.exporter.otlp.http.logs.OtlpHttpLogRecordExporter;
import io.opentelemetry.instrumentation.logback.appender.v1_0.OpenTelemetryAppender;
import io.opentelemetry.sdk.OpenTelemetrySdk;
import io.opentelemetry.sdk.logs.SdkLoggerProvider;
import io.opentelemetry.sdk.logs.export.BatchLogRecordProcessor;
import io.opentelemetry.sdk.resources.Resource;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

import java.time.Duration;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Spring Boot AWS Batch job demonstrating all 3 telemetry signals
 * via Micrometer + OTel bridge:
 *   - Traces  → Micrometer Tracing → OTel → OTLP/HTTP → Tempo
 *   - Metrics → Micrometer OTLP Registry → OTLP/HTTP → Mimir
 *   - Logs    → Logback → OTel Appender → OTLP/HTTP → Loki
 */
@SpringBootApplication
public class BatchTelemetryApplication implements CommandLineRunner {

    private static final Logger log = LoggerFactory.getLogger(BatchTelemetryApplication.class);

    private final MeterRegistry meterRegistry;
    private final Tracer tracer;
    private final SdkLoggerProvider loggerProvider;

    public BatchTelemetryApplication(MeterRegistry meterRegistry, Tracer tracer) {
        this.meterRegistry = meterRegistry;
        this.tracer = tracer;
        // Spring Boot 3.3's auto-configured OpenTelemetry bean has no SdkLoggerProvider
        // (added in Spring Boot 3.4). Build a dedicated SDK for log export from OTEL_*
        // env vars so every log record carries the correct service.name + resource attributes.
        this.loggerProvider = buildLoggerProvider();
        OpenTelemetryAppender.install(
                OpenTelemetrySdk.builder().setLoggerProvider(this.loggerProvider).build());
    }

    /**
     * Build an OTel SdkLoggerProvider from OTEL_* env vars.
     * Reads OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_SERVICE_NAME, and OTEL_RESOURCE_ATTRIBUTES
     * (comma-separated key=value pairs) — same env vars set in the Batch job definition.
     */
    private static SdkLoggerProvider buildLoggerProvider() {
        String endpoint = System.getenv().getOrDefault(
                "OTEL_EXPORTER_OTLP_ENDPOINT",
                "https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318");
        String serviceName = System.getenv().getOrDefault(
                "OTEL_SERVICE_NAME", "sre-batch-telemetry-java");

        AttributesBuilder attrs = Attributes.builder()
                .put(AttributeKey.stringKey("service.name"), serviceName);

        // Overlay OTEL_RESOURCE_ATTRIBUTES (e.g. "environment=dev,region=us-west-2")
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

    public static void main(String[] args) {
        SpringApplication.run(BatchTelemetryApplication.class, args);
    }

    @Override
    public void run(String... args) {
        log.info("=== Spring Boot Batch Telemetry POC ===");
        log.info("Tracer: {}", tracer.getClass().getSimpleName());
        log.info("MeterRegistry: {}", meterRegistry.getClass().getSimpleName());

        // Create metrics instruments
        Timer jobTimer = Timer.builder("job.duration")
                .description("Total batch job duration")
                .tag("job.type", "telemetry-poc")
                .register(meterRegistry);
        Counter itemsCounter = Counter.builder("job.items.processed")
                .description("Total items processed")
                .tag("job.type", "telemetry-poc")
                .register(meterRegistry);

        // Gauge: use AtomicInteger so the registry holds a strong reference (avoids GC/NaN)
        AtomicInteger jobStatus = new AtomicInteger(0);
        meterRegistry.gauge("job.status", jobStatus);

        // Root span
        Span rootSpan = tracer.nextSpan().name("batch-job-execution").start();
        try (Tracer.SpanInScope ws = tracer.withSpan(rootSpan)) {
            long startNanos = System.nanoTime();

            // Step 1: Fetch data
            int count = fetchData();

            // Step 2: Process data
            processData(count);

            // Step 3: Write results
            writeResults(count);

            // Record metrics
            long elapsed = System.nanoTime() - startNanos;
            jobTimer.record(Duration.ofNanos(elapsed));
            itemsCounter.increment(count);

            rootSpan.tag("job.items_processed", String.valueOf(count));
            rootSpan.tag("job.status", "success");
            // jobStatus stays 0 (success)

            log.info("Batch job completed. Items processed: {}, Duration: {}s",
                    count, String.format("%.2f", elapsed / 1_000_000_000.0));

        } catch (Exception e) {
            rootSpan.tag("job.status", "error");
            rootSpan.error(e);
            jobStatus.set(1); // 1 = error
            log.error("Batch job failed", e);
            throw new RuntimeException(e);
        } finally {
            rootSpan.end();
        }

        // Flush telemetry before exit
        flushTelemetry();

        // Wait for Firelens (Fluent Bit) to flush buffered logs via OTLP to Loki.
        // Fluent Bit's default flush interval is 5s; 15s gives 3 flush cycles before SIGTERM.
        log.info("Waiting 15s for Firelens to flush logs...");
        sleep(15000);
        log.info("Firelens flush wait complete. Exiting.");
    }

    private int fetchData() {
        Span span = tracer.nextSpan().name("fetch-data").start();
        try (Tracer.SpanInScope ws = tracer.withSpan(span)) {
            log.info("Fetching data...");
            sleep(100);
            int count = ThreadLocalRandom.current().nextInt(50, 200);
            span.tag("data.count", String.valueOf(count));
            log.info("Fetched {} records", count);
            return count;
        } finally {
            span.end();
        }
    }

    private void processData(int count) {
        Span span = tracer.nextSpan().name("process-data").start();
        try (Tracer.SpanInScope ws = tracer.withSpan(span)) {
            log.info("Processing {} records...", count);
            sleep(200);
            span.tag("data.processed", String.valueOf(count));
            log.info("Processed {} records", count);
        } finally {
            span.end();
        }
    }

    private void writeResults(int count) {
        Span span = tracer.nextSpan().name("write-results").start();
        try (Tracer.SpanInScope ws = tracer.withSpan(span)) {
            log.info("Writing {} results...", count);
            sleep(100);
            span.tag("data.written", String.valueOf(count));
            log.info("Wrote {} results", count);
        } finally {
            span.end();
        }
    }

    private void flushTelemetry() {
        log.info("Flushing telemetry before exit...");
        try {
            // Force a metrics push before shutdown
            if (meterRegistry instanceof AutoCloseable closeable) {
                closeable.close();
                log.info("MeterRegistry flushed");
            }
        } catch (Exception e) {
            log.warn("Error flushing MeterRegistry: {}", e.getMessage());
        }

        // Force flush of any buffered log records before the 15s sleep window
        loggerProvider.forceFlush();
        log.info("Telemetry flush complete");
    }

    private static void sleep(long millis) {
        try {
            Thread.sleep(millis);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}

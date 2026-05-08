package com.nice.sre.batch;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.micrometer.tracing.Span;
import io.micrometer.tracing.Tracer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.CommandLineRunner;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

import java.time.Duration;
import java.util.concurrent.ThreadLocalRandom;

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

    public BatchTelemetryApplication(MeterRegistry meterRegistry, Tracer tracer) {
        this.meterRegistry = meterRegistry;
        this.tracer = tracer;
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

            meterRegistry.gauge("job.status", 0); // 0 = success

            log.info("Batch job completed. Items processed: {}, Duration: {:.2f}s",
                    count, elapsed / 1_000_000_000.0);

        } catch (Exception e) {
            rootSpan.tag("job.status", "error");
            rootSpan.error(e);
            meterRegistry.gauge("job.status", 1); // 1 = error
            log.error("Batch job failed", e);
            throw new RuntimeException(e);
        } finally {
            rootSpan.end();
        }

        // Flush telemetry before exit
        flushTelemetry();
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

        // LoggerProvider shutdown is handled by OtelLogConfig @PreDestroy
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

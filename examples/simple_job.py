"""
Example: Minimal AWS Batch job with OpenTelemetry instrumentation.

This shows how a consumer integrates with batch_otel in ~10 lines of code.
All OTel configuration comes from environment variables set in the job definition.
"""

from batch_otel import init_telemetry, shutdown_telemetry


def main():
    # 1. Initialise telemetry (reads config from env vars)
    otel = init_telemetry()
    tracer = otel.tracer
    meter = otel.meter
    logger = otel.logger

    # 2. Create metrics
    duration_hist = meter.create_histogram("job.duration", unit="s")
    items_counter = meter.create_counter("job.items_processed", unit="1")

    # 3. Run your job inside a span
    import time
    start = time.time()

    with tracer.start_as_current_span("my-batch-job") as span:
        logger.info("Starting my batch job")

        # --- Your actual job logic here ---
        items_processed = do_work(tracer, logger)
        # ---

        span.set_attribute("job.items_processed", items_processed)
        items_counter.add(items_processed)

    elapsed = time.time() - start
    duration_hist.record(elapsed)
    logger.info("Job completed in %.2fs, processed %d items", elapsed, items_processed)

    # 4. Shut down (flushes all telemetry before exit)
    shutdown_telemetry(otel)


def do_work(tracer, logger):
    """Replace this with your actual batch job logic."""
    import time, random

    with tracer.start_as_current_span("fetch-data") as span:
        count = random.randint(50, 200)
        time.sleep(0.1)
        span.set_attribute("data.count", count)
        logger.info("Fetched %d records", count)

    with tracer.start_as_current_span("process-data") as span:
        time.sleep(0.2)
        span.set_attribute("data.processed", count)
        logger.info("Processed %d records", count)

    return count


if __name__ == "__main__":
    main()

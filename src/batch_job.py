"""
AWS Batch job with OpenTelemetry instrumentation.

Sends traces and metrics to an OpenAPM endpoint via OTLP/gRPC.
All OTel configuration is read from environment variables (12-factor style).
"""

import logging
import os
import random
import sys
import time

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("batch_job")

# ---------------------------------------------------------------------------
# OTel configuration (read from environment variables)
# ---------------------------------------------------------------------------

OTEL_ENDPOINT = os.environ.get(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "https://apm-na1.service.nicecxone-dev.com:4317",
)
SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "sre-aws-batch-telemetry")
RESOURCE_ATTRS_RAW = os.environ.get(
    "OTEL_RESOURCE_ATTRIBUTES", "environment=ic-dev,account.id=300813158921"
)


def _parse_resource_attributes(raw: str) -> dict:
    """Parse 'key=value,key2=value2' string into a dict."""
    attrs = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            key, _, value = pair.partition("=")
            attrs[key.strip()] = value.strip()
    return attrs


def setup_tracing(resource: Resource) -> TracerProvider:
    """Initialise and register a global TracerProvider with OTLP exporter."""
    exporter = OTLPSpanExporter(endpoint=OTEL_ENDPOINT)
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    logger.info("TracerProvider initialised, exporting to %s", OTEL_ENDPOINT)
    return provider


def setup_metrics(resource: Resource) -> MeterProvider:
    """Initialise and register a global MeterProvider with OTLP exporter."""
    exporter = OTLPMetricExporter(endpoint=OTEL_ENDPOINT)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=5_000)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    logger.info("MeterProvider initialised, exporting to %s", OTEL_ENDPOINT)
    return provider


# ---------------------------------------------------------------------------
# Simulated batch work
# ---------------------------------------------------------------------------

def fetch_data(tracer) -> int:
    """Simulate fetching data from a source."""
    with tracer.start_as_current_span("fetch-data") as span:
        item_count = random.randint(50, 200)
        duration = random.uniform(0.1, 0.5)
        time.sleep(duration)
        span.set_attribute("data.item_count", item_count)
        span.set_attribute("data.source", "s3")
        logger.info("Fetched %d items in %.2fs", item_count, duration)
        return item_count


def process_data(tracer, item_count: int) -> int:
    """Simulate processing items."""
    with tracer.start_as_current_span("process-data") as span:
        processed = item_count
        duration = random.uniform(0.2, 1.0)
        time.sleep(duration)
        span.set_attribute("data.items_processed", processed)
        span.set_attribute("data.processing_engine", "batch_v1")
        logger.info("Processed %d items in %.2fs", processed, duration)
        return processed


def write_results(tracer, processed_count: int) -> None:
    """Simulate writing results to a destination."""
    with tracer.start_as_current_span("write-results") as span:
        duration = random.uniform(0.05, 0.3)
        time.sleep(duration)
        span.set_attribute("data.items_written", processed_count)
        span.set_attribute("data.destination", "s3")
        logger.info("Wrote %d results in %.2fs", processed_count, duration)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_batch_job(tracer, meter) -> int:
    """
    Execute the full batch job workflow inside a parent span.

    Returns the number of items processed (>= 0 on success).
    Raises on unrecoverable error.
    """
    job_duration_histogram = meter.create_histogram(
        name="job.duration",
        description="Total batch job duration in seconds",
        unit="s",
    )
    items_processed_counter = meter.create_counter(
        name="job.items_processed",
        description="Total number of items processed by the batch job",
        unit="1",
    )
    job_status_gauge = meter.create_gauge(
        name="job.status",
        description="Batch job final status: 0=success, 1=error",
        unit="1",
    )

    start_time = time.time()
    status = 1  # default to error; updated on success

    with tracer.start_as_current_span("batch-job-execution") as parent_span:
        parent_span.set_attribute("job.name", SERVICE_NAME)
        parent_span.set_attribute("job.environment", os.environ.get("OTEL_RESOURCE_ATTRIBUTES", ""))

        try:
            item_count = fetch_data(tracer)
            processed = process_data(tracer, item_count)
            write_results(tracer, processed)

            parent_span.set_attribute("job.items_processed", processed)
            parent_span.set_attribute("job.success", True)

            items_processed_counter.add(processed)
            status = 0
            logger.info("Batch job completed successfully. Items processed: %d", processed)
            return processed

        except Exception as exc:
            parent_span.set_attribute("job.success", False)
            parent_span.record_exception(exc)
            logger.exception("Batch job failed: %s", exc)
            raise

        finally:
            elapsed = time.time() - start_time
            job_duration_histogram.record(elapsed)
            job_status_gauge.set(status)
            logger.info("Job duration: %.2fs, status: %d", elapsed, status)


def main() -> None:
    """Entry point for the AWS Batch job."""
    logger.info("Starting batch job — service: %s", SERVICE_NAME)

    # Build OTel Resource
    resource_attrs = {
        "service.name": SERVICE_NAME,
        **_parse_resource_attributes(RESOURCE_ATTRS_RAW),
    }
    resource = Resource.create(resource_attrs)

    tracer_provider = setup_tracing(resource)
    meter_provider = setup_metrics(resource)

    tracer = trace.get_tracer(SERVICE_NAME)
    meter = metrics.get_meter(SERVICE_NAME)

    try:
        run_batch_job(tracer, meter)
        logger.info("Batch job finished — flushing telemetry…")
    except Exception:
        logger.error("Batch job exited with error — flushing telemetry…")
        sys.exit(1)
    finally:
        # Flush and shutdown both providers so no telemetry is lost
        tracer_provider.shutdown()
        meter_provider.shutdown()
        logger.info("OTel providers shut down cleanly.")


if __name__ == "__main__":
    main()

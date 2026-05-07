"""
batch_otel - Generic OpenTelemetry instrumentation for AWS Batch jobs.

Exports all 3 signals to OpenAPM via OTLP/HTTP (same endpoint, port 4318):
  - Traces  → /v1/traces  → Tempo
  - Metrics → /v1/metrics → Mimir
  - Logs    → /v1/logs    → Loki

Usage:
    from batch_otel import init_telemetry, shutdown_telemetry

    otel = init_telemetry()
    tracer = otel.tracer
    meter = otel.meter
    logger = otel.logger  # logs automatically exported to Loki

    with tracer.start_as_current_span("my-job"):
        logger.info("Processing complete")

    shutdown_telemetry(otel)

All configuration is via environment variables (12-factor):
    OTEL_SERVICE_NAME           - Service name (required for Grafana discovery)
    OTEL_EXPORTER_OTLP_ENDPOINT - OpenAPM endpoint (e.g. https://apm-na1...:4318)
    OTEL_RESOURCE_ATTRIBUTES    - Comma-separated key=value pairs
"""

from batch_otel.instrumentation import (
    OTelContext,
    init_telemetry,
    shutdown_telemetry,
)

__all__ = ["init_telemetry", "shutdown_telemetry", "OTelContext"]
__version__ = "1.0.0"

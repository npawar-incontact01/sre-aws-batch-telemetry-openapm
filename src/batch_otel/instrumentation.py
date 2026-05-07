"""
Core OTel instrumentation for AWS Batch jobs.

Initialises TracerProvider, MeterProvider, and LoggerProvider with OTLP/HTTP exporters.
All 3 signals go to the same OpenAPM endpoint on port 4318:
  - /v1/traces  → Tempo
  - /v1/metrics → Mimir
  - /v1/logs    → Loki

All configuration is read from environment variables.
"""

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Optional: log exporter (requires opentelemetry-exporter-otlp-proto-http >= 1.24)
try:
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    _LOGS_AVAILABLE = True
except ImportError:
    _LOGS_AVAILABLE = False


_logger = logging.getLogger("batch_otel")


# ---------------------------------------------------------------------------
# Environment variable configuration
# ---------------------------------------------------------------------------

# Required env vars (with sensible defaults for mon-sandbox)
_ENV_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
_ENV_SERVICE_NAME = "OTEL_SERVICE_NAME"
_ENV_RESOURCE_ATTRS = "OTEL_RESOURCE_ATTRIBUTES"

# Default endpoint for mon-sandbox OpenAPM
_DEFAULT_ENDPOINT = "https://apm-na1.mon-sandbox.nicecxone-sbx.com:4318"


@dataclass
class OTelContext:
    """Holds references to all OTel providers for clean shutdown."""

    tracer: trace.Tracer
    meter: metrics.Meter
    logger: logging.Logger
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: Optional[object] = field(default=None)
    service_name: str = ""
    endpoint: str = ""


def _parse_resource_attributes(raw: str) -> dict:
    """Parse 'key=value,key2=value2' string into a dict."""
    attrs = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            key, _, value = pair.partition("=")
            attrs[key.strip()] = value.strip()
    return attrs


def init_telemetry(
    service_name: Optional[str] = None,
    endpoint: Optional[str] = None,
    resource_attributes: Optional[dict] = None,
    enable_logs: bool = True,
    metric_export_interval_ms: int = 10_000,
) -> OTelContext:
    """
    Initialise OpenTelemetry with traces, metrics, and logs.

    All 3 signals are exported via OTLP/HTTP to the same endpoint:
      - /v1/traces  → Tempo
      - /v1/metrics → Mimir
      - /v1/logs    → Loki

    All parameters can be overridden via environment variables:
        OTEL_SERVICE_NAME            -> service_name
        OTEL_EXPORTER_OTLP_ENDPOINT  -> endpoint
        OTEL_RESOURCE_ATTRIBUTES     -> resource_attributes (merged)

    Args:
        service_name: Override service name (env var takes precedence).
        endpoint: Override OTLP endpoint (env var takes precedence).
        resource_attributes: Additional resource attributes (merged with env var).
        enable_logs: Whether to export logs to OpenAPM (requires log exporter).
        metric_export_interval_ms: How often to push metrics (default 10s).

    Returns:
        OTelContext with tracer, meter, logger, and providers.
    """
    # Resolve configuration: env vars take precedence
    resolved_endpoint = os.environ.get(_ENV_ENDPOINT, endpoint or _DEFAULT_ENDPOINT)
    resolved_service = os.environ.get(_ENV_SERVICE_NAME, service_name or "unknown-batch-job")
    resolved_attrs_raw = os.environ.get(_ENV_RESOURCE_ATTRS, "")

    # Merge resource attributes
    merged_attrs = {"service.name": resolved_service}
    if resolved_attrs_raw:
        merged_attrs.update(_parse_resource_attributes(resolved_attrs_raw))
    if resource_attributes:
        merged_attrs.update(resource_attributes)

    resource = Resource.create(merged_attrs)

    _logger.info(
        "Initialising OTel: service=%s, endpoint=%s, attrs=%s",
        resolved_service,
        resolved_endpoint,
        merged_attrs,
    )

    # --- Traces ---
    trace_exporter = OTLPSpanExporter(endpoint=resolved_endpoint + "/v1/traces")
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
    trace.set_tracer_provider(tracer_provider)

    # --- Metrics ---
    metric_exporter = OTLPMetricExporter(endpoint=resolved_endpoint + "/v1/metrics")
    reader = PeriodicExportingMetricReader(
        metric_exporter, export_interval_millis=metric_export_interval_ms
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    # --- Logs (optional) ---
    logger_provider = None
    if enable_logs and _LOGS_AVAILABLE:
        log_exporter = OTLPLogExporter(endpoint=resolved_endpoint + "/v1/logs")
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        set_logger_provider(logger_provider)

        # Attach OTel handler to Python root logger so all logs are exported
        handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
        logging.getLogger().addHandler(handler)
        _logger.info("Log exporter enabled — logs will be sent to %s/v1/logs", resolved_endpoint)
    elif enable_logs and not _LOGS_AVAILABLE:
        _logger.warning(
            "Log export requested but opentelemetry log exporter not installed. "
            "Install opentelemetry-exporter-otlp-proto-http >= 1.24.0"
        )

    # Create user-facing tracer and meter
    tracer = trace.get_tracer(resolved_service)
    meter = metrics.get_meter(resolved_service)

    # Create a logger for the consumer
    app_logger = logging.getLogger(resolved_service)
    if not app_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)

    return OTelContext(
        tracer=tracer,
        meter=meter,
        logger=app_logger,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        service_name=resolved_service,
        endpoint=resolved_endpoint,
    )


def shutdown_telemetry(ctx: OTelContext) -> None:
    """Flush and shut down all OTel providers. Call this before exit."""
    _logger.info("Shutting down OTel providers…")
    try:
        ctx.tracer_provider.shutdown()
    except Exception as e:
        _logger.warning("Error shutting down TracerProvider: %s", e)
    try:
        ctx.meter_provider.shutdown()
    except Exception as e:
        _logger.warning("Error shutting down MeterProvider: %s", e)
    if ctx.logger_provider and hasattr(ctx.logger_provider, "shutdown"):
        try:
            ctx.logger_provider.shutdown()
        except Exception as e:
            _logger.warning("Error shutting down LoggerProvider: %s", e)
    _logger.info("OTel providers shut down cleanly.")

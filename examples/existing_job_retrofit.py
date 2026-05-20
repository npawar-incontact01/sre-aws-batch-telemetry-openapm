"""
Example: Existing batch job retrofitted with OTel — shows decorator pattern.

For jobs where you don't want to restructure existing code,
use the context manager approach.
"""

import time
import random
from batch_otel import init_telemetry, shutdown_telemetry


def existing_etl_logic():
    """This is your existing job code — unchanged."""
    time.sleep(random.uniform(0.5, 1.5))
    records = random.randint(100, 5000)
    return records


def main():
    otel = init_telemetry()

    # Wrap your existing logic with a span
    with otel.tracer.start_as_current_span("etl-pipeline") as span:
        try:
            records = existing_etl_logic()
            span.set_attribute("etl.records_processed", records)
            span.set_attribute("etl.status", "success")
            otel.logger.info("ETL completed: %d records", records)
        except Exception as e:
            span.set_attribute("etl.status", "error")
            span.record_exception(e)
            otel.logger.error("ETL failed: %s", e)
            shutdown_telemetry(otel)
            raise

    shutdown_telemetry(otel)


if __name__ == "__main__":
    main()

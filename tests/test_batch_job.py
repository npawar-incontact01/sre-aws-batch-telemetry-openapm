"""
Unit tests for src/batch_job.py

Tests cover:
- Resource attribute parsing
- OTel provider initialisation (mocked exporters)
- Batch work simulation functions
- Full job run (success and error paths)
- Graceful shutdown of providers
"""

import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Ensure src/ is importable
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import batch_job  # noqa: E402  (import after path manipulation)


class TestParseResourceAttributes(unittest.TestCase):
    def test_parses_key_value_pairs(self):
        result = batch_job._parse_resource_attributes("environment=ic-dev,account.id=300813158921")
        self.assertEqual(result, {"environment": "ic-dev", "account.id": "300813158921"})

    def test_single_pair(self):
        result = batch_job._parse_resource_attributes("key=value")
        self.assertEqual(result, {"key": "value"})

    def test_empty_string(self):
        result = batch_job._parse_resource_attributes("")
        self.assertEqual(result, {})

    def test_ignores_pairs_without_equals(self):
        result = batch_job._parse_resource_attributes("noequals,key=val")
        self.assertEqual(result, {"key": "val"})

    def test_value_with_colon(self):
        result = batch_job._parse_resource_attributes("endpoint=https://host:4317")
        self.assertEqual(result, {"endpoint": "https://host:4317"})

    def test_strips_whitespace(self):
        result = batch_job._parse_resource_attributes(" env = prod , region = us-east-1 ")
        self.assertEqual(result, {"env": "prod", "region": "us-east-1"})


class TestSetupTracing(unittest.TestCase):
    @patch("batch_job.OTLPSpanExporter")
    @patch("batch_job.BatchSpanProcessor")
    @patch("batch_job.TracerProvider")
    @patch("batch_job.trace")
    def test_returns_tracer_provider(self, mock_trace, mock_tp_cls, mock_bsp_cls, mock_exp_cls):
        mock_resource = MagicMock()
        mock_provider = MagicMock()
        mock_tp_cls.return_value = mock_provider

        result = batch_job.setup_tracing(mock_resource)

        mock_tp_cls.assert_called_once_with(resource=mock_resource)
        mock_trace.set_tracer_provider.assert_called_once_with(mock_provider)
        self.assertEqual(result, mock_provider)

    @patch("batch_job.OTLPSpanExporter")
    @patch("batch_job.BatchSpanProcessor")
    @patch("batch_job.TracerProvider")
    @patch("batch_job.trace")
    def test_exporter_uses_configured_endpoint(
        self, mock_trace, mock_tp_cls, mock_bsp_cls, mock_exp_cls
    ):
        mock_resource = MagicMock()
        with patch.object(batch_job, "OTEL_ENDPOINT", "https://custom-endpoint:4317"):
            batch_job.setup_tracing(mock_resource)
        mock_exp_cls.assert_called_once_with(endpoint="https://custom-endpoint:4317")


class TestSetupMetrics(unittest.TestCase):
    @patch("batch_job.OTLPMetricExporter")
    @patch("batch_job.PeriodicExportingMetricReader")
    @patch("batch_job.MeterProvider")
    @patch("batch_job.metrics")
    def test_returns_meter_provider(
        self, mock_metrics, mock_mp_cls, mock_reader_cls, mock_exp_cls
    ):
        mock_resource = MagicMock()
        mock_provider = MagicMock()
        mock_mp_cls.return_value = mock_provider

        result = batch_job.setup_metrics(mock_resource)

        mock_mp_cls.assert_called_once()
        mock_metrics.set_meter_provider.assert_called_once_with(mock_provider)
        self.assertEqual(result, mock_provider)


class TestFetchData(unittest.TestCase):
    def _make_tracer(self):
        """Return a tracer mock that supports context-manager spans."""
        span = MagicMock()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=span)
        cm.__exit__ = MagicMock(return_value=False)
        tracer = MagicMock()
        tracer.start_as_current_span.return_value = cm
        return tracer, span

    @patch("batch_job.time.sleep")
    @patch("batch_job.random.randint", return_value=100)
    @patch("batch_job.random.uniform", return_value=0.1)
    def test_returns_item_count(self, mock_uniform, mock_randint, mock_sleep):
        tracer, _ = self._make_tracer()
        result = batch_job.fetch_data(tracer)
        self.assertEqual(result, 100)

    @patch("batch_job.time.sleep")
    @patch("batch_job.random.randint", return_value=75)
    @patch("batch_job.random.uniform", return_value=0.2)
    def test_sets_span_attributes(self, mock_uniform, mock_randint, mock_sleep):
        tracer, span = self._make_tracer()
        batch_job.fetch_data(tracer)
        span.set_attribute.assert_any_call("data.item_count", 75)
        span.set_attribute.assert_any_call("data.source", "s3")


class TestProcessData(unittest.TestCase):
    def _make_tracer(self):
        span = MagicMock()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=span)
        cm.__exit__ = MagicMock(return_value=False)
        tracer = MagicMock()
        tracer.start_as_current_span.return_value = cm
        return tracer, span

    @patch("batch_job.time.sleep")
    @patch("batch_job.random.uniform", return_value=0.1)
    def test_returns_processed_count(self, mock_uniform, mock_sleep):
        tracer, _ = self._make_tracer()
        result = batch_job.process_data(tracer, 50)
        self.assertEqual(result, 50)

    @patch("batch_job.time.sleep")
    @patch("batch_job.random.uniform", return_value=0.1)
    def test_sets_span_attributes(self, mock_uniform, mock_sleep):
        tracer, span = self._make_tracer()
        batch_job.process_data(tracer, 42)
        span.set_attribute.assert_any_call("data.items_processed", 42)


class TestWriteResults(unittest.TestCase):
    def _make_tracer(self):
        span = MagicMock()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=span)
        cm.__exit__ = MagicMock(return_value=False)
        tracer = MagicMock()
        tracer.start_as_current_span.return_value = cm
        return tracer, span

    @patch("batch_job.time.sleep")
    @patch("batch_job.random.uniform", return_value=0.05)
    def test_sets_span_attributes(self, mock_uniform, mock_sleep):
        tracer, span = self._make_tracer()
        batch_job.write_results(tracer, 99)
        span.set_attribute.assert_any_call("data.items_written", 99)
        span.set_attribute.assert_any_call("data.destination", "s3")


class TestRunBatchJob(unittest.TestCase):
    def _make_tracer_with_span(self):
        span = MagicMock()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=span)
        cm.__exit__ = MagicMock(return_value=False)
        tracer = MagicMock()
        tracer.start_as_current_span.return_value = cm
        return tracer, span

    def _make_meter(self):
        meter = MagicMock()
        histogram = MagicMock()
        counter = MagicMock()
        gauge = MagicMock()
        meter.create_histogram.return_value = histogram
        meter.create_counter.return_value = counter
        meter.create_gauge.return_value = gauge
        return meter, histogram, counter, gauge

    @patch("batch_job.write_results")
    @patch("batch_job.process_data", return_value=100)
    @patch("batch_job.fetch_data", return_value=100)
    def test_success_returns_processed_count(
        self, mock_fetch, mock_process, mock_write
    ):
        tracer, _ = self._make_tracer_with_span()
        meter, histogram, counter, gauge = self._make_meter()
        result = batch_job.run_batch_job(tracer, meter)
        self.assertEqual(result, 100)

    @patch("batch_job.write_results")
    @patch("batch_job.process_data", return_value=100)
    @patch("batch_job.fetch_data", return_value=100)
    def test_success_records_metrics(
        self, mock_fetch, mock_process, mock_write
    ):
        tracer, _ = self._make_tracer_with_span()
        meter, histogram, counter, gauge = self._make_meter()
        batch_job.run_batch_job(tracer, meter)
        counter.add.assert_called_once_with(100)
        histogram.record.assert_called_once()
        gauge.set.assert_called_once_with(0)  # success=0

    @patch("batch_job.fetch_data", side_effect=RuntimeError("fetch failed"))
    def test_failure_raises_and_sets_error_status(self, mock_fetch):
        tracer, span = self._make_tracer_with_span()
        meter, histogram, counter, gauge = self._make_meter()
        with self.assertRaises(RuntimeError):
            batch_job.run_batch_job(tracer, meter)
        span.record_exception.assert_called_once()
        gauge.set.assert_called_once_with(1)  # error=1
        histogram.record.assert_called_once()


class TestMain(unittest.TestCase):
    @patch("batch_job.run_batch_job", return_value=50)
    @patch("batch_job.setup_metrics")
    @patch("batch_job.setup_tracing")
    @patch("batch_job.Resource")
    @patch("batch_job.metrics")
    @patch("batch_job.trace")
    def test_main_success_shuts_down_providers(
        self,
        mock_trace,
        mock_metrics,
        mock_resource_cls,
        mock_setup_tracing,
        mock_setup_metrics,
        mock_run,
    ):
        mock_tp = MagicMock()
        mock_mp = MagicMock()
        mock_setup_tracing.return_value = mock_tp
        mock_setup_metrics.return_value = mock_mp
        mock_resource_cls.create.return_value = MagicMock()
        mock_trace.get_tracer.return_value = MagicMock()
        mock_metrics.get_meter.return_value = MagicMock()

        batch_job.main()

        mock_tp.shutdown.assert_called_once()
        mock_mp.shutdown.assert_called_once()

    @patch("batch_job.run_batch_job", side_effect=RuntimeError("boom"))
    @patch("batch_job.setup_metrics")
    @patch("batch_job.setup_tracing")
    @patch("batch_job.Resource")
    @patch("batch_job.metrics")
    @patch("batch_job.trace")
    def test_main_error_shuts_down_providers_and_exits(
        self,
        mock_trace,
        mock_metrics,
        mock_resource_cls,
        mock_setup_tracing,
        mock_setup_metrics,
        mock_run,
    ):
        mock_tp = MagicMock()
        mock_mp = MagicMock()
        mock_setup_tracing.return_value = mock_tp
        mock_setup_metrics.return_value = mock_mp
        mock_resource_cls.create.return_value = MagicMock()
        mock_trace.get_tracer.return_value = MagicMock()
        mock_metrics.get_meter.return_value = MagicMock()

        with self.assertRaises(SystemExit) as ctx:
            batch_job.main()
        self.assertEqual(ctx.exception.code, 1)
        mock_tp.shutdown.assert_called_once()
        mock_mp.shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()

"""Shared OpenTelemetry wiring for both services (traces, metrics, logs → OTLP).

Each service calls setup_telemetry() with its own service name. W3C trace
context propagation (the default OTel propagator) links the chat service's
outbound call to the gateway's server span, so a chat turn and every gateway
routing decision live in a single distributed trace.
"""
from __future__ import annotations

import logging

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import (
    Counter,
    Histogram,
    MeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    UpDownCounter,
)
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality,
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)

# Dynatrace's OTLP metric ingest requires DELTA temporality. The OTel SDK
# defaults to CUMULATIVE for counters, which Dynatrace silently drops — so the
# custom gen_ai/gateway metrics never appear in Grail. Force delta for all
# instrument kinds.
_DELTA = {
    Counter: AggregationTemporality.DELTA,
    UpDownCounter: AggregationTemporality.DELTA,
    Histogram: AggregationTemporality.DELTA,
    ObservableCounter: AggregationTemporality.DELTA,
    ObservableUpDownCounter: AggregationTemporality.DELTA,
    ObservableGauge: AggregationTemporality.DELTA,
}
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from . import config

log = logging.getLogger("telemetry")


def _parse_headers(raw: str) -> dict:
    headers = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            headers[k.strip()] = v.strip()
    return headers


def setup_telemetry(app, service_name: str, instrument_anthropic: bool = False) -> None:
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "2.0.0",
            "deployment.environment": "demo",
        }
    )
    headers = _parse_headers(config.OTEL_HEADERS) if config.OTEL_HEADERS else {}
    to_dt = bool(config.OTEL_ENDPOINT)

    # ---- Traces ----
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = (
        OTLPSpanExporter(endpoint=f"{config.OTEL_ENDPOINT}/v1/traces", headers=headers)
        if to_dt else ConsoleSpanExporter()
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # ---- Metrics ----
    metric_exporter = (
        OTLPMetricExporter(endpoint=f"{config.OTEL_ENDPOINT}/v1/metrics", headers=headers,
                           preferred_temporality=_DELTA)
        if to_dt else ConsoleMetricExporter(preferred_temporality=_DELTA)
    )
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    # ---- Logs (trace-correlated) ----
    logger_provider = LoggerProvider(resource=resource)
    log_exporter = (
        OTLPLogExporter(endpoint=f"{config.OTEL_ENDPOINT}/v1/logs", headers=headers)
        if to_dt else ConsoleLogExporter()
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)
    logging.getLogger().addHandler(
        LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    )

    # ---- Auto-instrumentation ----
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)

    if instrument_anthropic:
        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

        # Content capture left ON deliberately (PROBLEMS.md #9).
        AnthropicInstrumentor().instrument()

    log.warning(
        "Telemetry for '%s' -> %s", service_name,
        config.OTEL_ENDPOINT or "console",
    )

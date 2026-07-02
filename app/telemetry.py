"""OpenTelemetry + OpenLLMetry wiring.

Sends traces, metrics AND logs to Dynatrace (or any OTLP endpoint). The Anthropic
instrumentor produces gen_ai.* spans and the token-usage / operation-duration
metrics that the Dynatrace **AI Observability** app consumes out of the box.
Logs are exported through the OTel logging pipeline with the active trace
context attached (trace_id / span_id), so Dynatrace stitches each log line to
the span it was emitted under.
"""
import logging

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    ConsoleLogExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

from . import config

log = logging.getLogger("telemetry")


def _parse_headers(raw: str) -> dict:
    """Parse 'Key=Value,Key2=Value2' into a dict (Dynatrace uses Authorization)."""
    headers = {}
    for pair in raw.split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            headers[k.strip()] = v.strip()
    return headers


def setup_telemetry(app=None) -> None:
    resource = Resource.create(
        {
            "service.name": config.SERVICE_NAME,
            "service.version": "1.0.0",
            "deployment.environment": "demo",
        }
    )

    headers = _parse_headers(config.OTEL_HEADERS) if config.OTEL_HEADERS else {}

    # ---- Traces ----
    tracer_provider = TracerProvider(resource=resource)
    if config.OTEL_ENDPOINT:
        span_exporter = OTLPSpanExporter(
            endpoint=f"{config.OTEL_ENDPOINT}/v1/traces", headers=headers
        )
        log.info("Exporting spans to %s", config.OTEL_ENDPOINT)
    else:
        span_exporter = ConsoleSpanExporter()
        log.warning("OTEL_EXPORTER_OTLP_ENDPOINT not set — spans go to console")
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # ---- Metrics (gen_ai token usage, cost, latency) ----
    if config.OTEL_ENDPOINT:
        metric_exporter = OTLPMetricExporter(
            endpoint=f"{config.OTEL_ENDPOINT}/v1/metrics", headers=headers
        )
    else:
        metric_exporter = ConsoleMetricExporter()
    reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    # ---- Logs (correlated to traces via trace_id / span_id) ----
    logger_provider = LoggerProvider(resource=resource)
    if config.OTEL_ENDPOINT:
        log_exporter = OTLPLogExporter(
            endpoint=f"{config.OTEL_ENDPOINT}/v1/logs", headers=headers
        )
    else:
        log_exporter = ConsoleLogExporter()
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(logger_provider)

    # Route Python's stdlib logging through OTel. The SDK LoggingHandler stamps
    # each record with the currently-active span's trace_id/span_id, which is
    # what lets Dynatrace stitch logs onto the trace.
    otel_handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
    logging.getLogger().addHandler(otel_handler)

    # ---- Auto-instrumentation ----
    from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor

    # NOTE: capturing full prompt+completion content is left ON deliberately —
    # see PROBLEMS.md anti-pattern #9 (sensitive data / PII capture).
    AnthropicInstrumentor().instrument()

    if app is not None:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=tracer_provider)

    log.info("Telemetry initialised for service '%s'", config.SERVICE_NAME)

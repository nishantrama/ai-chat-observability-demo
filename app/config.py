"""Central configuration, loaded from environment / .env."""
import os

from dotenv import load_dotenv

load_dotenv()


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-4-8")
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "claude-haiku-4-5-20251001")

# --- Mock mode ---
# When enabled, the Anthropic SDK is pointed at a local mock server that speaks
# the real Messages API, so the app runs with NO API key and NO network calls
# while still emitting genuine gen_ai.* spans + token/cost/latency metrics.
# Defaults ON whenever no real ANTHROPIC_API_KEY is provided.
USE_MOCK = _bool("MOCK_ANTHROPIC", default=not ANTHROPIC_API_KEY)
MOCK_HOST = os.getenv("MOCK_HOST", "127.0.0.1")
MOCK_PORT = int(os.getenv("MOCK_PORT", "8080"))

# --- OpenTelemetry ---
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
OTEL_HEADERS = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "ai-chat-observability-demo")

# --- Chaos toggles ---
CHAOS_LATENCY = _float("CHAOS_LATENCY", 0.25)
CHAOS_ERROR = _float("CHAOS_ERROR", 0.15)
CHAOS_RATE_LIMIT = _float("CHAOS_RATE_LIMIT", 0.10)

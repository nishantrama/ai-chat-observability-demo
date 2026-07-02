"""Central configuration, loaded from environment / .env."""
import os

from dotenv import load_dotenv

load_dotenv()


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-4-8")
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "claude-haiku-4-5-20251001")

# --- OpenTelemetry ---
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
OTEL_HEADERS = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "ai-chat-observability-demo")

# --- Chaos toggles ---
CHAOS_LATENCY = _float("CHAOS_LATENCY", 0.25)
CHAOS_ERROR = _float("CHAOS_ERROR", 0.15)
CHAOS_RATE_LIMIT = _float("CHAOS_RATE_LIMIT", 0.10)

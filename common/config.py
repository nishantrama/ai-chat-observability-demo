"""Shared configuration for the chat service and the AI gateway."""
from __future__ import annotations

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
# The chat app *requests* this model; the gateway may override it (smart routing).
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-4-8")
MID_MODEL = os.getenv("MID_MODEL", "claude-sonnet-5")
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "claude-haiku-4-5-20251001")

# USD per 1M tokens (input, output). Source: Claude API pricing.
PRICING = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}

# --- Mock upstream (owned by the gateway) ---
USE_MOCK = _bool("MOCK_ANTHROPIC", default=not ANTHROPIC_API_KEY)
MOCK_HOST = os.getenv("MOCK_HOST", "127.0.0.1")
MOCK_PORT = int(os.getenv("MOCK_PORT", "8080"))

# --- AI gateway ---
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8090"))
GATEWAY_URL = os.getenv("GATEWAY_URL", f"http://{GATEWAY_HOST}:{GATEWAY_PORT}")
# "smart" = route by prompt; "passthrough" = always honor the requested model.
ROUTER_MODE = os.getenv("ROUTER_MODE", "smart")

# --- OpenTelemetry ---
OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
OTEL_HEADERS = os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
CHAT_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "ai-chat-observability-demo")
GATEWAY_SERVICE_NAME = os.getenv("GATEWAY_SERVICE_NAME", "ai-gateway")

# --- Chaos toggles (applied at the gateway's upstream LLM call) ---
CHAOS_LATENCY = _float("CHAOS_LATENCY", 0.25)
CHAOS_ERROR = _float("CHAOS_ERROR", 0.15)
CHAOS_RATE_LIMIT = _float("CHAOS_RATE_LIMIT", 0.10)

# --- Gateway-layer problems (each is a real anti-pattern of AI gateways) ---
# G1: a "response cache" whose key includes a per-request nonce -> never hits.
GATEWAY_CACHE_ENABLED = _bool("GATEWAY_CACHE_ENABLED", True)
# G2: retry the upstream call this many times with NO backoff (retry storm).
GATEWAY_MAX_RETRIES = int(os.getenv("GATEWAY_MAX_RETRIES", "3"))
# G3: after retries are exhausted, silently serve the cheapest model instead.
GATEWAY_FALLBACK_ENABLED = _bool("GATEWAY_FALLBACK_ENABLED", True)
# G4: probability the (flaky) classifier routes to the wrong model tier.
GATEWAY_MISROUTE_RATE = _float("GATEWAY_MISROUTE_RATE", 0.15)
# G5: base latency (ms) the routing/classification step adds before any LLM call.
GATEWAY_ROUTE_LATENCY_MS = int(os.getenv("GATEWAY_ROUTE_LATENCY_MS", "60"))
# G6: whether safety flags actually BLOCK. False = detect-but-don't-enforce (the problem).
GATEWAY_ENFORCE_SAFETY = _bool("GATEWAY_ENFORCE_SAFETY", False)

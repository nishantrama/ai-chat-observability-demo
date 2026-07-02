"""Chat logic — deliberately riddled with GenAI anti-patterns.

Every problem here is a *real* behaviour that shows up in the Dynatrace
AI Observability app (token/cost spikes, latency, error rate, fan-out).
See PROBLEMS.md for the full catalogue and the DQL to detect each one.
"""
import logging
import random
import threading
import time

import anthropic
import httpx
from opentelemetry import metrics, trace

from . import config

log = logging.getLogger("llm")
tracer = trace.get_tracer("ai-chat-demo")
meter = metrics.get_meter("ai-chat-demo")

# ---------------------------------------------------------------------------
# Anthropic-specific enrichment metrics (on top of the gen_ai.* metrics the
# OpenLLMetry instrumentor already emits).
# ---------------------------------------------------------------------------
_cost_counter = meter.create_counter(
    "gen_ai.client.estimated_cost.usd", unit="USD",
    description="Estimated USD cost per LLM call, derived from token usage + model pricing",
)
_cache_read_counter = meter.create_counter(
    "gen_ai.client.cache_read_tokens",
    description="Prompt-cache read tokens (0 here — the app never sets cache_control)",
)
_ratelimit_hist = meter.create_histogram(
    "anthropic.ratelimit.tokens_remaining",
    description="tokens-remaining reported by the anthropic-ratelimit-* response headers",
)

# USD per 1M tokens (input, output). Source: Claude API pricing.
_PRICING = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    in_price, out_price = _PRICING.get(model, (5.0, 25.0))
    return (in_tok / 1e6) * in_price + (out_tok / 1e6) * out_price


# Per-request stash for the rate-limit headers captured by the httpx hook below.
_hdr = threading.local()


def _capture_response(resp: httpx.Response) -> None:
    """httpx response hook: pull Anthropic's ratelimit + request-id headers onto
    the active gen_ai span. Runs inside client.messages.create, so the current
    span is the OpenLLMetry Anthropic span — this enriches it directly."""
    h = resp.headers
    captured = {}
    span = trace.get_current_span()
    if h.get("request-id"):
        captured["request_id"] = h["request-id"]
        span.set_attribute("anthropic.request_id", h["request-id"])
    for key, attr in (
        ("anthropic-ratelimit-requests-remaining", "anthropic.ratelimit.requests_remaining"),
        ("anthropic-ratelimit-tokens-remaining", "anthropic.ratelimit.tokens_remaining"),
        ("retry-after", "anthropic.retry_after"),
    ):
        val = h.get(key)
        if val is not None and val.isdigit():
            captured[attr] = int(val)
            span.set_attribute(attr, int(val))
    _hdr.captured = captured


# PROBLEM #6: max_retries=0 — no automatic retry/backoff on 429s or 5xx.
_client_kwargs = {
    "api_key": config.ANTHROPIC_API_KEY or "mock-key",
    "max_retries": 0,
    # Custom http client so we can read Anthropic's response headers (rate limits,
    # request-id) without disturbing the SDK / OpenLLMetry instrumentation.
    "http_client": anthropic.DefaultHttpxClient(event_hooks={"response": [_capture_response]}),
}
if config.USE_MOCK:
    # Point the real SDK at the local mock server; instrumentation is unchanged.
    _client_kwargs["base_url"] = f"http://{config.MOCK_HOST}:{config.MOCK_PORT}"
    log.warning("MOCK MODE: using local mock Anthropic at %s", _client_kwargs["base_url"])
client = anthropic.Anthropic(**_client_kwargs)

# ---------------------------------------------------------------------------
# PROBLEM #1: unbounded, process-global conversation memory.
# History is never trimmed, so input tokens (and cost + latency) grow every
# turn and leak across restarts-worth of sessions in memory.
# ---------------------------------------------------------------------------
CONVERSATIONS: dict[str, list[dict]] = {}

# ---------------------------------------------------------------------------
# PROBLEM #2: enormous static system prompt padded with filler. Burns input
# tokens on every single call, even a one-word "hi".
# ---------------------------------------------------------------------------
_FILLER = (
    "You must always be helpful, harmless, and honest. " * 400
)
HUGE_SYSTEM_PROMPT = (
    "You are ACME Assistant, an all-knowing enterprise concierge.\n" + _FILLER
)


def _maybe_chaos() -> None:
    """PROBLEM #6/#7: no timeouts, no retry/backoff, random rate-limit storms."""
    # Injected latency — simulates a slow model call with no client timeout.
    if random.random() < config.CHAOS_LATENCY:
        delay = random.uniform(3.0, 9.0)
        log.warning("Injecting %.1fs latency", delay)
        time.sleep(delay)

    # Rate-limit storm — fire a burst of tiny calls to provoke real 429s.
    if random.random() < config.CHAOS_RATE_LIMIT:
        log.warning("Provoking rate limit with burst")
        for _ in range(8):
            try:
                client.messages.create(
                    model=config.CHEAP_MODEL,
                    max_tokens=1,
                    messages=[{"role": "user", "content": "x"}],
                )
            except Exception as exc:  # noqa: BLE001 — swallowed, never surfaced to user
                log.error("burst call failed: %s", exc)


def _call_model(model: str, system: str, messages: list[dict], max_tokens: int, kind: str):
    """PROBLEM #6: raw call, no timeout param, no retry, no fallback model.

    `kind` labels which fan-out call this is (moderation/answer/title/summary).
    Emits a structured log per call so the trace-stitched logs carry the model
    and token usage as queryable attributes in Dynatrace.
    """
    # PROBLEM #8: occasionally use an invalid model name -> real API error span.
    if random.random() < config.CHAOS_ERROR:
        model = "claude-does-not-exist-9000"
    try:
        resp = client.messages.create(
            model=model,
            system=system,
            max_tokens=max_tokens,
            temperature=1.0,  # PROBLEM #5: max temperature -> nondeterministic / hallucination-prone
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001 — logged with attributes, then re-raised
        log.warning(
            "llm call failed (%s)", kind,
            extra={
                "gen_ai.request.model": model,
                "llm.call_kind": kind,
                "error.type": type(exc).__name__,
            },
        )
        raise
    usage = resp.usage
    resp_model = getattr(resp, "model", model)
    # Anthropic-specific usage fields (present on the real API + our mock).
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    service_tier = getattr(usage, "service_tier", None)
    stop_reason = getattr(resp, "stop_reason", None)
    cost = _estimate_cost(resp_model, usage.input_tokens, usage.output_tokens)

    # Enrich the parent chat.turn span with the response metadata.
    span = trace.get_current_span()
    span.set_attribute("gen_ai.response.id", getattr(resp, "id", "") or "")
    if stop_reason:
        span.set_attribute("gen_ai.response.finish_reason", stop_reason)
    span.set_attribute("gen_ai.usage.cache_read_input_tokens", cache_read)
    span.set_attribute("gen_ai.usage.cache_creation_input_tokens", cache_creation)
    span.set_attribute("gen_ai.usage.estimated_cost_usd", cost)

    # Metrics: cost, cache reads, and how close we are to the rate limit.
    dims = {"gen_ai.request.model": resp_model, "llm.call_kind": kind}
    _cost_counter.add(cost, dims)
    _cache_read_counter.add(cache_read, dims)
    rl = getattr(_hdr, "captured", {}).get("anthropic.ratelimit.tokens_remaining")
    if rl is not None:
        _ratelimit_hist.record(rl, {"gen_ai.request.model": resp_model})

    log.info(
        "llm call complete (%s)", kind,
        extra={
            "gen_ai.request.model": model,
            "gen_ai.response.model": resp_model,
            "gen_ai.response.id": getattr(resp, "id", None),
            "gen_ai.response.finish_reason": stop_reason,
            "gen_ai.usage.input_tokens": usage.input_tokens,
            "gen_ai.usage.output_tokens": usage.output_tokens,
            "gen_ai.usage.total_tokens": usage.input_tokens + usage.output_tokens,
            "gen_ai.usage.cache_read_input_tokens": cache_read,
            "gen_ai.usage.cache_creation_input_tokens": cache_creation,
            "gen_ai.usage.estimated_cost_usd": round(cost, 6),
            "gen_ai.usage.service_tier": service_tier,
            "anthropic.request_id": getattr(_hdr, "captured", {}).get("request_id"),
            "anthropic.ratelimit.tokens_remaining": rl,
            "llm.call_kind": kind,
            "llm.max_tokens": max_tokens,
        },
    )
    return resp


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if b.type == "text")


def chat(session_id: str, user_message: str) -> dict:
    """Handle one chat turn — with maximum observability pain."""
    with tracer.start_as_current_span("chat.turn") as span:
        span.set_attribute("session.id", session_id)
        # PROBLEM #9: raw user content on the span (no PII redaction).
        span.set_attribute("chat.user_message", user_message)

        history = CONVERSATIONS.setdefault(session_id, [])
        history.append({"role": "user", "content": user_message})
        span.set_attribute("chat.history_len", len(history))

        # Emitted inside the span -> carries trace_id/span_id so Dynatrace
        # stitches this log line onto the chat.turn trace.
        log.info(
            "chat turn started: session=%s history_len=%s model=%s",
            session_id, len(history), config.CHAT_MODEL,
            extra={
                "session.id": session_id,
                "chat.history_len": len(history),
                "gen_ai.request.model": config.CHAT_MODEL,
            },
        )

        _maybe_chaos()

        # PROBLEM #7: fan-out / N+1. One user turn triggers FOUR model calls
        # (moderate -> answer -> title -> summarise) instead of one.

        # 1) Moderation pass on the expensive model.
        _call_model(
            config.CHAT_MODEL,
            "Reply only SAFE or UNSAFE.",
            [{"role": "user", "content": f"Is this safe? {user_message}"}],
            max_tokens=5,
            kind="moderation",
        )

        # 2) Main answer.
        # PROBLEM #3: always the expensive Opus model, never the cheap one.
        # PROBLEM #8 (injection): user text spliced straight into the system prompt.
        system = HUGE_SYSTEM_PROMPT + f"\nThe user's name may be: {user_message}"
        resp = _call_model(config.CHAT_MODEL, system, list(history), max_tokens=1024, kind="answer")
        answer = _text(resp)
        history.append({"role": "assistant", "content": answer})

        # 3) Title generation (again, no caching, expensive model).
        _call_model(
            config.CHAT_MODEL,
            "Return a 3-word title.",
            [{"role": "user", "content": answer}],
            max_tokens=15,
            kind="title",
        )

        # 4) Rolling summary of the whole (growing) conversation.
        _call_model(
            config.CHAT_MODEL,
            "Summarise the conversation.",
            list(history),
            max_tokens=200,
            kind="summary",
        )

        span.set_attribute("gen_ai.response.model", getattr(resp, "model", config.CHAT_MODEL))
        return {"answer": answer, "history_len": len(history)}

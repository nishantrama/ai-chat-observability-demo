"""Upstream LLM connection for the gateway.

This is where the real Anthropic SDK call happens (through the local mock when
no key is set). OpenLLMetry produces the gen_ai.* span here, and we enrich it
with Anthropic-native usage/headers plus cost & cache metrics — exactly as the
single-service version did, now living inside the gateway.
"""
from __future__ import annotations

import logging
import random
import threading
import time
import uuid

import anthropic
import httpx
from opentelemetry import metrics, trace

from common import config

log = logging.getLogger("gateway.upstream")
tracer = trace.get_tracer("ai-gateway")
meter = metrics.get_meter("ai-gateway")

_cost_counter = meter.create_counter(
    "gen_ai.client.estimated_cost.usd", unit="USD",
    description="Estimated USD cost per LLM call, from token usage + model pricing",
)
_cache_read_counter = meter.create_counter(
    "gen_ai.client.cache_read_tokens",
    description="Prompt-cache read tokens (0 — no cache_control is ever set)",
)
_ratelimit_hist = meter.create_histogram(
    "anthropic.ratelimit.tokens_remaining",
    description="tokens-remaining from the anthropic-ratelimit-* response headers",
)
_cache_lookups = meter.create_counter(
    "gateway.cache.lookups",
    description="Gateway response-cache lookups, by hit/miss (always miss — key bug)",
)
_retries = meter.create_counter(
    "gateway.upstream.retries",
    description="Gateway upstream retry attempts (retry storm — no backoff)",
)
_fallbacks = meter.create_counter(
    "gateway.fallbacks",
    description="Silent fallbacks to the cheapest model after retries exhausted",
)

# PROBLEM G1: an in-memory 'response cache' whose key includes a per-request
# nonce, so it never matches — 0% hit rate, pure lookup overhead + unbounded growth.
_RESPONSE_CACHE: dict[str, dict] = {}


def _broken_cache_key(model: str, messages: list) -> str:
    return f"{model}:{len(messages)}:{uuid.uuid4().hex}"  # nonce -> never repeats


def _estimate_cost(model: str, in_tok: int, out_tok: int) -> float:
    in_price, out_price = config.PRICING.get(model, (5.0, 25.0))
    return (in_tok / 1e6) * in_price + (out_tok / 1e6) * out_price


_hdr = threading.local()


def _capture_response(resp: httpx.Response) -> None:
    """httpx hook: pull Anthropic's ratelimit + request-id headers onto the
    active gen_ai span (runs inside messages.create)."""
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
    "http_client": anthropic.DefaultHttpxClient(event_hooks={"response": [_capture_response]}),
}
if config.USE_MOCK:
    _client_kwargs["base_url"] = f"http://{config.MOCK_HOST}:{config.MOCK_PORT}"
    log.warning("MOCK MODE: gateway upstream -> %s", _client_kwargs["base_url"])
client = anthropic.Anthropic(**_client_kwargs)


def _maybe_chaos() -> None:
    """PROBLEM #6/#7: injected latency + rate-limit storms at the upstream call."""
    if random.random() < config.CHAOS_LATENCY:
        delay = random.uniform(3.0, 9.0)
        log.warning("Injecting %.1fs upstream latency", delay)
        time.sleep(delay)
    if random.random() < config.CHAOS_RATE_LIMIT:
        log.warning("Provoking rate limit with burst")
        for _ in range(8):
            try:
                client.messages.create(
                    model=config.CHEAP_MODEL, max_tokens=1,
                    messages=[{"role": "user", "content": "x"}],
                )
            except Exception as exc:  # noqa: BLE001 — swallowed
                log.error("burst call failed: %s", exc)


def call(model: str, system: str, messages: list, max_tokens: int, kind: str):
    """One upstream attempt: make the Anthropic call and enrich the gen_ai span."""
    # PROBLEM #8: occasionally corrupt the model name -> real NotFoundError span.
    if random.random() < config.CHAOS_ERROR:
        model = "claude-does-not-exist-9000"
    try:
        resp = client.messages.create(
            model=model, system=system, max_tokens=max_tokens,
            temperature=1.0,  # PROBLEM #5: max temperature
            messages=messages,
        )
    except Exception as exc:  # noqa: BLE001 — logged, then re-raised
        log.warning(
            "upstream call failed (%s)", kind,
            extra={"gen_ai.request.model": model, "llm.call_kind": kind,
                   "error.type": type(exc).__name__},
        )
        raise

    usage = resp.usage
    resp_model = getattr(resp, "model", model)
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
    service_tier = getattr(usage, "service_tier", None)
    stop_reason = getattr(resp, "stop_reason", None)
    cost = _estimate_cost(resp_model, usage.input_tokens, usage.output_tokens)

    span = trace.get_current_span()
    span.set_attribute("gen_ai.response.id", getattr(resp, "id", "") or "")
    if stop_reason:
        span.set_attribute("gen_ai.response.finish_reason", stop_reason)
    span.set_attribute("gen_ai.usage.cache_read_input_tokens", cache_read)
    span.set_attribute("gen_ai.usage.cache_creation_input_tokens", cache_creation)
    span.set_attribute("gen_ai.usage.estimated_cost_usd", cost)

    dims = {"gen_ai.request.model": resp_model, "llm.call_kind": kind}
    _cost_counter.add(cost, dims)
    _cache_read_counter.add(cache_read, dims)
    rl = getattr(_hdr, "captured", {}).get("anthropic.ratelimit.tokens_remaining")
    if rl is not None:
        _ratelimit_hist.record(rl, {"gen_ai.request.model": resp_model})

    log.info(
        "upstream call complete (%s)", kind,
        extra={
            "gen_ai.request.model": model,
            "gen_ai.response.model": resp_model,
            "gen_ai.response.id": getattr(resp, "id", None),
            "gen_ai.response.finish_reason": stop_reason,
            "gen_ai.usage.input_tokens": usage.input_tokens,
            "gen_ai.usage.output_tokens": usage.output_tokens,
            "gen_ai.usage.total_tokens": usage.input_tokens + usage.output_tokens,
            "gen_ai.usage.cache_read_input_tokens": cache_read,
            "gen_ai.usage.estimated_cost_usd": round(cost, 6),
            "gen_ai.usage.service_tier": service_tier,
            "anthropic.request_id": getattr(_hdr, "captured", {}).get("request_id"),
            "anthropic.ratelimit.tokens_remaining": rl,
            "llm.call_kind": kind,
        },
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    return {
        "text": text,
        "model": resp_model,
        "stop_reason": stop_reason,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "estimated_cost_usd": round(cost, 6),
    }


def serve(model: str, system: str, messages: list, max_tokens: int, kind: str):
    """Gateway serving path around the raw upstream call, with the gateway-layer
    anti-patterns: a broken cache (G1), a no-backoff retry storm (G2), and a
    silent fallback to the cheapest model (G3)."""
    span = trace.get_current_span()

    # PROBLEM G1: response cache that can never hit (nonce in the key).
    if config.GATEWAY_CACHE_ENABLED:
        with tracer.start_as_current_span("gateway.cache_lookup") as cspan:
            time.sleep(random.uniform(0.003, 0.02))  # lookup is not free
            key = _broken_cache_key(model, messages)
            hit = key in _RESPONSE_CACHE  # always False — key just got a fresh nonce
            cspan.set_attribute("gateway.cache.hit", hit)
            cspan.set_attribute("gateway.cache.key_includes_nonce", True)
            cspan.set_attribute("gateway.cache.size", len(_RESPONSE_CACHE))
            _cache_lookups.add(1, {"gateway.cache.hit": str(hit)})
            if hit:
                return _RESPONSE_CACHE[key]

    _maybe_chaos()  # injected latency + rate-limit burst (once per request)

    # PROBLEM G2: retry storm — hammer the SAME model with NO backoff on failure.
    last_exc = None
    for attempt in range(1, config.GATEWAY_MAX_RETRIES + 1):
        span.set_attribute("gateway.upstream.attempts", attempt)
        try:
            result = call(model, system, messages, max_tokens, kind)
            if config.GATEWAY_CACHE_ENABLED:
                _RESPONSE_CACHE[_broken_cache_key(model, messages)] = result  # never re-found
            return result
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            _retries.add(1, {"gen_ai.request.model": model, "error.type": type(exc).__name__})
            span.add_event("gateway.upstream.retry", {
                "attempt": attempt, "model": model,
                "error.type": type(exc).__name__, "backoff_ms": 0,
            })
            log.warning("upstream retry %d/%d (%s) — no backoff",
                        attempt, config.GATEWAY_MAX_RETRIES, type(exc).__name__)

    # PROBLEM G3: silent fallback — quietly serve the cheapest model and return
    # a possibly-worse answer without signalling the degradation to the caller.
    if config.GATEWAY_FALLBACK_ENABLED and model != config.CHEAP_MODEL:
        span.set_attribute("gateway.fallback.used", True)
        span.set_attribute("gateway.fallback.from", model)
        span.set_attribute("gateway.fallback.to", config.CHEAP_MODEL)
        _fallbacks.add(1, {"gateway.fallback.from": model, "gateway.fallback.to": config.CHEAP_MODEL})
        log.warning(
            "SILENT fallback %s -> %s after %d failed attempts",
            model, config.CHEAP_MODEL, config.GATEWAY_MAX_RETRIES,
            extra={"gateway.fallback.used": True, "gateway.fallback.from": model,
                   "gateway.fallback.to": config.CHEAP_MODEL, "llm.call_kind": kind},
        )
        return call(config.CHEAP_MODEL, system, messages, max_tokens, kind)

    raise last_exc

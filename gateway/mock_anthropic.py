"""A tiny local stand-in for the Anthropic Messages API.

Lets the demo run with **no API key and no network access** while the real
Anthropic SDK + OpenLLMetry instrumentation stay completely unchanged — so
Dynatrace still receives genuine gen_ai.* spans with token usage, model,
latency, and errors. Token counts are derived from the actual request size,
so the oversized-prompt / unbounded-history problems produce real numbers.

Reproduces the response surface the real Anthropic API exposes, so the app can
capture rich Anthropic-specific telemetry:
  * 404 not_found_error for unknown models        (problem #8 error spans)
  * 429 rate_limit_error + Retry-After under load  (problem #6 / rate limits)
  * usage.{input,output,cache_read,cache_creation}_tokens + service_tier
  * stop_reason (end_turn / max_tokens when the answer hits the cap)
  * request-id + anthropic-ratelimit-* response headers
"""
from __future__ import annotations

import random
import time
import uuid
from collections import deque

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

mock_app = FastAPI(title="Mock Anthropic API")

# Sliding-window request timestamps for a crude rate limiter.
_REQUESTS: deque[float] = deque()
_RATE_WINDOW_S = 1.0
_RATE_MAX = 8  # >8 requests within 1s -> 429. One turn's fan-out (4 calls) is
#              safe; the CHAOS_RATE_LIMIT burst (8 extra calls) reliably trips it.

# Simulated org rate-limit budget, surfaced via anthropic-ratelimit-* headers.
_REQ_LIMIT = 1000
_TOK_LIMIT = 200_000
_state = {"req_remaining": _REQ_LIMIT, "tok_remaining": _TOK_LIMIT, "reset_at": time.time() + 60}

_ANSWERS = [
    "Sure — here's a concise, helpful answer to your question.",
    "Great question. In short: it depends on your context, but generally yes.",
    "Here's what I'd suggest, step by step, based on best practices.",
    "Absolutely. Let me walk you through the key considerations.",
]

# Known models we'll happily "serve". Anything else -> 404, like the real API.
_KNOWN = {
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
    "claude-fable-5",
}


def _rate_headers() -> dict:
    """anthropic-ratelimit-* headers, as the real API returns on every response."""
    import datetime

    if time.time() > _state["reset_at"]:  # window rolled over — refill
        _state.update(req_remaining=_REQ_LIMIT, tok_remaining=_TOK_LIMIT, reset_at=time.time() + 60)
    reset_iso = datetime.datetime.utcfromtimestamp(_state["reset_at"]).isoformat() + "Z"
    return {
        "request-id": f"req_{uuid.uuid4().hex[:24]}",
        "anthropic-ratelimit-requests-limit": str(_REQ_LIMIT),
        "anthropic-ratelimit-requests-remaining": str(max(0, _state["req_remaining"])),
        "anthropic-ratelimit-requests-reset": reset_iso,
        "anthropic-ratelimit-tokens-limit": str(_TOK_LIMIT),
        "anthropic-ratelimit-tokens-remaining": str(max(0, _state["tok_remaining"])),
        "anthropic-ratelimit-tokens-reset": reset_iso,
    }


def _err(status: int, etype: str, message: str, extra_headers: dict | None = None) -> JSONResponse:
    headers = _rate_headers()
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": etype, "message": message}},
        headers=headers,
    )


@mock_app.get("/healthz")
def healthz():
    return {"status": "ok", "mock": True}


@mock_app.post("/v1/messages")
async def messages(request: Request):
    # --- rate limiting (429) ---
    now = time.time()
    _REQUESTS.append(now)
    while _REQUESTS and now - _REQUESTS[0] > _RATE_WINDOW_S:
        _REQUESTS.popleft()
    if len(_REQUESTS) > _RATE_MAX:
        return _err(
            429, "rate_limit_error", "Number of requests has exceeded your rate limit.",
            extra_headers={"retry-after": "5"},
        )

    body = await request.json()
    model = body.get("model", "unknown")

    # --- unknown model (404), mirrors the real API ---
    if model not in _KNOWN:
        return _err(404, "not_found_error", f"model: {model}")

    # --- token accounting derived from real request size ---
    system = body.get("system", "") or ""
    if isinstance(system, list):  # system can be a list of blocks
        system = " ".join(b.get("text", "") for b in system if isinstance(b, dict))
    msg_chars = 0
    for m in body.get("messages", []):
        content = m.get("content", "")
        if isinstance(content, str):
            msg_chars += len(content)
        elif isinstance(content, list):
            msg_chars += sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    input_tokens = max(1, (len(system) + msg_chars) // 4)  # ~4 chars/token

    max_tokens = int(body.get("max_tokens", 256))
    text = random.choice(_ANSWERS)
    raw_output = max(5, len(text) // 4 + random.randint(0, 60))
    output_tokens = min(max_tokens, raw_output)
    # PROBLEM #6 signal: tiny max_tokens truncates the answer -> stop_reason=max_tokens.
    stop_reason = "max_tokens" if raw_output > max_tokens else "end_turn"

    # PROBLEM #4 signal: the app never sets cache_control, so cache reads are always
    # 0 even though the huge system prompt repeats on every call (0% cache utilisation).
    cache_read = 0
    cache_creation = 0

    # Draw down the simulated budget so ratelimit-remaining trends toward the limit.
    _state["req_remaining"] -= 1
    _state["tok_remaining"] -= input_tokens + output_tokens

    # Small, size-proportional latency so duration metrics look realistic.
    time.sleep(min(0.8, 0.02 + output_tokens * 0.003))

    return JSONResponse(
        headers=_rate_headers(),
        content={
            "id": f"msg_{uuid.uuid4().hex[:24]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
                "service_tier": "standard",
            },
        },
    )

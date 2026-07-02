"""A tiny local stand-in for the Anthropic Messages API.

Lets the demo run with **no API key and no network access** while the real
Anthropic SDK + OpenLLMetry instrumentation stay completely unchanged — so
Dynatrace still receives genuine gen_ai.* spans with token usage, model,
latency, and errors. Token counts are derived from the actual request size,
so the oversized-prompt / unbounded-history problems produce real numbers.

Faithfully reproduces the behaviours the demo relies on:
  * 404 not_found_error for unknown models  (drives problem #8 error spans)
  * 429 rate_limit_error under burst load    (drives problem #6 / rate limits)
  * usage.input_tokens / usage.output_tokens (drives token & cost signals)
"""
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


def _err(status: int, etype: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": etype, "message": message}},
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
        return _err(429, "rate_limit_error", "Number of requests has exceeded your rate limit.")

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
    output_tokens = min(max_tokens, max(5, len(text) // 4 + random.randint(0, 40)))

    # Small, size-proportional latency so duration metrics look realistic.
    time.sleep(min(0.8, 0.02 + output_tokens * 0.003))

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }

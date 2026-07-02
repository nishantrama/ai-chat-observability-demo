"""Chat logic — deliberately riddled with GenAI anti-patterns.

Every problem here is a *real* behaviour that shows up in the Dynatrace
AI Observability app (token/cost spikes, latency, error rate, fan-out).
See PROBLEMS.md for the full catalogue and the DQL to detect each one.
"""
import logging
import random
import time

import anthropic
from opentelemetry import trace

from . import config

log = logging.getLogger("llm")
tracer = trace.get_tracer("ai-chat-demo")

# PROBLEM #6: max_retries=0 — no automatic retry/backoff on 429s or 5xx.
_client_kwargs = {"api_key": config.ANTHROPIC_API_KEY or "mock-key", "max_retries": 0}
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


def _call_model(model: str, system: str, messages: list[dict], max_tokens: int):
    """PROBLEM #6: raw call, no timeout param, no retry, no fallback model."""
    # PROBLEM #8: occasionally use an invalid model name -> real API error span.
    if random.random() < config.CHAOS_ERROR:
        model = "claude-does-not-exist-9000"
    return client.messages.create(
        model=model,
        system=system,
        max_tokens=max_tokens,
        temperature=1.0,  # PROBLEM #5: max temperature -> nondeterministic / hallucination-prone
        messages=messages,
    )


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

        _maybe_chaos()

        # PROBLEM #7: fan-out / N+1. One user turn triggers FOUR model calls
        # (moderate -> answer -> title -> summarise) instead of one.

        # 1) Moderation pass on the expensive model.
        _call_model(
            config.CHAT_MODEL,
            "Reply only SAFE or UNSAFE.",
            [{"role": "user", "content": f"Is this safe? {user_message}"}],
            max_tokens=5,
        )

        # 2) Main answer.
        # PROBLEM #3: always the expensive Opus model, never the cheap one.
        # PROBLEM #8 (injection): user text spliced straight into the system prompt.
        system = HUGE_SYSTEM_PROMPT + f"\nThe user's name may be: {user_message}"
        resp = _call_model(config.CHAT_MODEL, system, list(history), max_tokens=1024)
        answer = _text(resp)
        history.append({"role": "assistant", "content": answer})

        # 3) Title generation (again, no caching, expensive model).
        _call_model(
            config.CHAT_MODEL,
            "Return a 3-word title.",
            [{"role": "user", "content": answer}],
            max_tokens=15,
        )

        # 4) Rolling summary of the whole (growing) conversation.
        _call_model(
            config.CHAT_MODEL,
            "Summarise the conversation.",
            list(history),
            max_tokens=200,
        )

        span.set_attribute("gen_ai.response.model", getattr(resp, "model", config.CHAT_MODEL))
        return {"answer": answer, "history_len": len(history)}

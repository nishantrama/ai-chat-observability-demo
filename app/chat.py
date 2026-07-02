"""Chat turn logic — still riddled with anti-patterns, now routed via the gateway.

The prompt-shaping problems (unbounded history, oversized system prompt,
fan-out, PII on spans, prompt injection) live here. The actual model call —
and model *selection* — happens in the AI gateway, reached over HTTP with the
trace context injected so it joins the same distributed trace.
"""
from __future__ import annotations

import logging

import httpx
from opentelemetry import trace
from opentelemetry.propagate import inject

from common import config

log = logging.getLogger("chat")
tracer = trace.get_tracer("ai-chat-demo")

# PROBLEM #1: unbounded, process-global conversation memory (never trimmed).
CONVERSATIONS: dict[str, list[dict]] = {}

# PROBLEM #2: enormous static system prompt padded with filler, sent every call.
_FILLER = "You must always be helpful, harmless, and honest. " * 400
HUGE_SYSTEM_PROMPT = "You are ACME Assistant, an all-knowing enterprise concierge.\n" + _FILLER


def _gateway_call(kind: str, system: str, messages: list, max_tokens: int) -> dict:
    """Call the AI gateway, propagating W3C trace context so it's one trace."""
    payload = {
        "kind": kind, "system": system, "messages": messages,
        "max_tokens": max_tokens, "requested_model": config.CHAT_MODEL,
    }
    headers = {"content-type": "application/json"}
    with tracer.start_as_current_span("chat.gateway_call") as span:
        span.set_attribute("gateway.request.kind", kind)
        span.set_attribute("peer.service", config.GATEWAY_SERVICE_NAME)
        inject(headers)  # adds traceparent -> gateway server span becomes a child
        resp = httpx.post(f"{config.GATEWAY_URL}/v1/route", json=payload,
                          headers=headers, timeout=60.0)
        if resp.status_code >= 400:
            span.set_attribute("http.status_code", resp.status_code)
            span.set_status(trace.Status(trace.StatusCode.ERROR))
            raise RuntimeError(f"gateway {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        span.set_attribute("gateway.model.selected", data.get("model_selected", ""))
        span.set_attribute("gateway.route.reason", data.get("route_reason", ""))
        return data


def chat(session_id: str, user_message: str) -> dict:
    with tracer.start_as_current_span("chat.turn") as span:
        span.set_attribute("session.id", session_id)
        # PROBLEM #9: raw user content on the span (no PII redaction).
        span.set_attribute("chat.user_message", user_message)

        history = CONVERSATIONS.setdefault(session_id, [])
        history.append({"role": "user", "content": user_message})
        span.set_attribute("chat.history_len", len(history))
        log.info(
            "chat turn started: session=%s history_len=%s", session_id, len(history),
            extra={"session.id": session_id, "chat.history_len": len(history)},
        )

        # PROBLEM #7: fan-out / N+1 — one user turn -> FOUR gateway calls.
        # 1) Moderation
        _gateway_call("moderation", "Reply only SAFE or UNSAFE.",
                     [{"role": "user", "content": f"Is this safe? {user_message}"}], max_tokens=5)

        # 2) Main answer — PROBLEM #8 (injection): user text spliced into the system prompt.
        system = HUGE_SYSTEM_PROMPT + f"\nThe user's name may be: {user_message}"
        answer_data = _gateway_call("answer", system, list(history), max_tokens=1024)
        answer = answer_data["answer"]
        history.append({"role": "assistant", "content": answer})

        # 3) Title generation
        _gateway_call("title", "Return a 3-word title.",
                     [{"role": "user", "content": answer}], max_tokens=15)

        # 4) Rolling summary of the whole (growing) conversation
        _gateway_call("summary", "Summarise the conversation.", list(history), max_tokens=200)

        span.set_attribute("gateway.model.selected", answer_data.get("model_selected", ""))
        return {
            "answer": answer,
            "history_len": len(history),
            "model": answer_data.get("model_selected"),
            "route_reason": answer_data.get("route_reason"),
        }

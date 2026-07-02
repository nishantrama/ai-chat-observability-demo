"""Prompt-based model routing — every decision is a span.

The gateway inspects each incoming prompt and chooses a model. Each phase
(classify → safety → select) opens its own child span with the inputs and the
outcome as attributes/events, so a Dynatrace trace shows *why* a given model
was picked for a given call.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field

from opentelemetry import metrics, trace

from common import config

log = logging.getLogger("gateway.routing")
tracer = trace.get_tracer("ai-gateway")
meter = metrics.get_meter("ai-gateway")

_decisions = meter.create_counter(
    "gateway.routing.decisions",
    description="Routing decisions, dimensioned by selected model + category",
)

# Keyword signals used for classification.
_CODE = ["code", "python", "javascript", "typescript", "kubernetes", "function",
         "bug", "stack trace", "regex", "sql", "api", "compile", "docker"]
_MATH = ["calculate", "solve", "equation", "integral", "derivative", "probability", "2+2"]
_TRANSLATE = ["translate", "translation", "in french", "in spanish", "to french", "to spanish"]
_CREATIVE = ["story", "poem", "haiku", "imagine", "write me a"]
_GREETING = ["hi", "hello", "hey", "thanks", "thank you", "yo", "sup"]

_INJECTION = re.compile(r"ignore (all )?(previous|prior) instructions|system prompt|reveal", re.I)
_PII = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")  # SSN-shaped


@dataclass
class Decision:
    model: str
    category: str
    reason: str
    tokens_est: int
    safety_flags: list = field(default_factory=list)
    safety_enforced: bool = False
    misrouted: bool = False


def _categorize(text: str) -> str:
    low = text.lower()
    if any(k in low for k in _CODE):
        return "code"
    if any(k in low for k in _MATH):
        return "math"
    if any(k in low for k in _TRANSLATE):
        return "translation"
    if any(k in low for k in _CREATIVE):
        return "creative"
    if low.strip() in _GREETING or (len(low) < 20 and any(g in low for g in _GREETING)):
        return "trivial"
    return "general"


def route(kind: str, system: str, prompt_text: str, requested_model: str) -> Decision:
    with tracer.start_as_current_span("gateway.route") as span:
        span.set_attribute("gateway.request.kind", kind)
        span.set_attribute("gateway.request.requested_model", requested_model or "")
        span.set_attribute("gateway.router.mode", config.ROUTER_MODE)

        # --- 1. classify ---
        with tracer.start_as_current_span("gateway.classify") as cs:
            # PROBLEM G5: routing/classification overhead — pure latency the
            # gateway adds *before* any LLM call. Occasionally the classifier stalls.
            overhead_ms = config.GATEWAY_ROUTE_LATENCY_MS + random.randint(0, 40)
            if random.random() < 0.10:
                overhead_ms += random.randint(200, 500)  # classifier stall
            time.sleep(overhead_ms / 1000.0)
            tokens_est = max(1, (len(system) + len(prompt_text)) // 4)
            category = _categorize(prompt_text)
            cs.set_attribute("gateway.prompt.tokens_est", tokens_est)
            cs.set_attribute("gateway.prompt.category", category)
            cs.set_attribute("gateway.classify.overhead_ms", overhead_ms)
            cs.add_event("classified", {"category": category, "tokens_est": tokens_est})

        # --- 2. safety ---
        with tracer.start_as_current_span("gateway.policy.safety") as ss:
            flags = []
            if _INJECTION.search(prompt_text):
                flags.append("prompt_injection")
            if _PII.search(prompt_text):
                flags.append("pii")
            # PROBLEM G6: detect-but-don't-enforce — flags are recorded but the
            # request is routed through anyway unless enforcement is turned on.
            enforced = config.GATEWAY_ENFORCE_SAFETY
            ss.set_attribute("gateway.safety.flagged", bool(flags))
            ss.set_attribute("gateway.safety.flags", ",".join(flags))
            ss.set_attribute("gateway.safety.enforced", enforced)
            for f in flags:
                ss.add_event("safety_flag", {"flag": f, "enforced": enforced})

        # --- 3. select ---
        with tracer.start_as_current_span("gateway.select_model") as ms:
            if config.ROUTER_MODE == "passthrough":
                model, reason = requested_model or config.CHAT_MODEL, "passthrough mode"
            elif kind in ("moderation", "title"):
                model, reason = config.CHEAP_MODEL, f"low-stakes fan-out call ({kind}) -> cheap model"
            elif category == "trivial" and tokens_est < 200:
                model, reason = config.CHEAP_MODEL, "trivial short prompt -> cheap model"
            elif category in ("code", "math") or tokens_est > 3000:
                why = "large prompt" if tokens_est > 3000 else category
                model, reason = config.CHAT_MODEL, f"{why} -> most capable model"
            elif category in ("translation", "creative"):
                model, reason = config.MID_MODEL, f"{category} -> mid-tier model"
            else:
                model, reason = config.MID_MODEL, "general task -> mid-tier default"

            # PROBLEM G4: flaky classifier — occasionally route to the WRONG tier
            # (over-provision a trivial call to Opus, or under-provision a complex
            # one to Haiku). Cost waste or quality risk, and a category↔model mismatch.
            misrouted = False
            if config.ROUTER_MODE == "smart" and random.random() < config.GATEWAY_MISROUTE_RATE:
                model = config.CHAT_MODEL if model != config.CHAT_MODEL else config.CHEAP_MODEL
                reason = f"MISROUTED (flaky classifier) -> {model}"
                misrouted = True

            ms.set_attribute("gateway.model.selected", model)
            ms.set_attribute("gateway.route.reason", reason)
            ms.set_attribute("gateway.route.misrouted", misrouted)
            ms.set_attribute("gateway.route.overrode_request", model != (requested_model or model))

        # Roll the decision up onto the parent route span.
        span.set_attribute("gateway.prompt.category", category)
        span.set_attribute("gateway.prompt.tokens_est", tokens_est)
        span.set_attribute("gateway.model.selected", model)
        span.set_attribute("gateway.route.reason", reason)
        span.set_attribute("gateway.route.misrouted", misrouted)
        span.set_attribute("gateway.safety.flagged", bool(flags))
        span.set_attribute("gateway.safety.enforced", enforced)

        _decisions.add(1, {"gateway.model.selected": model, "gateway.prompt.category": category,
                           "llm.call_kind": kind, "gateway.route.misrouted": str(misrouted)})
        log.info(
            "route decision: %s -> %s (%s)", kind, model, reason,
            extra={
                "gateway.request.kind": kind,
                "gateway.prompt.category": category,
                "gateway.prompt.tokens_est": tokens_est,
                "gateway.model.selected": model,
                "gateway.route.reason": reason,
                "gateway.route.misrouted": misrouted,
                "gateway.route.requested_model": requested_model,
                "gateway.safety.flags": ",".join(flags),
                "gateway.safety.enforced": enforced,
            },
        )
        return Decision(model, category, reason, tokens_est, flags,
                        safety_enforced=enforced, misrouted=misrouted)

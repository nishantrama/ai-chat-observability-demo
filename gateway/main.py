"""AI Gateway service — routes each chat call to a model based on the prompt.

Runs as its own service (service.name = "ai-gateway"). The chat app calls
POST /v1/route with W3C trace context, so the gateway's work — routing
decisions and the upstream LLM call — attaches to the same distributed trace.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import config
from common.telemetry import setup_telemetry

logging.basicConfig(level=logging.INFO)


def _start_mock_server() -> None:
    """Run the local mock Anthropic API in a background thread (no key needed)."""
    import uvicorn

    from .mock_anthropic import mock_app

    def _run():
        cfg = uvicorn.Config(mock_app, host=config.MOCK_HOST, port=config.MOCK_PORT, log_level="warning")
        server = uvicorn.Server(cfg)
        server.install_signal_handlers = lambda: None
        server.run()

    threading.Thread(target=_run, daemon=True).start()
    for _ in range(50):
        try:
            with socket.create_connection((config.MOCK_HOST, config.MOCK_PORT), 0.1):
                logging.info("Mock Anthropic ready on %s:%s", config.MOCK_HOST, config.MOCK_PORT)
                return
        except OSError:
            time.sleep(0.1)
    logging.warning("Mock Anthropic did not come up in time")


if config.USE_MOCK:
    _start_mock_server()

app = FastAPI(title="AI Gateway")
setup_telemetry(app, service_name=config.GATEWAY_SERVICE_NAME, instrument_anthropic=True)

# Imported after telemetry so the Anthropic instrumentor is already active.
from . import routing, upstream  # noqa: E402


class RouteRequest(BaseModel):
    kind: str = "answer"
    system: str = ""
    messages: list = []
    max_tokens: int = 1024
    requested_model: str = config.CHAT_MODEL


def _last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content", "")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


@app.get("/healthz")
def healthz():
    return {"status": "ok", "service": config.GATEWAY_SERVICE_NAME}


@app.post("/v1/route")
def route(req: RouteRequest):
    prompt_text = _last_user_text(req.messages)
    decision = routing.route(req.kind, req.system, prompt_text, req.requested_model)
    try:
        result = upstream.call(decision.model, req.system, req.messages, req.max_tokens, req.kind)
    except Exception as exc:  # noqa: BLE001
        logging.exception("gateway upstream failed")
        return JSONResponse(status_code=502, content={
            "error": str(exc), "error_type": type(exc).__name__,
            "model_selected": decision.model, "route_reason": decision.reason,
        })
    return {
        "answer": result["text"],
        "model_selected": decision.model,
        "route_category": decision.category,
        "route_reason": decision.reason,
        "safety_flags": decision.safety_flags,
        "usage": {
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "estimated_cost_usd": result["estimated_cost_usd"],
            "stop_reason": result["stop_reason"],
        },
    }

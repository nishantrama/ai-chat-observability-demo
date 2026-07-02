"""FastAPI entrypoint for the chat service.

The chat app no longer calls Anthropic directly — every model call now goes
through the AI gateway (a separate service), so the LLM work shows up under the
gateway in Dynatrace and the whole turn is one distributed trace.
"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from common import config
from common.telemetry import setup_telemetry

from . import chat as chat_logic

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Chat Observability Demo")
setup_telemetry(app, service_name=config.CHAT_SERVICE_NAME, instrument_anthropic=False)

_STATIC = os.path.join(os.path.dirname(__file__), "static")


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC, "index.html"))


@app.get("/healthz")
def healthz():
    return {"status": "ok", "gateway": config.GATEWAY_URL}


@app.post("/api/chat")
def chat(req: ChatRequest):
    try:
        return chat_logic.chat(req.session_id, req.message)
    except Exception as exc:  # noqa: BLE001
        # PROBLEM #6: errors bubble up to a raw 500 with no graceful degradation.
        logging.exception(
            "chat failed",
            extra={"session.id": req.session_id, "error.type": type(exc).__name__,
                   "http.status_code": 500},
        )
        return JSONResponse(status_code=500, content={"error": str(exc)})


app.mount("/static", StaticFiles(directory=_STATIC), name="static")

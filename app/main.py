"""FastAPI entrypoint for the AI chat observability demo."""
import logging
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm
from .telemetry import setup_telemetry

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="AI Chat Observability Demo")
setup_telemetry(app)

_STATIC = os.path.join(os.path.dirname(__file__), "static")


class ChatRequest(BaseModel):
    session_id: str = "default"
    message: str


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC, "index.html"))


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/api/chat")
def chat(req: ChatRequest):
    try:
        result = llm.chat(req.session_id, req.message)
        return result
    except Exception as exc:  # noqa: BLE001
        # PROBLEM #6: errors bubble up to a raw 500 with no graceful degradation.
        logging.exception("chat failed")
        return JSONResponse(status_code=500, content={"error": str(exc)})


app.mount("/static", StaticFiles(directory=_STATIC), name="static")

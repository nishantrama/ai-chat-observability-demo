"""FastAPI entrypoint for the AI chat observability demo."""
import logging
import os
import socket
import threading
import time

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, llm
from .telemetry import setup_telemetry

logging.basicConfig(level=logging.INFO)


def _start_mock_server() -> None:
    """Run the local mock Anthropic API in a background thread (no key needed)."""
    import uvicorn

    from .mock_anthropic import mock_app

    def _run():
        cfg = uvicorn.Config(
            mock_app, host=config.MOCK_HOST, port=config.MOCK_PORT, log_level="warning"
        )
        server = uvicorn.Server(cfg)
        server.install_signal_handlers = lambda: None  # allowed off the main thread
        server.run()

    threading.Thread(target=_run, daemon=True).start()

    # Wait until it accepts connections so the first chat call doesn't race it.
    for _ in range(50):
        try:
            with socket.create_connection((config.MOCK_HOST, config.MOCK_PORT), 0.1):
                logging.info("Mock Anthropic server ready on %s:%s", config.MOCK_HOST, config.MOCK_PORT)
                return
        except OSError:
            time.sleep(0.1)
    logging.warning("Mock Anthropic server did not come up in time")


if config.USE_MOCK:
    _start_mock_server()

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
        logging.exception(
            "chat failed",
            extra={
                "session.id": req.session_id,
                "error.type": type(exc).__name__,
                "http.status_code": 500,
            },
        )
        return JSONResponse(status_code=500, content={"error": str(exc)})


app.mount("/static", StaticFiles(directory=_STATIC), name="static")

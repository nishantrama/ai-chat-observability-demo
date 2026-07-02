# AI Chat Observability Demo

A deliberately-flawed sample **AI chat application** built on an AI-native stack,
instrumented end-to-end with **OpenTelemetry / OpenLLMetry** so that the
**Dynatrace AI Observability** app has plenty of real problems to detect:
token/cost blowups, latency spikes, LLM error rates, model-cost inefficiency,
fan-out, prompt injection, and unredacted PII.

> ⚠️ This app is intentionally bad. It is a teaching/demo artifact — do **not**
> ship any of this. See [`PROBLEMS.md`](PROBLEMS.md) for the full catalogue and
> the DQL to detect each issue.

## Stack

| Layer | Choice |
|-------|--------|
| API | FastAPI + Uvicorn |
| LLM | Anthropic Claude (`claude-opus-4-8`) via the official SDK |
| Observability | OpenTelemetry SDK + OpenLLMetry Anthropic instrumentor → OTLP → Dynatrace |
| UI | Single-file static chat page |

The OpenLLMetry Anthropic instrumentor emits spans with OpenTelemetry **GenAI
semantic-convention** attributes (`gen_ai.request.model`, token usage, etc.) plus
the `gen_ai.client.token.usage` and `gen_ai.client.operation.duration` metrics —
exactly what the Dynatrace AI Observability app ingests.

## No API key? No problem — mock mode

You do **not** need an Anthropic API key. When `ANTHROPIC_API_KEY` is blank (or
`MOCK_ANTHROPIC=true`), the app boots a built-in **mock Anthropic server** that
speaks the real Messages API. The genuine Anthropic SDK + OpenLLMetry
instrumentation run unchanged and pointed at `localhost`, so Dynatrace still
receives real `gen_ai.*` spans, token counts (derived from actual request size),
latency, and errors — with zero network calls and zero cost. The mock even
returns real `404` (unknown model) and `429` (rate-limit) errors to drive the
error-rate problems.

To use the real Anthropic API instead, set `ANTHROPIC_API_KEY` and
`MOCK_ANTHROPIC=false`.

## Quick start

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Optional: set the Dynatrace OTLP endpoint + token. No Anthropic key needed —
# mock mode is on by default when ANTHROPIC_API_KEY is blank.

uvicorn app.main:app --reload --port 8000
```

Open <http://localhost:8000> and chat. Then generate load for richer telemetry:

```bash
python scripts/generate_load.py --turns 40 --sessions 5
```

## Pointing at Dynatrace

Set these in `.env` (token needs `openTelemetryTrace.ingest` + `metrics.ingest`):

```
OTEL_EXPORTER_OTLP_ENDPOINT=https://<env-id>.live.dynatrace.com/api/v2/otlp
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Api-Token dt0c01.XXXXXXXX
```

If the endpoint is left blank, spans and metrics print to the console so you can
still see the instrumentation working without a backend.

## What to look for in Dynatrace

Open the **AI Observability** app and you'll see this service (`ai-chat-observability-demo`)
with its GenAI calls. The problems in [`PROBLEMS.md`](PROBLEMS.md) manifest as
cost/token spikes, a 100%-Opus model mix, elevated error rate, latency long-tail,
and ~4 LLM calls per chat turn.

## Chaos toggles

Tune the failure injection in `.env`:

- `CHAOS_LATENCY` — probability of injected slow responses
- `CHAOS_ERROR` — probability of a real invalid-model API error
- `CHAOS_RATE_LIMIT` — probability of a burst that provokes 429s

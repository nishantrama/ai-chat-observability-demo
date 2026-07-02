# AI Chat Observability Demo

A deliberately-flawed sample **AI chat application** built on an AI-native stack,
instrumented end-to-end with **OpenTelemetry / OpenLLMetry** so that the
**Dynatrace AI Observability** app has plenty of real problems to detect:
token/cost blowups, latency spikes, LLM error rates, model-cost inefficiency,
fan-out, prompt injection, and unredacted PII.

> ⚠️ This app is intentionally bad. It is a teaching/demo artifact — do **not**
> ship any of this. See [`PROBLEMS.md`](PROBLEMS.md) for the full catalogue and
> the DQL to detect each issue.

## Architecture — two instrumented services

```
  browser ──HTTP──▶  chat service            ──HTTP (W3C traceparent)──▶  ai-gateway            ──Anthropic SDK──▶  mock / real Anthropic
                     (ai-chat-observability-demo)                          (ai-gateway)
                     builds the (flawed) requests:                         routes each call to a model
                     unbounded history, huge prompt,                       based on the prompt, then makes
                     fan-out (4 calls/turn), PII on spans                   the LLM call + enriches the span
```

Both services are independently OpenTelemetry-instrumented and export to the
**same** Dynatrace tenant, and trace context is propagated on the chat→gateway
hop — so **one chat turn is a single distributed trace** spanning both services,
and Dynatrace draws the service flow `chat → ai-gateway → Anthropic`.

| Layer | Choice |
|-------|--------|
| Services | `chat` (FastAPI, :8000) + `ai-gateway` (FastAPI, :8090) |
| LLM | Anthropic Claude via the official SDK (called from the gateway) |
| Routing | Custom, prompt-based; **every decision is a span** (`gateway.classify` → `policy.safety` → `select_model`) |
| Observability | OpenTelemetry SDK + OpenLLMetry Anthropic instrumentor → OTLP → Dynatrace (traces, metrics, logs) |
| UI | Single-file static chat page (shows the routed model per turn) |

**The gateway decides the model from the prompt** (`ROUTER_MODE=smart`):
low-stakes fan-out calls (moderation/title) → Haiku, moderate tasks → Sonnet,
code/math or oversized prompts → Opus. Set `ROUTER_MODE=passthrough` to force
the original always-Opus behaviour (problem #3) for a before/after demo.

The OpenLLMetry Anthropic instrumentor (in the gateway) emits spans with
OpenTelemetry **GenAI semantic-convention** attributes plus token/cost/latency
metrics — exactly what the Dynatrace AI Observability app ingests.

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

./run.sh          # starts ai-gateway (:8090, + mock upstream :8080) and chat (:8000)
```

`run.sh` launches **both** services. (To run them by hand:
`uvicorn gateway.main:app --port 8090` then `uvicorn app.main:app --port 8000`.)

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

- **Service flow**: `ai-chat-observability-demo → ai-gateway → Anthropic`, two
  distinct services on one distributed trace per chat turn.
- **Distributed trace waterfall**: `chat.turn` → `chat.gateway_call` → (gateway)
  `gateway.route` → `gateway.classify` / `gateway.policy.safety` /
  `gateway.select_model` → the `gen_ai` LLM span. You can read *why* each model
  was chosen from the `gateway.route.reason` / `gateway.prompt.category` span
  attributes — every routing decision is traced.
- **AI Observability** app: GenAI calls now attributed to `ai-gateway`, with a
  **varied model mix** (Haiku/Sonnet/Opus) instead of 100% Opus — problem #3 is
  now mitigated *and* observable. The other problems in [`PROBLEMS.md`](PROBLEMS.md)
  (cost/token spikes, error rate, latency long-tail, ~4 calls/turn fan-out) remain.

## Chaos toggles

Tune the failure injection in `.env`:

- `CHAOS_LATENCY` — probability of injected slow responses
- `CHAOS_ERROR` — probability of a real invalid-model API error
- `CHAOS_RATE_LIMIT` — probability of a burst that provokes 429s

**Gateway-layer problems** (G1–G6 in [`PROBLEMS.md`](PROBLEMS.md)) have their own toggles:

- `GATEWAY_CACHE_ENABLED` — broken response cache that never hits (G1)
- `GATEWAY_MAX_RETRIES` — retry-storm count, no backoff (G2)
- `GATEWAY_FALLBACK_ENABLED` — silent downgrade to the cheapest model (G3)
- `GATEWAY_MISROUTE_RATE` — flaky-classifier misroute probability (G4)
- `GATEWAY_ROUTE_LATENCY_MS` — routing overhead before each LLM call (G5)
- `GATEWAY_ENFORCE_SAFETY` — `false` = detect-but-don't-enforce (G6)

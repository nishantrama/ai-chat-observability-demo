# Dashboards

## `ai-observability-dashboard.json`

An importable **Dynatrace** dashboard (schema v10) for this demo, organized into
three persona sections:

- **🏦 Executive** — total LLM spend, spend trend & cost-by-model, success rate,
  chat turns, LLM calls, tokens consumed, failed calls.
- **📊 Director / VP** — model distribution (most popular model), cost by call
  type (fan-out driver), cache hit ratio (G1 → ~0), misroute rate (G4), safety
  flagged-vs-enforced (G6), LLM calls per turn (fan-out #7), token usage by model.
- **🛠️ SRE / Developer** — LLM error rate over time, errors by type, latency
  p50/p95/p99, retry storm (G2), silent fallbacks (G3), rate-limit headroom,
  routing overhead (G5), upstream attempts, slowest calls.

### Import

1. In Dynatrace, open the **Dashboards** app → **Upload** (or **＋ → Upload
   dashboard**) and pick this file. (Or paste the JSON via the dashboard's
   "Edit as JSON" view.)
2. Make sure the demo has been sending data to the tenant (run `./run.sh` +
   `scripts/generate_load.py`) and set the dashboard timeframe to cover the run.

### Notes on the queries

- **Cost / routing / retries / fallbacks / cache** tiles use the custom metrics
  the gateway emits: `gen_ai.client.estimated_cost.usd`, `gateway.routing.decisions`,
  `gateway.upstream.retries`, `gateway.fallbacks`, `gateway.cache.lookups`,
  `anthropic.ratelimit.tokens_remaining`.
- **Rates / latency / errors / slowest** tiles run over **spans**
  (`fetch spans … | makeTimeseries/summarize`).
- **Token** tiles read the `gen_ai.usage.total_tokens` attribute from the
  trace-stitched **logs**. If your tenant stores that attribute as a string,
  swap those two tiles to the OpenLLMetry metric `gen_ai.client.token.usage`.
- Attribute/field names follow what the app emits (see `PROBLEMS.md`); if a tile
  is empty, confirm the field name in a Notebook first — OTLP attribute keys can
  vary slightly by Dynatrace ingest version.

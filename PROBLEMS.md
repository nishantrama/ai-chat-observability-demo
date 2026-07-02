# The problem catalogue

Every anti-pattern below is a *real* behaviour of this app. Each one produces a
signal the **Dynatrace AI Observability** app can surface, along with example
DQL you can drop into a Notebook or Dashboard. Traces arrive as spans carrying
the OpenTelemetry GenAI semantic-convention attributes (`gen_ai.*`) emitted by
the OpenLLMetry Anthropic instrumentor, plus the `gen_ai.client.token.usage`
and `gen_ai.client.operation.duration` metrics.

| # | Anti-pattern | Where | Symptom in Dynatrace AI Observability |
|---|--------------|-------|----------------------------------------|
| 1 | Unbounded conversation history (never trimmed) | `llm.py` `CONVERSATIONS` | Input tokens & cost climb turn-over-turn; latency creeps up |
| 2 | Enormous padded system prompt on every call | `llm.py` `HUGE_SYSTEM_PROMPT` | Huge input-token floor even for "hi" |
| 3 | Chat always *requests* Opus | `app/chat.py` (`requested_model`) | **Now mitigated by the gateway** — with `ROUTER_MODE=smart` the gateway routes by prompt (Haiku/Sonnet/Opus), so the model mix is varied and the shift is visible. Set `ROUTER_MODE=passthrough` to restore 100%-Opus |
| 4 | No response caching (identical prompts re-billed) | `gateway/upstream.py` | Duplicate prompts, repeated spend, cache-read tokens always 0 |
| 5 | `temperature=1.0` everywhere | `gateway/upstream.py` `call` | Nondeterministic output / hallucination risk |
| 6 | No timeout, no retry/backoff, raw 500s | `gateway/upstream.py`, `app/chat.py` | Long-tail latency spikes; unhandled errors → failure rate on both services |
| 7 | Fan-out / N+1 — 4 gateway calls per user turn | `app/chat.py` `chat` | Request count 4× turns; cost & token multiplier |
| 8 | Prompt injection + invalid-model errors | `app/chat.py`, `gateway/upstream.py` | Error spans (`claude-does-not-exist-9000`), injection exposure |
| 9 | Full prompt/PII captured on spans, no redaction | `common/telemetry.py`, `app/chat.py` | Sensitive user content (SSN, names) visible in trace attributes |

## Gateway-layer problems

These are anti-patterns of the **gateway itself** (distinct from the LLM/chat
problems above), each toggleable via env and fully traced under `ai-gateway`:

| # | Anti-pattern | Where | Symptom in Dynatrace |
|---|--------------|-------|----------------------|
| G1 | Broken response cache — key includes a per-request nonce, so it never hits | `gateway/upstream.py` `_broken_cache_key` | `gateway.cache_lookup` spans with `gateway.cache.hit=false` always; 0% hit ratio; lookup latency + unbounded `gateway.cache.size` growth |
| G2 | Retry storm — retries the upstream call with **no backoff** | `gateway/upstream.py` `serve` (`GATEWAY_MAX_RETRIES`) | `gateway.upstream.attempts>1`, repeated `gateway.upstream.retry` span events, amplified cost/latency, self-inflicted 429s |
| G3 | Silent fallback downgrade — quietly serves the cheapest model after retries fail | `gateway/upstream.py` `serve` (`GATEWAY_FALLBACK_ENABLED`) | `gateway.fallback.used=true` with `from`/`to`; response `model_selected` ≠ routed model (silent quality degradation) |
| G4 | Misrouting — flaky classifier routes to the wrong tier | `gateway/routing.py` (`GATEWAY_MISROUTE_RATE`) | `gateway.route.misrouted=true`; category↔model mismatch (trivial→Opus waste, complex→Haiku quality risk) |
| G5 | Routing overhead — classification adds latency before any LLM call | `gateway/routing.py` (`GATEWAY_ROUTE_LATENCY_MS`) | `gateway.classify` span duration + `gateway.classify.overhead_ms`; occasional classifier stalls |
| G6 | Detect-but-don't-enforce — flags injection/PII but routes through anyway | `gateway/routing.py`, `gateway/main.py` (`GATEWAY_ENFORCE_SAFETY`) | `gateway.safety.flagged=true` while `gateway.safety.enforced=false`; flagged prompts still served |

### Gateway-problem DQL

**Cache hit ratio — always 0 (G1):**
```
timeseries lookups = sum(gateway.cache.lookups), by:{`gateway.cache.hit`}, interval:1m
```

**Retry amplification (G2) — attempts per call:**
```
fetch spans | filter isNotNull(gateway.upstream.attempts)
| summarize avg_attempts = avg(gateway.upstream.attempts), max_attempts = max(gateway.upstream.attempts)
```

**Silent fallbacks (G3):**
```
timeseries fallbacks = sum(gateway.fallbacks), by:{`gateway.fallback.to`}, interval:1m
```

**Misroutes (G4):**
```
fetch logs | filter gateway.route.misrouted == true
| summarize misroutes = count(), by:{gateway.model.selected, gateway.prompt.category}
```

**Detect-but-don't-enforce (G6) — flagged yet served:**
```
fetch spans | filter gateway.safety.flagged == true and gateway.safety.enforced == false
| summarize served_despite_flag = count(), by:{gateway.safety.flags}
```

## The AI gateway (routing) — traced decisions

Every chat call is routed through the **`ai-gateway`** service, which picks a
model from the prompt. Each phase is its own span, so a trace shows the decision:

| Span | Attributes |
|---|---|
| `gateway.route` | `gateway.request.kind`, `gateway.router.mode`, rolled-up decision |
| `gateway.classify` | `gateway.prompt.category`, `gateway.prompt.tokens_est` |
| `gateway.policy.safety` | `gateway.safety.flagged`, `gateway.safety.flags` (injection / PII) |
| `gateway.select_model` | `gateway.model.selected`, `gateway.route.reason`, `gateway.route.overrode_request` |

Metric: `gateway.routing.decisions` (by `gateway.model.selected` + `gateway.prompt.category`).

**Model mix chosen by the gateway (problem #3 — before/after):**
```
timeseries decisions = sum(gateway.routing.decisions), by:{gateway.model.selected}, interval:1m
```

**Routing reasons — why did it pick what it picked?**
```
fetch logs
| filter isNotNull(gateway.route.reason)
| summarize calls = count(), by:{gateway.route.reason, gateway.model.selected}
| sort calls desc
```

**Safety flags raised at the gateway (problem #8):**
```
fetch logs | filter gateway.safety.flags != "" | fields timestamp, gateway.safety.flags, gateway.prompt.category
```

## Example DQL

**Token cost trend (spot problems #1, #2, #7):**
```
timeseries tokens = sum(gen_ai.client.token.usage), by:{gen_ai.request.model}, interval:1m
```

**Model distribution — is everything on Opus? (problem #3):**
```
fetch spans
| filter isNotNull(gen_ai.request.model)
| summarize calls = count(), by:{gen_ai.request.model}
| sort calls desc
```

**LLM error rate (problems #6, #8):**
```
fetch spans
| filter isNotNull(gen_ai.request.model)
| summarize total = count(), errors = countIf(span.status_code == "ERROR")
| fieldsAdd error_rate = (errors / total) * 100
```

**Slowest LLM calls (problem #6):**
```
fetch spans
| filter isNotNull(gen_ai.request.model)
| fields gen_ai.request.model, duration, span.name
| sort duration desc
| limit 20
```

**Fan-out — model calls per chat turn (problem #7):**
```
fetch spans
| filter span.name == "chat.turn" or isNotNull(gen_ai.request.model)
| summarize turns = countIf(span.name == "chat.turn"),
            llm_calls = countIf(isNotNull(gen_ai.request.model))
| fieldsAdd calls_per_turn = llm_calls / turns
```

## Anthropic-specific telemetry captured

On top of the standard `gen_ai.*` spans/metrics, every LLM call now records the
fields the Anthropic API actually returns — on the span, as metrics, and on the
trace-stitched logs:

| Signal | Where it comes from | Why it matters |
|---|---|---|
| `gen_ai.usage.estimated_cost_usd` + metric `gen_ai.client.estimated_cost.usd` | token usage × model pricing | Real cost per call/turn (answer calls dominate — problem #2) |
| `gen_ai.usage.cache_read_input_tokens` + metric `gen_ai.client.cache_read_tokens` | `usage.cache_read_input_tokens` | Always **0** — proves 0% prompt-cache utilisation (problem #4) despite the repeated huge prompt |
| `gen_ai.response.finish_reason` | `stop_reason` | `max_tokens` flags truncated answers (problem #6) |
| `anthropic.ratelimit.tokens_remaining` (span + histogram) | `anthropic-ratelimit-*` headers | How close the workload is to the token rate limit |
| `anthropic.request_id`, `gen_ai.response.id`, `gen_ai.usage.service_tier` | response headers / body | Support correlation + tier visibility |

### Extra DQL

**Estimated cost by fan-out call type (problems #2, #7):**
```
timeseries cost = sum(gen_ai.client.estimated_cost.usd), by:{llm.call_kind}, interval:1m
```

**Prompt-cache utilisation — is it always zero? (problem #4):**
```
fetch logs
| filter isNotNull(llm.call_kind)
| summarize cache_read = sum(gen_ai.usage.cache_read_input_tokens),
            input = sum(gen_ai.usage.input_tokens)
| fieldsAdd cache_hit_ratio_pct = (cache_read / input) * 100
```

**Truncated responses (problem #6):**
```
fetch logs
| filter gen_ai.response.finish_reason == "max_tokens"
| summarize truncated = count(), by:{llm.call_kind}
```

## Fixing them (the "after")

To demo remediation, flip these in `llm.py`: trim history to the last N turns,
shrink the system prompt, route trivial prompts to `CHEAP_MODEL`, add an
in-memory cache keyed on the prompt, set `temperature=0.2`, add a client
timeout + retry, collapse the fan-out to a single call, and disable content
capture (`AnthropicInstrumentor(enrich_token_usage=True)` with
`TRACELOOP_TRACE_CONTENT=false`).

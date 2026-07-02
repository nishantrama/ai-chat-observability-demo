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
| 3 | Always uses expensive Opus, never the cheap model | `llm.py` `_call_model` | Cost per request far above necessary; model distribution 100% Opus |
| 4 | No response caching (identical prompts re-billed) | `llm.py` | Duplicate prompts, repeated spend, no cache-hit ratio |
| 5 | `temperature=1.0` everywhere | `llm.py` `_call_model` | Nondeterministic output / hallucination risk |
| 6 | No timeout, no retry/backoff, raw 500s | `llm.py`, `main.py` | Long-tail latency spikes; unhandled errors → failure rate |
| 7 | Fan-out / N+1 — 4 model calls per user turn | `llm.py` `chat` | Request count 4× turns; cost & token multiplier |
| 8 | Prompt injection + invalid-model errors | `llm.py` | Error spans (`claude-does-not-exist-9000`), injection exposure |
| 9 | Full prompt/PII captured on spans, no redaction | `telemetry.py`, `llm.py` | Sensitive user content (SSN, names) visible in trace attributes |

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

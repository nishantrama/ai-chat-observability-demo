# Davis anomaly detectors

Nine **Davis anomaly detectors** (Dynatrace schema `builtin:davis.anomaly-detectors`),
one per app/gateway problem. Each raises a Davis **Problem** named after the
demo problem when its DQL metric crosses a static threshold.

| Problem (event name) | DQL query | Condition | Threshold |
|---|---|---|---|
| **#2/#7** · AI cost spike | `timeseries cost = sum(gen_ai.client.estimated_cost.usd), interval:1m` | ABOVE | `0.5` USD/min |
| **#1/#2** · LLM token consumption spike | `fetch logs \| filter isNotNull(gen_ai.usage.total_tokens) \| makeTimeseries tokens = sum(gen_ai.usage.total_tokens), interval:1m` | ABOVE | `20000` |
| **#6/#8** · LLM error rate elevated | `fetch spans \| filter isNotNull(gen_ai.request.model) \| makeTimeseries total = count(), errors = countIf(span.status_code == "ERROR"), interval:1m \| fieldsAdd error_rate = (errors[]/total[])*100 \| fieldsRemove total, errors` | ABOVE | `10` % |
| **#6** · LLM latency degradation (p95) | `fetch spans \| filter isNotNull(gen_ai.request.model) \| makeTimeseries p95_ms = percentile(duration, 95)/1000000.0, interval:1m` | ABOVE | `4000` ms |
| **G2** · Gateway retry storm | `timeseries retries = sum(gateway.upstream.retries), interval:1m` | ABOVE | `5` |
| **G3** · Gateway silent fallback | `timeseries fallbacks = sum(gateway.fallbacks), interval:1m` | ABOVE | `0` |
| **G4** · Gateway misrouting | `timeseries misroutes = sum(gateway.routing.decisions), filter:{gateway.route.misrouted == "True"}, interval:1m` | ABOVE | `2` |
| **G1** · Gateway cache 0% hit rate | `timeseries misses = sum(gateway.cache.lookups), filter:{gateway.cache.hit == "False"}, interval:1m` | ABOVE | `20` |
| **#6** · Anthropic rate-limit headroom low | `timeseries tokens_remaining = avg(anthropic.ratelimit.tokens_remaining), interval:1m` | BELOW | `30000` |

Thresholds are demo defaults — tune per your traffic.

## ⚠️ Creating these requires OAuth (not a classic API token)

Davis detectors run their DQL under an OAuth identity, so the Settings API
rejects a classic `dt0c01` token ("Could not do validation as request was not
done using oAuth"). Two ways to apply them:

### Option A — apply script (OAuth / platform token)

```bash
export DT_ENV="https://<env-id>.live.dynatrace.com"
export DT_BEARER="<platform token dt0s16… or OAuth access token>"
export DT_ACTOR="<actor uuid>"     # identity the detector query runs as
python3 anomaly-detectors/apply.py          # validate
python3 anomaly-detectors/apply.py create    # create
```

Token scopes needed: `settings:objects:write`, `settings:objects:read`, and
storage read for the queried data (`storage:metrics:read`, `storage:spans:read`,
`storage:logs:read`, `storage:buckets:read`).

### Option B — Davis Anomaly Detection app (UI, no secrets)

For each row above: **Davis Anomaly Detection** app → **＋ Anomaly detector** →
paste the DQL, set the condition + threshold, and set the event name to the
problem name (**Event type: Custom alert** so it opens a Problem).

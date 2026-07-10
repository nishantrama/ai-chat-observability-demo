# Service-level failure-rate detection (native Davis → root cause)

Custom `builtin:davis.anomaly-detectors` problems are single threshold events and
**never get a computed root cause**. Davis's automatic root-cause analysis only
runs on its **native** detections (service failure-rate / response-time) over the
Smartscape topology.

These two `builtin:anomaly-detection.services` objects turn on **fixed-threshold
failure-rate detection** (>10%, high sensitivity) for just our two services
(scoped by SERVICE entity id — the shared tenant is untouched). When the gateway
degrades and chat requests fail, Davis raises a **native service Problem** and
walks the `ai-chat → ai-gateway` call edge to name **ai-gateway as the root
cause** of the ai-chat failure-rate increase.

Apply with an OAuth/platform token (settings:objects:write):
`POST /api/v2/settings/objects` with each file's `{schemaId, scope, value}`.
Replace the `SERVICE-…` scope ids with your tenant's (find via
`fetch spans | summarize count(), by:{entityName(dt.entity.service)}`).

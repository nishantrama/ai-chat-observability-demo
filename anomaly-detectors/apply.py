#!/usr/bin/env python3
"""Create the Davis anomaly detectors in a Dynatrace environment.

Davis anomaly detectors (schema `builtin:davis.anomaly-detectors`) run the
detector's DQL under an OAuth identity, so the Settings API REQUIRES an OAuth
bearer token — a classic `dt0c01` API token is rejected with
"Could not do validation as request was not done using oAuth".

Usage:
    export DT_ENV="https://<env-id>.live.dynatrace.com"
    export DT_BEARER="<platform token dt0s16... or OAuth access token>"
    export DT_ACTOR="<actor uuid>"     # the identity the detector query runs as
    python3 apply.py                    # validate only (default)
    python3 apply.py create             # actually create

Required token scopes: settings:objects:write, settings:objects:read, and
storage read for what the queries touch (storage:metrics:read,
storage:spans:read, storage:logs:read, storage:buckets:read).
"""
import glob
import json
import os
import sys
import urllib.request

ENV = os.environ["DT_ENV"].rstrip("/")
BEARER = os.environ["DT_BEARER"]
ACTOR = os.environ.get("DT_ACTOR", "")
CREATE = len(sys.argv) > 1 and sys.argv[1] == "create"
H = {"Authorization": f"Bearer {BEARER}", "Content-Type": "application/json"}

ok = 0
files = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "*.json")))
for f in files:
    obj = json.load(open(f))
    if ACTOR:
        obj["value"].setdefault("executionSettings", {})["actor"] = ACTOR
    url = ENV + "/api/v2/settings/objects" + ("" if CREATE else "?validateOnly=true")
    req = urllib.request.Request(url, data=json.dumps([obj]).encode(), headers=H, method="POST")
    title = obj["value"]["title"]
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ok += 1
            print(f"[{'CREATED' if CREATE else 'VALID'} {r.status}] {title}")
    except urllib.error.HTTPError as e:
        print(f"[FAIL {e.code}] {title}\n     {e.read().decode()[:200]}")
print(f"\n{ok}/{len(files)} {'created' if CREATE else 'valid'}")

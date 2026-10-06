#!/usr/bin/env python3
"""After triggering an event in the Playground: list events, then fetch a snapshot at the event time."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spike_playground as sp

sp.TOKEN = os.environ["RING_TOKEN"]
_, devs = sp.call("GET", "/v1/devices")
dev = devs["data"][0]["id"]
status, body, events = sp.events_for(dev)
print(f"Event History -> {status}, {len(events)} event(s)")
for e in events[:3]:
    print("  ", json.dumps(e)[:400])

# Use any epoch-ms-looking value in the first event's attributes; else fall back to recent times.
ts = None
if events:
    for v in (events[0].get("attributes") or {}).values():
        if isinstance(v, (int, float)) and v > 1e12:
            ts = int(v)
            break
now_ms = int(time.time() * 1000)
candidates = ([ts] if ts else []) + [now_ms - d for d in (5000, 30000, 60000, 120000)]
for t in candidates:
    s, resp = sp.call("POST", f"/v1/devices/{dev}/media/image/download", {"type": "at_timestamp", "timestamp": t})
    print(f"snapshot at {t} -> {s}: {sp.short(resp)[:200]}")
    if s == 200:
        break

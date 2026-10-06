#!/usr/bin/env python3
"""Step 1 spike: check what the Ring Playground can do before designing the project.

Usage:
  1. Open the Ring Playground and click to generate a token (valid 30 min).
  2. export RING_TOKEN='<token>'
  3. python3 spike_playground.py
     The script pauses once and asks you to trigger a Package/Vehicle/Motion
     event in the Playground UI, then checks whether it shows up via the API.

Optional env: RING_BASE_URL (default https://api.amazonvision.com),
RING_DEVICE_ID (default: first device found). Standard library only.
"""
import base64, json, os, sys, time, urllib.error, urllib.request

BASE = os.environ.get("RING_BASE_URL", "https://api.amazonvision.com").rstrip("/")
TOKEN = os.environ.get("RING_TOKEN")
results = {}


def call(method, path, body=None):
    """Call the Ring API; return (status, parsed JSON or text). Never raises on HTTP errors."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.api+json, application/json",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw, status, ctype = r.read(), r.status, r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, status, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
    if "json" in ctype:
        try:
            return status, json.loads(raw)
        except ValueError:
            pass
    if ctype.startswith(("image/", "video/")):
        return status, f"<{ctype}, {len(raw)} bytes>"
    return status, raw[:300].decode(errors="replace")


def record(n, question, verdict, detail):
    results[n] = {"question": question, "verdict": verdict, "detail": detail}
    print(f"\n[{n}] {question}\n    {verdict}: {detail}")


def short(obj):
    return json.dumps(obj)[:300] if not isinstance(obj, str) else obj[:300]


def token_lifetime():
    # Playground tokens may be JWTs; read the exp claim without verifying the signature.
    try:
        payload = TOKEN.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return f"JWT expires in {int((claims['exp'] - time.time()) / 60)} min"
    except Exception:
        return "token is not a readable JWT; lifetime unknown"


def events_for(device_id):
    status, body = call("GET", f"/v1/history/devices/{device_id}/events")
    items = body.get("data", []) if isinstance(body, dict) else []
    return status, body, items


def main():
    if not TOKEN:
        sys.exit("Set RING_TOKEN first (Playground one-click token).")
    print(f"Base URL: {BASE}")

    # Q1: token lifetime and refresh path.
    record(1, "Token: only the 30-min one-click token, or can it refresh?", "CHECK",
           f"{token_lifetime()}. The Playground gives no refresh token, so a long-running "
           "poller needs a real app (OAuth refresh_token grant at oauth.ring.com).")

    # Q6 first: do the APIs work with this account, and is there a subscription?
    s_me, me = call("GET", "/v1/users/me")
    s_sub, subs = call("GET", "/v1/accounts/me/subscriptions")
    record(6, "Works with no Ring subscription?",
           "PASS" if s_me == 200 else "FAIL",
           f"/users/me -> {s_me}; /subscriptions -> {s_sub}: {short(subs)}")

    s_dev, devs = call("GET", "/v1/devices?include=status,capabilities")
    dev_list = devs.get("data", []) if isinstance(devs, dict) else []
    print(f"\nDevices -> {s_dev}, found {len(dev_list)}: "
          + ", ".join(f"{d.get('id')} ({d.get('type')})" for d in dev_list[:5]))
    device_id = os.environ.get("RING_DEVICE_ID") or (dev_list[0]["id"] if dev_list else None)
    if not device_id:
        record(3, "Simulated events appear in Event History?", "FAIL", f"no devices: {short(devs)}")
        return finish()

    # Q2 + Q3: there is no documented trigger endpoint, so trigger in the UI and diff the history.
    _, _, before = events_for(device_id)
    before_ids = {e.get("id") for e in before}
    record(2, "Trigger events by API?", "FAIL (docs)",
           "Ring API docs list no trigger/simulate endpoint. Testing the UI trigger instead.")
    print(f"\nExisting events on {device_id}: {len(before)}")
    try:
        input(f">>> In the Playground, trigger a Package/Vehicle/Motion event on device "
              f"{device_id}, then press Enter...")
    except EOFError:
        print("(no terminal input; polling for new events anyway)")
    new = []
    for _ in range(6):  # poll up to ~60s
        status, body, after = events_for(device_id)
        new = [e for e in after if e.get("id") not in before_ids]
        if new:
            break
        time.sleep(10)
    if new:
        record(3, "Simulated events appear in Event History?", "PASS",
               f"{len(new)} new event(s); first: {short(new[0])}")
    else:
        record(3, "Simulated events appear in Event History?", "FAIL",
               f"no new events after 60s; last response {status}: {short(body)}")

    # Q4: snapshot download. Body found by probing: type at_timestamp + epoch ms.
    # latest_in_range (start_timestamp/end_timestamp) is rejected if the range starts before the token was issued.
    now_ms = int(time.time() * 1000)
    tries = [{"type": "at_timestamp", "timestamp": now_ms - 5000},
             {"type": "latest_in_range", "start_timestamp": now_ms - 120000, "end_timestamp": now_ms}]
    outcome = []
    for body in tries:
        s_img, img = call("POST", f"/v1/devices/{device_id}/media/image/download", body)
        outcome.append(f"body={json.dumps(body)} -> {s_img}: {short(img)}")
        if s_img in (200, 201, 202):
            break
    ok = any(" -> 20" in o for o in outcome)
    record(4, "Snapshot/clip for an event (for Bedrock)?", "PASS" if ok else "UNKNOWN",
           " | ".join(outcome) + ("" if ok else "  (read the error to fix the body shape)"))

    # Q5: webhooks need a public HTTPS endpoint, so this is a manual check.
    record(5, "Does the Playground send webhooks?", "MANUAL",
           "Look for a webhook URL setting in the Playground. If none, plan on polling Event History.")
    finish()


def finish():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spike_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved {out}")


if __name__ == "__main__":
    main()

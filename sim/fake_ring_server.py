#!/usr/bin/env python3
"""Local fake of the Ring Partner API, driven by a scenario file, with optional chaos (monkey testing).

Matches what the real API did on 2026-10-05: JSON:API shapes, the snapshot body format and its error codes.
motion_detected / button_press and their webhooks follow the Ring docs' event list.

Webhooks use Ring's documented shape (data.type + attributes.source/timestamp/sub_type + meta.request_id)
and are signed like Ring's: X-Signature: sha256=<HMAC-SHA256 of the raw body>. Chaos also re-delivers the
same request and sends forged (wrongly signed) webhooks, which the app must refuse.

Run:  python3 sim/fake_ring_server.py sim/scenarios/night_visit.json --speed 60 --chaos 0.2 \
          --webhook http://127.0.0.1:8080/webhook
Then: RING_WEBHOOK_SECRET=sim-secret RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token python3 app.py
"""
import argparse, json, os, random, sys, threading, time, urllib.error, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # run as a script from anywhere
from webhooks import sign  # noqa: E402

WEBHOOK_TYPES = {"motion_detected", "button_press"}


def error(status, code, detail):
    return status, {"errors": [{"status": str(status), "code": code, "detail": detail}]}


class FakeRing:
    def __init__(self, scenario_path, speed=1.0, chaos=0.0, token="sim-token", token_ttl=1800, seed=None,
                 redirect_media=False):
        with open(scenario_path) as f:
            data = json.load(f)
        here = os.path.dirname(os.path.abspath(scenario_path))
        tz = ZoneInfo(data.get("timezone", "UTC"))
        now = datetime.now(tz)
        base = now
        if data.get("start_at"):  # scenario clock starts at this local time today
            h, m = map(int, data["start_at"].split(":"))
            base = now.replace(hour=h, minute=m, second=0, microsecond=0)
        self.base_ms = int(base.timestamp() * 1000)
        self.devices = data["devices"]
        self.events = []
        for i, e in enumerate(sorted(data["events"], key=lambda e: e["at_s"])):
            start = self.base_ms + int(e["at_s"] * 1000)
            image = os.path.normpath(os.path.join(here, e["image"])) if e.get("image") else None
            self.events.append({**e, "id": f"sim.event.{i}", "request_id": f"sim-req-{i}", "start": start,
                                "end": start + int(e.get("duration_s", 20) * 1000), "image_path": image})
        self.speed, self.chaos, self.token = speed, chaos, token
        self.redirect_media = redirect_media  # answer snapshots with 303 -> pre-signed-style URL
        self.started, self.token_ttl = time.time(), token_ttl
        self.rand = random.Random(seed)
        self.webhooks_sent = set()
        self.injected_failures = 0

    # --- scenario clock -------------------------------------------------
    def released(self):
        """Events whose scenario time has passed (scenario seconds = real seconds x speed)."""
        elapsed = (time.time() - self.started) * self.speed
        return [e for e in self.events if e["at_s"] <= elapsed]

    @staticmethod
    def history_json(e):
        attrs = {"start": e["start"], "end": e["end"], "event_type": e["type"], "is_third_party_reviewed": True}
        if e.get("sub_type"):
            attrs["sub_type"] = e["sub_type"]
        return {"type": "history-events", "id": e["id"], "attributes": attrs,
                "relationships": {"cv_detections": {"data": []},
                                  "source": {"data": {"type": "devices", "id": e["device"]}}}}

    @staticmethod
    def webhook_payload(e):
        """Ring's documented webhook shape. request_id is stable per event, so a re-delivery is a true duplicate."""
        attrs = {"source": e["device"], "source_type": "devices", "timestamp": e["start"], "component_ids": ["0"]}
        if e.get("sub_type"):
            attrs["sub_type"] = e["sub_type"]
        sent = datetime.fromtimestamp(e["start"] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"data": {"type": e["type"], "attributes": attrs},
                "meta": {"version": "1.0", "time": sent, "request_id": e["request_id"], "account_id": "sim.account"}}

    def deliveries(self, e, secret):
        """What gets POSTed for one event: (body, X-Signature, forged). Chaos adds Ring-style re-deliveries
        of the same body and forged webhooks signed with the wrong key."""
        body = json.dumps(self.webhook_payload(e)).encode()
        out = [(body, sign(secret, body), False)]
        if self.chaos and self.rand.random() < self.chaos:
            out.append((body, sign(secret, body), False))
        if self.chaos and self.rand.random() < self.chaos:
            forged = self.webhook_payload(e)
            forged["meta"] = {**forged["meta"], "request_id": f"forged-{self.rand.randrange(10**9)}"}
            forged_body = json.dumps(forged).encode()
            out.append((forged_body, sign("not-the-ring-key", forged_body), True))
        return out

    # --- request handling (returns status, body, content_type) ----------
    def handle(self, method, path, headers, body):
        if method == "GET" and path.startswith("/media/"):  # stands in for a pre-signed media URL
            if headers.get("Authorization"):
                return (*error(400, "INVALID_ARGUMENT", "Only one auth mechanism allowed"), None)
            hit = [e for e in self.events if path == f"/media/{e['id']}.jpg" and e["image_path"]]
            if not hit:
                return (*error(404, "NOT_FOUND", path), None)
            with open(hit[0]["image_path"], "rb") as f:
                return 200, f.read(), "image/jpeg"
        if headers.get("Authorization") != f"Bearer {self.token}":
            return (*error(401, "UNAUTHORIZED", "invalid token"), None)
        if time.time() - self.started > self.token_ttl:
            return (*error(401, "TOKEN_EXPIRED", "token expired"), None)
        if self.chaos and self.rand.random() < self.chaos:
            roll = self.rand.random()
            if roll < 0.4:
                self.injected_failures += 1
                return (*error(500, "INTERNAL_ERROR", "chaos: injected failure"), None)
            if roll < 0.7:
                time.sleep(self.rand.uniform(0.5, 2.0))  # chaos: slow reply, then answer normally
        parts = path.split("?")[0].strip("/").split("/")

        if method == "GET" and parts == ["v1", "users", "me"]:
            return 200, {"data": {"type": "users", "id": "sim.account"}}, None
        if method == "GET" and parts == ["v1", "devices"]:
            return 200, {"data": [{"type": "devices", "id": d["id"], "attributes": {"name": d["name"]}}
                                  for d in self.devices]}, None
        if method == "GET" and parts[:3] == ["v1", "history", "devices"] and parts[4:] == ["events"]:
            items = [self.history_json(e) for e in reversed(self.released()) if e["device"] == parts[3]]
            if self.chaos and items and self.rand.random() < self.chaos:  # chaos: duplicates, wrong order
                items.append(self.rand.choice(items))
                self.rand.shuffle(items)
            return 200, {"data": items}, None
        if method == "POST" and parts[:2] == ["v1", "devices"] and parts[3:] == ["media", "image", "download"]:
            return self.snapshot(parts[2], body)
        return (*error(404, "NOT_FOUND", f"no route {method} {path}"), None)

    def snapshot(self, device_id, body):
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            return (*error(403, "REQUEST_FORBIDDEN", "Cannot authorize: invalid request body"), None)
        kind = req.get("type")
        if not kind:
            return (*error(400, "INVALID_PAYLOAD", "type is required"), None)
        if kind == "at_timestamp" and isinstance(req.get("timestamp"), int):
            lo, hi = req["timestamp"] - 5000, req["timestamp"] + 5000
        elif kind == "latest_in_range" and isinstance(req.get("start_timestamp"), int) \
                and isinstance(req.get("end_timestamp"), int):
            lo, hi = req["start_timestamp"], req["end_timestamp"]
        elif kind in ("at_timestamp", "latest_in_range"):
            return (*error(403, "REQUEST_FORBIDDEN", "Cannot authorize: missing required timestamp fields in request body"), None)
        else:
            return (*error(400, "INVALID_PAYLOAD", "type not supported, valid values: at_timestamp, latest_in_range"), None)
        hits = [e for e in self.released() if e["device"] == device_id and e["image_path"]
                and e["start"] <= hi and e["end"] >= lo]
        if not hits:
            return (*error(416, "MEDIA_NOT_FOUND", f"No media found in time range {lo}-{hi}"), None)
        if self.redirect_media:
            return 303, {"location": f"/media/{hits[-1]['id']}.jpg"}, "redirect"
        with open(hits[-1]["image_path"], "rb") as f:
            return 200, f.read(), "image/jpeg"

    # --- webhooks --------------------------------------------------------
    def due_webhooks(self):
        due = [e for e in self.released() if e["type"] in WEBHOOK_TYPES and e["id"] not in self.webhooks_sent]
        self.webhooks_sent.update(e["id"] for e in due)
        return due

    def push_webhooks(self, url, stop, secret):
        while not stop.is_set():
            for e in self.due_webhooks():
                for body, signature, forged in self.deliveries(e, secret):
                    req = urllib.request.Request(url, data=body, method="POST", headers={
                        "Content-Type": "application/json", "X-Signature": signature})
                    try:
                        urllib.request.urlopen(req, timeout=5).read()
                    except urllib.error.HTTPError as ex:
                        print(f"{'forged webhook refused' if forged else 'webhook rejected'}: HTTP {ex.code}")
                    except Exception as ex:
                        print(f"webhook to {url} failed: {ex}")
            stop.wait(0.5)


def make_server(fake, port=9000):
    class Handler(BaseHTTPRequestHandler):
        def _run(self, method):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if method == "POST" else None
            status, payload, ctype = fake.handle(method, self.path, self.headers, body)
            if ctype == "redirect":
                self.send_response(status)
                self.send_header("Location", payload["location"])
                self.send_header("Content-Length", "0")
                return self.end_headers()
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype or "application/vnd.api+json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._run("GET")

        def do_POST(self):
            self._run("POST")

        def log_message(self, fmt, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("scenario")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--speed", type=float, default=60, help="scenario seconds per real second")
    p.add_argument("--chaos", type=float, default=0.0, help="0..1 chance per request of an injected fault")
    p.add_argument("--token", default="sim-token")
    p.add_argument("--token-ttl", type=int, default=1800, help="real seconds until every call returns 401")
    p.add_argument("--webhook", help="URL to POST motion_detected / button_press webhooks to")
    p.add_argument("--webhook-secret", default=os.environ.get("RING_WEBHOOK_SECRET") or "sim-secret",
                   help="HMAC key used to sign webhooks (the app's RING_WEBHOOK_SECRET)")
    p.add_argument("--seed", type=int)
    p.add_argument("--redirect-media", action="store_true", help="answer snapshots with a 303 redirect")
    a = p.parse_args()
    fake = FakeRing(a.scenario, a.speed, a.chaos, a.token, a.token_ttl, a.seed, a.redirect_media)
    stop = threading.Event()
    if a.webhook:
        threading.Thread(target=fake.push_webhooks, args=(a.webhook, stop, a.webhook_secret), daemon=True).start()
    last = fake.events[-1]["at_s"] / a.speed if fake.events else 0
    print(f"Fake Ring API on http://127.0.0.1:{a.port} | token '{a.token}' | {len(fake.events)} events over "
          f"{last:.0f}s real time | chaos {a.chaos} | webhooks {'-> ' + a.webhook if a.webhook else 'off'}")
    try:
        make_server(fake, a.port).serve_forever()
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()

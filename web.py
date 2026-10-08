"""Local web dashboard: the home's lights (virtual devices) and the last 24 hours of door events."""
import json, os, re, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

from bridge import describe_action
from decider import DEFAULT_MIN_P
from live import LiveError, MAX_OFFER_BYTES
from webhooks import parse_webhook, verify_signature

MAX_WEBHOOK_BYTES = 65_536  # Ring webhooks are small; refuse anything bigger before reading it
REQUEST_TIMEOUT = 10  # seconds a client may stall on the socket before its connection is dropped
CHIP_SURE = 0.8  # older rows (one yes/no Decider question per automation): below this was "not sure"
CHIP_NO = 0.5  # older rows: with nothing fired, below this was a confident "no", a skipped false alarm

_ICON_WORDS = (("package", r"packages?|parcels?|box(es)?"), ("vehicle", r"cars?|vehicles?|trucks?|vans?"),
               ("animal", r"cats?|dogs?|animals?|birds?"), ("person", r"person|people|man|woman|courier"))


def summarize(e, actions, min_p=DEFAULT_MIN_P):
    """What the dashboard says about one door event: a headline, what the house did, a chip saying who
    decided (a rule, or Decider's answer and how sure it was) and an icon key. Text is returned raw; the page escapes."""
    kinds = [k for k in (e.get("kinds") or "").split(",") if k]
    description = (e.get("description") or "").strip()
    text = description.lower()
    ran = [a for a in actions if not a.get("skipped")]  # includes failed commands, so the page can show FAILED
    fired = [a for a in ran if a.get("ok")]
    if "doorbell" in kinds:
        headline, icon = "Doorbell pressed", "doorbell"
    elif description:
        headline = description
        icon = next((key for key, words in _ICON_WORDS if re.search(rf"\b(?:{words})\b", text)), "motion")
    elif e.get("event_type") == "on_demand":
        headline, icon = "Live view opened", "live"
    elif "person" in kinds:
        headline, icon = "Person at the door", "person"
    else:
        headline, icon = "Motion", "motion"
    history = e.get("status") == "history"
    if ran:
        detail = "; ".join(a.get("text") or "" for a in ran)
    elif history:
        detail = "recorded at startup"
    elif kinds or actions:
        detail = "nothing done"
    else:
        detail = ""
    ps = [a["decision_p"] for a in actions if a.get("decision_p") is not None]
    if e.get("decision"):
        label, p = e["decision"], e.get("decision_p") or 0.0
        if p < min_p:
            kind = "unsure"  # the "Not sure" pulse fired
        elif not ran and label in ("nothing", "animal"):
            kind = "skip"
        else:
            kind = "ai"
        chip = {"kind": kind, "text": f"Decider: {label} {p:.2f}"}
        if kind == "skip" and detail == "nothing done":
            detail = "false alarm skipped"
    elif ps:  # older rows: Decider's p per automation
        p = max(ps)
        if fired:
            kind = "ai" if p >= CHIP_SURE else "unsure"
        else:
            kind = "skip" if p < CHIP_NO else "unsure"
        chip = {"kind": kind, "text": f"Decider {p:.2f}"}
        if kind == "skip" and detail == "nothing done":
            detail = "false alarm skipped"
    elif "doorbell" in kinds:
        chip = {"kind": "rule", "text": "rule · no AI"}
    elif fired:
        chip = {"kind": "rule", "text": "rule"}
    elif history:
        chip = {"kind": "skip", "text": "before start"}
    else:
        chip = {"kind": "skip", "text": "no action"}
    return {"headline": headline, "detail": detail, "chip": chip, "icon": icon}

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8") as _f:
    PAGE = _f.read()  # the dashboard: plain HTML, CSS and JavaScript, edited as a file


def make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None, min_p=DEFAULT_MIN_P, live=None):
    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

        def do_POST(self):
            if self.path.startswith("/api/live/"):
                return self._live_post(self.path[len("/api/live/"):].split("/"))
            if self.path != "/webhook" or not enqueue:
                return self._send(404, "text/plain", "not found")
            if not webhook_secret:
                return self._refuse(503, "webhook secret not configured")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._refuse(415, "expected application/json")
            length = self._length()
            if length < 0:
                return self._refuse(400, "bad Content-Length")
            if length > MAX_WEBHOOK_BYTES:
                return self._refuse(413, "webhook too large")
            body = self.rfile.read(length)
            if not verify_signature(webhook_secret, body, self.headers.get("X-Signature")):
                return self._refuse(401, "bad signature")
            if log_bodies:  # opt-in (WEBHOOK_LOG=1): capture Ring's real payload; only verified bodies are printed
                print(f"webhook body: {body.decode('utf-8', 'replace')}")
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("payload is not a JSON object")
                ev = parse_webhook(payload)
            except ValueError as e:
                return self._refuse(400, f"bad webhook: {e}")
            if ev is not None:  # door event: the worker does the rest; Ring needs a 200 within 5 s
                enqueue(ev, None)
            self._send(200, "text/plain", "ok")

        def _live_post(self, parts):
            if len(parts) == 3 and parts[0] == "session" and parts[2] == "stop":  # sendBeacon when the page closes
                if live:
                    live.stop(parts[1])
                return self._send(204, "text/plain", "")
            if len(parts) != 1 or not parts[0]:
                return self._send(404, "text/plain", "not found")
            if not live:
                return self._json(503, {"error": "Live view needs a Ring token"})
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/sdp":  # also blocks cross-site form posts: this type needs CORS, never granted
                return self._json(415, {"error": "expected application/sdp"})
            length = self._length()
            if length < 0:
                return self._json(400, {"error": "bad Content-Length"})
            if length > MAX_OFFER_BYTES:
                return self._json(413, {"error": "video offer too large"})
            try:
                out = live.start(unquote(parts[0]), self.rfile.read(length).decode(errors="replace"))
            except LiveError as e:
                return self._json(e.status, {"error": e.message})
            self._json(201, out)

        def do_DELETE(self):
            prefix = "/api/live/session/"
            session = self.path[len(prefix):] if self.path.startswith(prefix) else ""
            if not session or "/" in session:
                return self._send(404, "text/plain", "not found")
            if live:
                live.stop(session)
            self._send(204, "text/plain", "")

        def do_GET(self):
            if self.path == "/api/status":
                return self._send(200, "application/json", json.dumps(status() if status else {}))
            if self.path == "/api/live":
                return self._json(200, {"available": bool(live), "cameras": live.cameras() if live else []})
            if self.path == "/api/devices":
                return self._send(200, "application/json", json.dumps(devices.snapshot() if devices else []))
            items = store.timeline(int(time.time() * 1000) - 86_400_000)
            acts_by_event = store.actions_for([e["id"] for e in items])
            if self.path == "/api/events":
                names = {d["id"]: d["name"] for d in (devices.snapshot() if devices else [])}
                out = []
                for e in items:
                    acts = [{**a, "text": describe_action({**a, "device_name": names.get(a["device"], a["device"])})}
                            for a in acts_by_event.get(e["id"], [])]
                    out.append({**e, "actions": acts, "summary": summarize(e, acts, min_p)})
                self._send(200, "application/json", json.dumps(out))
            elif self.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE)
            else:
                self._send(404, "text/plain", "not found")

        def _refuse(self, code, reason):
            print(f"webhook refused {code}: {reason}")  # never the body: it is not verified yet
            self._send(code, "text/plain", reason)

        def _json(self, code, obj):
            self._send(code, "application/json", json.dumps(obj))

        def _length(self):
            try:
                return int(self.headers.get("Content-Length") or "")
            except ValueError:
                return -1

        def _send(self, code, ctype, text):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *args):  # keep the poller output readable
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None, min_p=DEFAULT_MIN_P, live=None):
    server = make_server(store, tz, port, enqueue, devices, webhook_secret, log_bodies, status, min_p, live)
    print(f"Dashboard: http://127.0.0.1:{port}")
    server.serve_forever()

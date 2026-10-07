"""Local web dashboard: the home's lights (virtual devices) and the last 24 hours of door events."""
import html, json, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import describe_action
from webhooks import parse_webhook, verify_signature

MAX_WEBHOOK_BYTES = 65_536  # Ring webhooks are small; refuse anything bigger before reading it
REQUEST_TIMEOUT = 10  # seconds a client may stall on the socket before its connection is dropped

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PorchLink</title>
<style>
body{{font:15px system-ui;margin:24px;max-width:860px;background:#fafafa;color:#1d1d1f}}
table{{border-collapse:collapse;width:100%;background:#fff}} td,th{{padding:8px;border-bottom:1px solid #ddd;text-align:left;vertical-align:top}}
.alert{{color:#b3261e;font-weight:600}} small{{color:#555}} .act{{color:#0b6b3a}}
#devices{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin:12px 0 24px}}
.dev{{background:#fff;border:1px solid #ddd;border-radius:12px;padding:14px}}
.bulb{{font-size:38px;filter:grayscale(1) opacity(.35)}} .on .bulb{{filter:none;text-shadow:0 0 18px #ffd60a}}
.flash .bulb{{animation:f .5s steps(1) infinite}} @keyframes f{{50%{{filter:grayscale(1) opacity(.35)}}}}
</style></head><body><h1>PorchLink</h1>
{devices_block}
<h2>Door events (last 24 hours)</h2>
<table><tr><th>Time</th><th>Where</th><th>Length</th><th>Alert and home actions</th></tr>{rows}</table>
<script>
async function tick(){{
  const box=document.getElementById('devices'); if(!box) return;
  const ds=await (await fetch('/api/devices')).json();
  box.innerHTML=ds.map(d=>`<div class="dev ${{d.flashing?'flash':d.on?'on':''}}"><div class="bulb">💡</div>
    <b>${{d.name}}</b><br><small>${{d.flashing?'flashing':d.on?'ON, '+Math.ceil(d.seconds_left/60)+' min left':'off'}}</small>
    ${{d.last?'<br><small>last: '+d.last.automation+'</small>':''}}</div>`).join('');
}}
tick(); setInterval(tick,1000); setTimeout(()=>location.reload(),15000);
</script></body></html>"""


def make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False):
    class Handler(BaseHTTPRequestHandler):
        timeout = REQUEST_TIMEOUT

        def do_POST(self):
            if self.path != "/webhook" or not enqueue:
                return self._send(404, "text/plain", "not found")
            if not webhook_secret:
                return self._refuse(503, "webhook secret not configured")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._refuse(415, "expected application/json")
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                length = -1
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

        def do_GET(self):
            if self.path == "/api/devices":
                return self._send(200, "application/json", json.dumps(devices.snapshot() if devices else []))
            items = store.timeline(int(time.time() * 1000) - 86_400_000)
            acts = store.actions_for([e["id"] for e in items])
            if self.path == "/api/events":
                self._send(200, "application/json", json.dumps([{**e, "actions": acts.get(e["id"], [])} for e in items]))
            elif self.path == "/":
                block = ('<h2>Home</h2><div id="devices"></div>' if devices else
                         "<p><small>Devices are on AWS IoT Core: see their state in the IoT console.</small></p>")
                rows = "".join(row(e, acts.get(e["id"], [])) for e in items) or "<tr><td colspan=4>No events yet.</td></tr>"
                self._send(200, "text/html; charset=utf-8", PAGE.format(devices_block=block, rows=rows))
            else:
                self._send(404, "text/plain", "not found")

        def _refuse(self, code, reason):
            print(f"webhook refused {code}: {reason}")  # never the body: it is not verified yet
            self._send(code, "text/plain", reason)

        def _send(self, code, ctype, text):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(text.encode())

        def log_message(self, *args):  # keep the poller output readable
            pass

    def row(e, actions):
        when = datetime.fromtimestamp(e["start_ms"] / 1000, tz).strftime("%I:%M:%S %p")
        secs = max(0, round((e["end_ms"] - e["start_ms"]) / 1000))
        kinds = [k for k in (e["kinds"] or "").split(",") if k and k != "activity"]
        how = "" if not e["kinds"] else (" (emailed)" if e["delivered"] else " (printed only)")
        label = html.escape(", ".join(kinds)) if kinds else ("activity" if e["kinds"] else "")
        alert = f'<span class="alert">{label}</span>{how}' if kinds else f"{label}{how}"
        seen = f"<br><small>{html.escape(e['description'])}</small>" if e.get("description") else ""
        names = {d["id"]: d["name"] for d in (devices.snapshot() if devices else [])}
        acts = "".join(
            f'<br><small class="act">→ {html.escape(describe_action({**a, "device_name": names.get(a["device"], a["device"])}))}</small>'
            for a in actions)
        return (f"<tr><td>{when}</td><td>{html.escape(e['device_name'] or '')}</td>"
                f"<td>{secs} s</td><td>{alert}{seen}{acts}</td></tr>")

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False):
    server = make_server(store, tz, port, enqueue, devices, webhook_secret, log_bodies)
    print(f"Dashboard: http://127.0.0.1:{port}")
    server.serve_forever()

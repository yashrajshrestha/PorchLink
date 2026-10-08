"""Local web dashboard: the home's lights (virtual devices) and the last 24 hours of door events."""
import json, re, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import describe_action
from webhooks import parse_webhook, verify_signature

MAX_WEBHOOK_BYTES = 65_536  # Ring webhooks are small; refuse anything bigger before reading it
REQUEST_TIMEOUT = 10  # seconds a client may stall on the socket before its connection is dropped
CHIP_SURE = 0.8  # Decider answers below this are "not sure": the house gives a gentle pulse

_ICON_WORDS = (("package", r"packages?|parcels?|box(es)?"), ("vehicle", r"cars?|vehicles?|trucks?|vans?"),
               ("animal", r"cats?|dogs?|animals?|birds?"), ("person", r"person|people|man|woman|courier"))


def summarize(e, actions):
    """What the dashboard says about one door event: a headline, what the house did, a chip saying who
    decided (a rule, or Decider and how sure it was) and an icon key. Text is returned raw; the page escapes."""
    kinds = [k for k in (e.get("kinds") or "").split(",") if k]
    description = (e.get("description") or "").strip()
    text = description.lower()
    fired = [a for a in actions if a.get("ok") and not a.get("skipped")]
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
    if fired:
        detail = "; ".join(a.get("text") or "" for a in fired)
    elif history:
        detail = "recorded at startup"
    elif kinds or actions:
        detail = "nothing done"
    else:
        detail = ""
    ps = [a["decision_p"] for a in actions if a.get("decision_p") is not None]
    if ps:
        p = max(ps)
        chip = {"kind": "unsure" if p < CHIP_SURE else ("ai" if fired else "skip"), "text": f"Decider {p:.2f}"}
    elif "doorbell" in kinds:
        chip = {"kind": "rule", "text": "rule · no AI"}
    elif fired:
        chip = {"kind": "rule", "text": "rule"}
    elif history:
        chip = {"kind": "skip", "text": "before start"}
    else:
        chip = {"kind": "skip", "text": "no action"}
    return {"headline": headline, "detail": detail, "chip": chip, "icon": icon}

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>PorchLink</title>
<style>
:root{--bg:#0e1116;--card:#171b22;--line:#262c36;--text:#e8e6e1;--muted:#9aa3b2;--amber:#ffc24d;--glow:255,176,32}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 24px 48px}
.top{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:22px}
.brand{font-size:26px;font-weight:800;letter-spacing:-.01em}.brand span{color:var(--amber)}
.status{display:flex;gap:10px;align-items:center;font-size:14px;color:var(--muted)}
.dot{width:10px;height:10px;border-radius:50%;background:#6b7280}
.dot.ok{background:#34d399;box-shadow:0 0 8px #34d399}.dot.bad{background:#f87171;box-shadow:0 0 8px #f87171}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:20px;min-height:168px;transition:background .3s,border-color .3s}
.bulb{width:64px;height:64px;border-radius:50%;background:#232833;border:2px solid #2f3643;margin-bottom:16px}
.room{font-size:14px;color:var(--muted);font-weight:600}
.state{font-size:24px;font-weight:800;margin-top:2px}
.why{font-size:13px;color:var(--muted);margin-top:4px;min-height:1.2em}
.tile.on{border-color:#5a4416;background:linear-gradient(180deg,#241d0f,var(--card) 70%)}
.tile.on .bulb,.tile.flash .bulb{background:radial-gradient(circle at 40% 35%,#fff4c2,#ffbf3a 50%,#e8870b);border-color:#ffb020}
.tile.on .bulb{animation:breathe 2.4s ease-in-out infinite}
.tile.flash{border-color:#6b4f12;background:linear-gradient(180deg,#2a210f,var(--card) 70%)}
.tile.flash .bulb{animation:flash .6s steps(1) infinite}
.tile.pulse .bulb{background:radial-gradient(circle at 40% 35%,#fff8de,#ffd98a 55%,#d8a33c);border-color:#c99a3a;animation:pulse 2.6s ease-in-out infinite}
.tile.on .state,.tile.flash .state{color:var(--amber)}.tile.pulse .state{color:#f3d58c}
@keyframes breathe{0%,100%{box-shadow:0 0 22px 6px rgba(var(--glow),.5)}50%{box-shadow:0 0 44px 14px rgba(var(--glow),.9)}}
@keyframes flash{0%,49%{opacity:1}50%,100%{opacity:.2}}
@keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(255,214,120,0);opacity:.55}50%{box-shadow:0 0 28px 8px rgba(255,214,120,.6);opacity:1}}
@media (prefers-reduced-motion:reduce){.tile .bulb{animation:none!important}.tile.flash .bulb{box-shadow:0 0 0 6px rgba(var(--glow),.9)}}
.legend{display:flex;gap:20px;flex-wrap:wrap;font-size:13px;color:var(--muted);margin-top:18px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:7px;vertical-align:-1px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:30px 0 12px;font-weight:700}
.ev{display:flex;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:16px;padding:12px 14px;margin-bottom:9px}
.icon{width:52px;height:52px;border-radius:12px;flex:none;display:grid;place-items:center;font-size:26px;background:#20252e}
.ev b{font-size:16px}.meta{font-size:13px;color:var(--muted)}
.chip{margin-left:auto;font-size:12px;font-weight:700;border-radius:999px;padding:4px 11px;white-space:nowrap}
.chip.rule{background:#3a2a0b;color:#ffc24d}.chip.ai{background:#14233d;color:#8db4ff}
.chip.skip{background:#1f242d;color:#9aa3b2}.chip.unsure{background:#33290f;color:#f3d58c}
.empty,.note{color:var(--muted);background:var(--card);border:1px dashed var(--line);border-radius:16px;padding:18px;text-align:center}
@media (max-width:560px){main{padding:18px 14px}.ev{flex-wrap:wrap}.chip{margin-left:66px}}
</style></head><body><main>
<div class="top"><div class="brand">Porch<span>Link</span></div>
<div class="status"><span class="dot" id="dot"></span><span id="status-text">Connecting…</span></div></div>
<div class="grid" id="tiles"></div>
<div class="legend" id="legend"><span><i style="background:#ffbf3a"></i>On (steady glow)</span>
<span><i style="background:#ffbf3a;opacity:.5"></i>Flashing = doorbell or alert</span>
<span><i style="background:#ffd98a"></i>Gentle pulse = AI not sure</span><span><i style="background:#2f3643"></i>Off</span></div>
<h2>Door events · last 24 hours</h2>
<div id="events"><div class="empty">No events yet.</div></div>
</main><script>
const ICON={doorbell:"🔔",package:"📦",vehicle:"🚗",person:"🧍",animal:"🐾",motion:"👣",live:"📹"};
const esc=s=>String(s==null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const clock=ms=>new Date(ms).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"});
const tilesEl=document.getElementById("tiles"),eventsEl=document.getElementById("events");
let lastEvents="";
function view(d){
  const label=d.last&&d.last.label, s=d.seconds_left;
  if(d.flashing)return{cls:"flash",state:label||"Alert!",why:"flashing · just now"};
  if(d.pulsing)return{cls:"pulse",state:label||"Not sure",why:"gentle pulse · check camera"};
  if(d.on)return{cls:"on",state:label||"On",why:"amber · "+(s>=60?Math.ceil(s/60)+" min left":s+" s left")};
  return{cls:"",state:"Off",why:""};
}
function setText(el,sel,text){const n=el.querySelector(sel);if(n.textContent!==text)n.textContent=text;}
function renderTiles(ds){
  if(!ds.length){tilesEl.innerHTML='<div class="note">Lights are on AWS IoT Core: see their state in the IoT console</div>';return;}
  const note=tilesEl.querySelector(".note");if(note)note.remove();
  for(const d of ds){
    let el=tilesEl.querySelector('[data-id="'+CSS.escape(d.id)+'"]');
    if(!el){el=document.createElement("div");el.dataset.id=d.id;el.dataset.testid="tile-"+d.id;
      el.innerHTML='<div class="bulb"></div><div class="room"></div><div class="state"></div><div class="why"></div>';tilesEl.appendChild(el);}
    const v=view(d),cls=("tile "+v.cls).trim();
    if(el.className!==cls)el.className=cls;  // only the class changes, so the bulb's animation keeps running
    setText(el,".room",d.name);setText(el,".state",v.state);setText(el,".why",v.why);
  }
}
function eventRow(e){
  const s=e.summary,meta=[e.device_name||"",clock(e.start_ms),s.detail].filter(Boolean).map(esc).join(" · ");
  return '<div class="ev" data-testid="event"><div class="icon" aria-hidden="true">'+(ICON[s.icon]||ICON.motion)+
    '</div><div><b>'+esc(s.headline)+'</b><div class="meta">'+meta+'</div></div><span class="chip '+esc(s.chip.kind)+'">'+esc(s.chip.text)+'</span></div>';
}
function renderStatus(st){
  document.getElementById("dot").className="dot "+(st.ring_ok===false?"bad":st.ring_ok?"ok":"");
  const ring=st.ring_ok===false?"Ring not reachable":st.ring_ok?"Ring connected":(st.mode||"Starting");
  const when=st.day&&st.time?st.day+" "+st.time:"",phase=st.night==null?"":(st.night?"Night":"Day");
  document.getElementById("status-text").textContent=[ring,when,phase].filter(Boolean).join(" · ");
}
async function tick(){
  try{
    const [ds,evs,st]=await Promise.all(["/api/devices","/api/events","/api/status"].map(u=>fetch(u).then(r=>r.json())));
    renderTiles(ds);document.getElementById("legend").hidden=!ds.length;
    const key=JSON.stringify(evs);
    if(key!==lastEvents){lastEvents=key;eventsEl.innerHTML=evs.length?evs.map(eventRow).join(""):'<div class="empty">No events yet.</div>';}
    renderStatus(st);
  }catch(err){
    document.getElementById("dot").className="dot bad";
    document.getElementById("status-text").textContent="Dashboard lost connection to PorchLink";
  }
}
tick();setInterval(tick,1000);
</script></body></html>"""


def make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None):
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
            if self.path == "/api/status":
                return self._send(200, "application/json", json.dumps(status() if status else {}))
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
                    out.append({**e, "actions": acts, "summary": summarize(e, acts)})
                self._send(200, "application/json", json.dumps(out))
            elif self.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE)
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

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None):
    server = make_server(store, tz, port, enqueue, devices, webhook_secret, log_bodies, status)
    print(f"Dashboard: http://127.0.0.1:{port}")
    server.serve_forever()

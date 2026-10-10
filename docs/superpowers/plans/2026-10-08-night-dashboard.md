# Night-Mode Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace PorchLink's plain dashboard with the approved night-mode design: amber-glowing light tiles (on, flashing, gentle pulse, off), a live status header, and plain-English door events with "who decided" chips.

**Architecture:** The server computes what the page shows. A pure `web.summarize()` turns each event into a headline, detail, chip and icon, and a new `/api/status` endpoint is backed by `app.make_status()`. The page itself is one static HTML/CSS/JS document that polls `/api/devices`, `/api/events` and `/api/status` every second. It updates the tiles in place, so CSS animations never restart. The bridge gains a `pulse` action and automation `label`s.

**Tech Stack:** Python 3.10 standard library (`http.server`, `re`, `unittest`). Vanilla HTML/CSS/JS, with no frameworks or CDNs.

**Spec:** `docs/superpowers/specs/2026-10-08-night-dashboard-design.md`

## Global Constraints

- **Workspace:** repo `/Users/yashshrestha/PorchLink`, branch `feat/night-dashboard`.
- **Libraries:** standard library only. The page uses no external scripts, fonts or images; everything is inline.
- **Full suite**, run from the repo root:
  - command: `python3 -m unittest test_porchlink test_sim test_vision test_bridge test_decider test_webhooks test_worker test_web test_dashboard`
  - baseline before this plan: 102 OK, without `test_dashboard`.
- **Colours** (exact):
  - background `#0e1116`, card `#171b22`, line `#262c36`, text `#e8e6e1`, muted `#9aa3b2`, amber `#ffc24d`;
  - lit bulb gradient `#fff4c2 → #ffbf3a → #e8870b`, pulse bulb `#fff8de → #ffd98a → #d8a33c`.
  - Chips:

    | Chip | Background | Text |
    |---|---|---|
    | `rule` | `#3a2a0b` | `#ffc24d` |
    | `ai` | `#14233d` | `#8db4ff` |
    | `skip` | `#1f242d` | `#9aa3b2` |
    | `unsure` | `#33290f` | `#f3d58c` |
- **Copy** (exact): "PorchLink" (with "Link" in amber), "Ring connected", "Ring not reachable", "Door events · last 24 hours", "No events yet.", "Lights are on AWS IoT Core: see their state in the IoT console", "gentle pulse · check camera", "flashing · just now", "rule · no AI", "nothing done", "recorded at startup", "before start", "no action".
- **Unsure threshold:** `CHIP_SURE = 0.8`.
- **Safety:** all store and AI text is HTML-escaped in the page. Updating a tile must not reset its bulb animation.
- **Commits:** messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

### Task 1: Pulse, labels, event summaries and status API

**Files:**
- Modify: `bridge.py` (pulse action, label in `last` and in published payloads, `describe_action` for pulse)
- Modify: `automations.json` (a label on each automation)
- Modify: `web.py` (`summarize`, `/api/status`, `/api/events` gains `summary` and per-action `text`, plus the `status` parameter)
- Modify: `app.py` (`make_status`, health tracking, pass `status` to `web.serve`)
- Create: `test_dashboard.py`

**Interfaces:**
- **Produces, used by Task 2:**
  - `GET /api/devices`: each device gains `pulsing: bool`, and `last` becomes `{"automation", "label", "at"}`.
  - `GET /api/events`: each event gains `summary: {"headline", "detail", "chip": {"kind", "text"}, "icon"}`, and each action gains `text`.
  - `GET /api/status` returns `{"mode", "ring_ok", "night", "time", "day"}`.
  - `web.make_server(store, tz, port, enqueue, devices, webhook_secret, log_bodies, status)` and `web.serve(...)` with the same arguments.
  - `web.summarize(event, actions) -> dict` and `web.CHIP_SURE`.
  - `app.make_status(health, settings, tz, poll_seconds, mode) -> callable`.
- `GET /` keeps returning the old page in this task. Task 2 replaces it.

- [ ] **Step 1: Write the failing tests**

Create `test_dashboard.py`:

```python
"""Tests for the night-mode dashboard: pulse + labels, event summaries, status API. Run: python3 -m unittest test_dashboard -v"""
import json, os, threading, time, unittest, urllib.request
from zoneinfo import ZoneInfo

import app
import web
from bridge import Bridge, LocalPublisher, VirtualDevices, describe_action, load_config
from rules import Settings
from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Asia/Kathmandu")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


CONFIG = {"devices": [{"id": "lamp", "name": "Living room light", "type": "light"}],
          "automations": [{"name": "Visual doorbell", "label": "Doorbell!", "when": {"kind": ["doorbell"]},
                           "do": [{"device": "lamp", "action": "flash", "times": 3}]}]}


class PulseAndLabelTest(unittest.TestCase):
    def test_pulse_lasts_its_duration(self):
        clock = Clock()
        devices = VirtualDevices(CONFIG, clock=clock)
        devices.apply("lamp", {"action": "pulse", "duration_s": 5, "label": "Not sure"})
        lamp = devices.snapshot()[0]
        self.assertTrue(lamp["pulsing"])
        self.assertFalse(lamp["on"] or lamp["flashing"])
        self.assertEqual(lamp["last"]["label"], "Not sure")
        clock.t += 6
        self.assertFalse(devices.snapshot()[0]["pulsing"])

    def test_pulse_defaults_to_ten_seconds(self):
        clock = Clock()
        devices = VirtualDevices(CONFIG, clock=clock)
        devices.apply("lamp", {"action": "pulse"})
        clock.t += 9
        self.assertTrue(devices.snapshot()[0]["pulsing"])
        clock.t += 2
        self.assertFalse(devices.snapshot()[0]["pulsing"])

    def test_bridge_publishes_the_automation_label(self):
        devices = VirtualDevices(CONFIG)
        publisher = LocalPublisher(devices)
        Bridge(CONFIG, publisher).run({"id": "e1", "start_ms": 1_000_000}, {"doorbell"}, None, "Front Door")
        topic, payload = publisher.sent[0]
        self.assertEqual(payload["label"], "Doorbell!")
        self.assertEqual(devices.snapshot()[0]["last"]["label"], "Doorbell!")

    def test_describe_pulse(self):
        r = {"automation": "Unsure", "device": "lamp", "device_name": "Living room light",
             "command": {"action": "pulse"}, "ok": True, "skipped": False, "decision_p": None}
        self.assertEqual(describe_action(r), "Living room light gentle pulse (Unsure)")

    def test_every_shipped_automation_has_a_label(self):
        cfg = load_config(os.path.join(HERE, "automations.json"))
        self.assertTrue(all(a.get("label") for a in cfg["automations"]), [a["name"] for a in cfg["automations"]])


def action(text="Living room light flash x3 (Visual doorbell)", ok=True, skipped=False, p=None):
    return {"automation": "x", "device": "lamp", "command": {"action": "flash"}, "ok": ok, "skipped": skipped,
            "decision_p": p, "text": text}


class SummarizeTest(unittest.TestCase):
    def test_chip_threshold(self):
        self.assertEqual(web.CHIP_SURE, 0.8)

    def test_doorbell_is_a_rule(self):
        s = web.summarize({"kinds": "doorbell,night", "event_type": "button_press"}, [action()])
        self.assertEqual(s, {"headline": "Doorbell pressed", "detail": "Living room light flash x3 (Visual doorbell)",
                             "chip": {"kind": "rule", "text": "rule · no AI"}, "icon": "doorbell"})

    def test_decider_sure(self):
        s = web.summarize({"kinds": "activity", "event_type": "motion_detected",
                           "description": "A cardboard package sits on the doorstep."},
                          [action("Porch light ON for 10 min (Package porch light) (Decider p=0.93)", p=0.93)])
        self.assertEqual((s["headline"], s["icon"], s["chip"]),
                         ("A cardboard package sits on the doorstep.", "package", {"kind": "ai", "text": "Decider 0.93"}))

    def test_decider_unsure(self):
        s = web.summarize({"kinds": "activity", "event_type": "motion_detected", "description": "Something moved."},
                          [action("Porch light left alone (Package porch light): Decider unsure, p=0.61", skipped=True, p=0.61)])
        self.assertEqual(s["chip"], {"kind": "unsure", "text": "Decider 0.61"})
        self.assertEqual(s["detail"], "nothing done")

    def test_decider_says_no(self):
        s = web.summarize({"kinds": "activity", "event_type": "motion_detected", "description": "A cat walks past."},
                          [action("left alone", skipped=True, p=0.88)])
        self.assertEqual((s["icon"], s["chip"], s["detail"]), ("animal", {"kind": "skip", "text": "Decider 0.88"}, "nothing done"))

    def test_highest_p_wins_and_failed_actions_do_not_count(self):
        s = web.summarize({"kinds": "night", "event_type": "motion_detected"},
                          [action("a", skipped=True, p=0.3), action("b", ok=False, p=0.9)])
        self.assertEqual(s["chip"], {"kind": "skip", "text": "Decider 0.90"})
        self.assertEqual(s["detail"], "nothing done")

    def test_plain_rule_and_no_action(self):
        ran = web.summarize({"kinds": "night", "event_type": "motion_detected"}, [action("Hallway light ON for 5 min (Night path light)")])
        self.assertEqual((ran["headline"], ran["chip"]), ("Motion", {"kind": "rule", "text": "rule"}))
        idle = web.summarize({"kinds": "activity", "event_type": "motion_detected"}, [])
        self.assertEqual((idle["chip"], idle["detail"]), ({"kind": "skip", "text": "no action"}, "nothing done"))

    def test_person_live_view_and_history(self):
        self.assertEqual(web.summarize({"kinds": "person", "event_type": "motion_detected"}, [])["headline"], "Person at the door")
        live = web.summarize({"kinds": "activity", "event_type": "on_demand"}, [])
        self.assertEqual((live["headline"], live["icon"]), ("Live view opened", "live"))
        old = web.summarize({"kinds": None, "event_type": "motion_detected", "status": "history"}, [])
        self.assertEqual((old["chip"], old["detail"]), ({"kind": "skip", "text": "before start"}, "recorded at startup"))

    def test_icons_match_whole_words(self):
        icon = lambda d: web.summarize({"kinds": "activity", "event_type": "motion_detected", "description": d}, [])["icon"]
        self.assertEqual(icon("A silver car is parked in the driveway."), "vehicle")
        self.assertEqual(icon("A courier stands at the door."), "person")
        self.assertEqual(icon("Nothing notable at this location."), "motion")  # 'cat' inside 'location' is not a cat
        self.assertEqual(icon("Two boxes by the gate."), "package")

    def test_unsafe_text_is_returned_verbatim(self):
        s = web.summarize({"kinds": "activity", "event_type": "motion_detected", "description": "<img src=x onerror=alert(1)>"}, [])
        self.assertEqual(s["headline"], "<img src=x onerror=alert(1)>")  # the page escapes it


class StatusTest(unittest.TestCase):
    def test_status_states(self):
        health = {}
        status = app.make_status(health, Settings(), TZ, 10, "Ring API")
        first = status()
        self.assertEqual((first["mode"], first["ring_ok"]), ("Ring API", None))
        self.assertIsInstance(first["night"], bool)
        self.assertTrue(first["time"] and first["day"])
        health["last_ok"] = time.time()
        self.assertTrue(status()["ring_ok"])
        health["last_ok"] = time.time() - 60  # older than 3 x 10 s + 5 s
        self.assertFalse(status()["ring_ok"])
        self.assertFalse(app.make_status({"failed": True}, Settings(), TZ, 10, "Ring API")()["ring_ok"])


class ApiTest(unittest.TestCase):
    def start(self, **kw):
        self.store = Store(":memory:")
        server = web.make_server(self.store, TZ, 0, None, kw.get("devices"), "", False, kw.get("status"))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type"), r.read().decode()

    def test_status_endpoint(self):
        self.start(status=lambda: {"mode": "Ring API", "ring_ok": True, "night": True, "time": "10:52 PM", "day": "Thu"})
        code, ctype, body = self.get("/api/status")
        self.assertEqual((code, ctype), (200, "application/json"))
        self.assertEqual(json.loads(body)["time"], "10:52 PM")

    def test_status_endpoint_without_status_is_empty(self):
        self.start()
        self.assertEqual(json.loads(self.get("/api/status")[2]), {})

    def test_events_carry_summary_and_action_text(self):
        devices = VirtualDevices(CONFIG)
        self.start(devices=devices)
        now = int(time.time() * 1000)
        ev = {"id": "e1", "device_id": "dev1", "start_ms": now, "end_ms": now, "event_type": "button_press",
              "sub_type": None, "source": "poll"}
        self.store.begin(ev, "Front Door")
        self.store.finish("e1", "done")
        self.store.add_alert(ev, "doorbell", "Doorbell pressed.", True)
        self.store.add_actions("e1", [{"automation": "Visual doorbell", "device": "lamp", "topic": "porchlink/lamp/set",
                                       "command": {"action": "flash", "times": 3}, "ok": True, "error": None}])
        [e] = json.loads(self.get("/api/events")[2])
        self.assertEqual(e["summary"]["headline"], "Doorbell pressed")
        self.assertEqual(e["summary"]["chip"]["kind"], "rule")
        self.assertEqual(e["actions"][0]["text"], "Living room light flash x3 (Visual doorbell)")

    def test_devices_report_pulsing(self):
        devices = VirtualDevices(CONFIG)
        devices.apply("lamp", {"action": "pulse", "label": "Not sure"})
        self.start(devices=devices)
        [d] = json.loads(self.get("/api/devices")[2])
        self.assertTrue(d["pulsing"])
        self.assertEqual(d["last"]["label"], "Not sure")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python3 -m unittest test_dashboard`
Expected: errors such as `KeyError: 'pulsing'`, `AttributeError: module 'web' has no attribute 'summarize'`, `module 'app' has no attribute 'make_status'`, and `make_server() takes … positional arguments`.

- [ ] **Step 3: Pulse and labels in `bridge.py`**

1. In `VirtualDevices.__init__`, add `"pulse_until": 0,` to each device's state dict, next to `"flash_until": 0,`.
2. In `VirtualDevices.apply`, add this branch after the `flash` branch:

```python
            elif cmd["action"] == "pulse":
                s["pulse_until"] = now + cmd.get("duration_s", 10)  # a soft, slow glow: "the AI is not sure"
```

   and change the `s["last"] = ...` line to:

```python
            s["last"] = {"automation": cmd.get("automation"), "label": cmd.get("label"), "at": int(now * 1000)}
```

3. In `VirtualDevices.snapshot`, add `"pulsing": s["pulse_until"] > now,` next to `"flashing": ...`.
4. In `Bridge.run`, change the `payload = {...}` line to also carry the automation's label:

```python
            payload = {**command, "automation": automation["name"], "event_id": ev["id"],
                       "source": source_name, "sent_at": int(self.clock() * 1000)}
            if automation.get("label"):
                payload["label"] = automation["label"]  # what the dashboard tile says ("Doorbell!", "Package")
```

5. In `describe_action`, add a branch before the final `else:`:

```python
    elif c["action"] == "pulse":
        what = "gentle pulse"
```

- [ ] **Step 4: Label the shipped automations**

In `automations.json`, add a `"label"` key to each automation, right after its `"why"` line:
- `"Night path light"` gets `"label": "Night path"`
- `"Visual doorbell"` gets `"label": "Doorbell!"`
- `"Package porch light"` gets `"label": "Package"`
- `"Night vehicle floodlight"` gets `"label": "Car arriving"`

- [ ] **Step 5: Summaries and the status endpoint in `web.py`**

1. Change the imports at the top to:

```python
import html, json, re, time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
```

   `html` and `datetime` are still used by the old page; Task 2 removes them.

2. Add these after `REQUEST_TIMEOUT`:

```python
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
```

3. Change the signature `def make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False):` to `def make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None):`.

4. In `do_GET`, add a status route as the first check, after `def do_GET(self):`:

```python
            if self.path == "/api/status":
                return self._send(200, "application/json", json.dumps(status() if status else {}))
```

   Then replace the `/api/events` branch's send line with:

```python
            if self.path == "/api/events":
                names = {d["id"]: d["name"] for d in (devices.snapshot() if devices else [])}
                out = []
                for e in items:
                    acts = [{**a, "text": describe_action({**a, "device_name": names.get(a["device"], a["device"])})}
                            for a in acts_by_event.get(e["id"], [])]
                    out.append({**e, "actions": acts, "summary": summarize(e, acts)})
                self._send(200, "application/json", json.dumps(out))
```

   Rename the existing `acts = store.actions_for(...)` line just above it to `acts_by_event = store.actions_for([e["id"] for e in items])`, and update the `/` branch to use `acts_by_event.get(...)`.

5. Change `serve` to pass `status` through:

```python
def serve(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None):
    server = make_server(store, tz, port, enqueue, devices, webhook_secret, log_bodies, status)
```

   Keep its print and `serve_forever` lines.

- [ ] **Step 6: Status and health in `app.py`**

1. Change `from rules import Settings, evaluate, format_alert` to `from rules import Settings, evaluate, format_alert, is_night`.
2. Add after `make_process`:

```python
def make_status(health, settings, tz, poll_seconds, mode):
    """The dashboard header: is Ring answering, local time, and day or night (the app's night window)."""
    def status():
        now = datetime.now(tz)
        last = health.get("last_ok")
        if last is None:
            ring_ok = False if health.get("failed") else None
        else:
            ring_ok = time.time() - last < 3 * poll_seconds + 5
        return {"mode": mode, "ring_ok": ring_ok, "night": is_night(now.hour, settings),
                "time": now.strftime("%I:%M %p").lstrip("0"), "day": now.strftime("%a")}
    return status
```

3. In `main()`, right before the `threading.Thread(target=web.serve, ...)` line, add:

```python
    health = {}
    status = make_status(health, settings, tz, poll_seconds, "FAKE mode" if fake else "Ring API")
```

   Then add `status` as the last argument of that `web.serve` thread's `args` tuple.

4. In the poll loop, after `poll_once(ring, store, worker.submit, seeded=seeded)`, add `health["last_ok"] = time.time()`. In the generic `except Exception as e:` branch, add `health["failed"] = True` before the print.

- [ ] **Step 7: Run the tests to make sure they pass**

Run: `python3 -m unittest test_dashboard`. Expected: `OK` (20 tests).
Run the full suite. Expected: `OK`, 122 tests.

- [ ] **Step 8: Commit**

```bash
git add bridge.py automations.json web.py app.py test_dashboard.py
git commit -m "feat(dashboard): pulse action, automation labels, event summaries and status API

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The night-mode page and a preview script

**Files:**
- Modify: `web.py` (replace `PAGE` and the `/` route; remove the server-rendered `row` helper and the now-unused `html` and `datetime` imports)
- Create: `dashboard_preview.py`
- Modify: `test_dashboard.py` (a page test)
- Modify: `README.md` (one line about the preview)

**Interfaces:**
- Consumes: Task 1's `/api/devices` (`pulsing`, `last.label`), `/api/events` (`summary`), `/api/status`.

- [ ] **Step 1: Write the failing page test**

Add to `test_dashboard.py`, inside `ApiTest`:

```python
    def test_page_is_the_night_dashboard(self):
        self.start()
        code, ctype, body = self.get("/")
        self.assertEqual((code, ctype), (200, "text/html; charset=utf-8"))
        for marker in ('id="tiles"', 'id="events"', 'id="legend"', 'id="status-text"',
                       "Door events · last 24 hours", "/api/status", "#0e1116", "prefers-reduced-motion"):
            self.assertIn(marker, body)
        self.assertNotIn("location.reload", body)  # live updates, no page reloads
        self.assertNotIn("<script src", body)      # everything inline
```

Run `python3 -m unittest test_dashboard`. Expected: FAIL on `id="tiles"`.

- [ ] **Step 2: Replace the page in `web.py`**

1. Replace the whole `PAGE = """...""" ` constant with the following. It is a plain string, not a format template, so use single braces.

```python
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
```

2. In `do_GET`, replace the whole `elif self.path == "/":` branch (the `block = ...`, `rows = ...` and `self._send(... PAGE.format(...))` lines) with:

```python
            elif self.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE)
```

3. Delete the nested `def row(e, actions):` helper. Change the imports to `import json, re, time`, removing `html` and `datetime`. Then run `grep -n "html\.\|datetime" web.py`; it must print nothing.

- [ ] **Step 3: Create `dashboard_preview.py`**

```python
#!/usr/bin/env python3
"""Show the PorchLink dashboard with every light state and sample events, without Ring or AWS.

Run: python3 dashboard_preview.py [--port 8090]   then open http://127.0.0.1:8090
Lights: hallway off, living room flashing (doorbell), porch amber (package), driveway gentle pulse (not sure).
The states re-apply every few seconds so the preview stays lively for screenshots and the demo video.
"""
import argparse, threading, time
from zoneinfo import ZoneInfo

import web
from bridge import VirtualDevices, load_config
from store import Store

TZ = ZoneInfo("Asia/Kathmandu")
STATES = [("living-room-light", {"action": "flash", "times": 4, "automation": "Visual doorbell", "label": "Doorbell!"}),
          ("porch-light", {"action": "on", "duration_s": 480, "automation": "Package porch light", "label": "Package"}),
          ("floodlight", {"action": "pulse", "duration_s": 6, "automation": "Not sure", "label": "Not sure"})]


def seed(store, now_ms):
    events = [  # (id, minutes ago, type, description, alert kind, action, decision p)
        ("p-doorbell", 0, "button_press", None, "doorbell",
         {"automation": "Visual doorbell", "device": "living-room-light", "command": {"action": "flash", "times": 3}}, None),
        ("p-unsure", 5, "motion_detected", "Something moved at the edge of the driveway.", "night",
         {"automation": "Not sure", "device": "floodlight", "command": {"action": "pulse"}}, 0.61),
        ("p-package", 11, "motion_detected", "A cardboard package sits on the doorstep.", "activity",
         {"automation": "Package porch light", "device": "porch-light", "command": {"action": "on", "duration_s": 600}}, 0.93),
        ("p-cat", 22, "motion_detected", "A cat walks across the back yard.", "activity", None, 0.88),
    ]
    for eid, ago, kind, description, alert, act, p in events:
        start = now_ms - ago * 60_000
        ev = {"id": eid, "device_id": "front" if eid != "p-cat" else "back", "start_ms": start, "end_ms": start,
              "event_type": kind, "sub_type": None, "source": "poll"}
        store.begin(ev, "Front Door" if eid != "p-cat" else "Back Yard")
        store.finish(eid, "done")
        if description:
            store.set_description(eid, description)
        store.add_alert(ev, alert, alert, True)
        if act:
            store.add_actions(eid, [{**act, "topic": f"porchlink/{act['device']}/set", "ok": True, "error": None,
                                     "decision_p": p}])
        else:  # Decider said no: recorded as a skipped action, the house did nothing
            store.add_actions(eid, [{"automation": "Package porch light", "device": "porch-light",
                                     "topic": "porchlink/porch-light/set", "command": {"action": "on"}, "ok": True,
                                     "error": None, "skipped": True, "decision_p": p}])


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8090)
    a = p.parse_args()
    devices, store = VirtualDevices(load_config("automations.json")), Store(":memory:")
    seed(store, int(time.time() * 1000))

    def keep_lively():
        while True:
            for device_id, cmd in STATES:
                devices.apply(device_id, cmd)
            time.sleep(5)

    threading.Thread(target=keep_lively, daemon=True).start()
    status = lambda: {"mode": "Preview", "ring_ok": True, "night": True,
                      "time": time.strftime("%I:%M %p").lstrip("0"), "day": time.strftime("%a")}
    web.serve(store, TZ, a.port, None, devices, "", False, status)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m unittest test_dashboard`. Expected: `OK` (21 tests).
Run the full suite. Expected: `OK`, 123 tests.

- [ ] **Step 5: Check the preview runs**

1. Start it in the background: `python3 dashboard_preview.py --port 8090 &`.
2. Run `curl -s localhost:8090/api/devices`. It must show one `"flashing": true`, one `"on": true` and one `"pulsing": true`.
3. Run `curl -s localhost:8090/api/events`. It must show 4 events, with chips `rule`, `unsure`, `ai` and `skip`.
4. Stop it with `kill %1`.

- [ ] **Step 6: Document it**

In `README.md`, after the line that starts `Dashboard: http://127.0.0.1:8080`, add:

```
Preview the dashboard without Ring or AWS (every light state + sample events): `python3 dashboard_preview.py`, then open http://127.0.0.1:8090
```

- [ ] **Step 7: Commit**

```bash
git add web.py dashboard_preview.py test_dashboard.py README.md
git commit -m "feat(dashboard): night-mode page with glowing amber tiles, live status and event chips; preview script

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

The visual check runs afterwards on the Sonnet model. It screenshots the preview at 1280 px and 390 px.

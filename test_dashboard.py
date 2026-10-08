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

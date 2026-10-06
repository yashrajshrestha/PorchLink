"""Tests against the local fake Ring server, including a chaos (monkey) run. Standard library only.

Run: python3 -m unittest test_sim -v
"""
import json, os, threading, unittest, urllib.error, urllib.request
from zoneinfo import ZoneInfo

import app
from rules import Settings
from ring_client import RingClient, TokenExpired
from sim.fake_ring_server import FakeRing, make_server
from store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.join(HERE, "sim", "scenarios", "night_visit.json")
TZ = ZoneInfo("Asia/Kathmandu")


class CaptureAlerter:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append(subject)
        return True


class SimTestCase(unittest.TestCase):
    def start(self, **kw):
        # speed 1e6: every scenario event is already "in the past" when the test starts
        self.fake = FakeRing(SCENARIO, **{"speed": 1e6, "seed": 7, **kw})
        self.server = make_server(self.fake, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        return RingClient("sim-token", self.base)

    def post(self, path, body, token="sim-token"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()


class FakeServerTest(SimTestCase):
    def test_devices_and_history_shape(self):
        ring = self.start()
        devices = ring.devices()
        self.assertEqual([d["attributes"]["name"] for d in devices], ["Front Door", "Back Yard"])
        events = ring.events("sim.device.front")
        self.assertEqual(len(events), 4)
        self.assertEqual(events[0]["type"], "history-events")
        self.assertIn("cv_detections", events[0]["relationships"])

    def test_token_expiry_raises(self):
        ring = self.start(token_ttl=-1)
        with self.assertRaises(TokenExpired):
            ring.devices()

    def test_snapshot_errors_match_real_api(self):
        self.start()
        ev = self.fake.events[0]
        path = f"/v1/devices/{ev['device']}/media/image/download"
        cases = [({}, 400), ({"type": "jpeg", "timestamp": ev["start"]}, 400),
                 ({"type": "at_timestamp"}, 403), ({"type": "at_timestamp", "timestamp": 1000}, 416)]
        for body, code in cases:
            self.assertEqual(self.post(path, body)[0], code, body)
        status, ctype, data = self.post(path, {"type": "at_timestamp", "timestamp": ev["start"] + 1000})
        self.assertEqual((status, ctype, data[:2]), (200, "image/jpeg", b"\xff\xd8"))  # JPEG magic bytes


class AppAgainstSimTest(SimTestCase):
    def setUp(self):
        self.store, self.alerter = Store(":memory:"), CaptureAlerter()

    def alerts_per_event(self):
        rows = self.store.db.execute("SELECT event_id, COUNT(DISTINCT kind), COUNT(*) FROM alerts GROUP BY event_id")
        return {r[0]: (r[1], r[2]) for r in rows}

    def test_rules_fire_for_scenario(self):
        ring = self.start()
        app.poll_once(ring, self.store, self.alerter, Settings(), TZ)
        subjects = " | ".join(self.alerter.sent)
        self.assertEqual(len(self.alerter.sent), 7)
        self.assertIn("Doorbell at Front Door", subjects)      # 22:52 button_press
        self.assertIn("Person at Front Door", subjects)        # 22:50 motion human
        self.assertIn("Unusual activity at Back Yard", subjects)  # after 23:00, plus a burst of 3

    def test_monkey_chaos_every_event_alerts_once(self):
        ring = self.start(chaos=0.5)
        on_webhook = app.make_webhook_handler(ring, self.store, self.alerter, Settings(), TZ)
        errors = 0
        for _ in range(40):
            try:
                app.poll_once(ring, self.store, self.alerter, Settings(), TZ)
            except RuntimeError:
                errors += 1  # injected 500s: the real loop logs these and polls again
            for e in self.fake.due_webhooks():
                for _ in range(2):  # every webhook delivered twice, racing the poller
                    try:
                        on_webhook(self.fake.webhook_payload(e))
                    except RuntimeError:
                        errors += 1  # device lookup hit an injected 500; the next delivery retries
        self.fake.chaos = 0
        app.poll_once(ring, self.store, self.alerter, Settings(), TZ)  # quiet poll picks up anything missed
        self.assertGreater(errors, 0, "chaos should have injected some failures")
        per_event = self.alerts_per_event()
        self.assertEqual(set(per_event), {e["id"] for e in self.fake.events})
        self.assertEqual(len(self.alerter.sent), len(self.fake.events))  # one email per event, no repeats
        for kinds, rows in per_event.values():
            self.assertEqual(kinds, rows)  # no alert kind recorded twice for the same event


if __name__ == "__main__":
    unittest.main()

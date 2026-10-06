"""Tests against the local fake Ring server, including a chaos (monkey) run. Standard library only.

Run: python3 -m unittest test_sim -v
"""
import json, os, threading, unittest, urllib.error, urllib.request
from unittest import mock
from zoneinfo import ZoneInfo

import app
import web
from rules import Settings
from ring_client import RingClient, TokenExpired
from sim.fake_ring_server import FakeRing, make_server
from store import Store
from webhooks import parse_webhook, verify_signature
from worker import EventWorker

HERE = os.path.dirname(os.path.abspath(__file__))
SCENARIO = os.path.join(HERE, "sim", "scenarios", "night_visit.json")
TZ = ZoneInfo("Asia/Kathmandu")
SECRET = "sim-secret"


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

    def test_webhooks_are_real_shape_and_signed(self):
        self.start()
        e = next(x for x in self.fake.events if x["type"] == "button_press")
        [(body, signature, forged)] = self.fake.deliveries(e, SECRET)  # no chaos: exactly one delivery
        self.assertFalse(forged)
        self.assertTrue(verify_signature(SECRET, body, signature))
        ev = parse_webhook(json.loads(body))
        self.assertEqual((ev["id"], ev["device_id"], ev["start_ms"], ev["event_type"]),
                         (f"webhook:{e['request_id']}", e["device"], e["start"], "button_press"))


class AppAgainstSimTest(SimTestCase):
    def setUp(self):
        self.store, self.alerter = Store(":memory:"), CaptureAlerter()

    def alerts_per_event(self):
        rows = self.store.db.execute("SELECT event_id, COUNT(DISTINCT kind), COUNT(*) FROM alerts GROUP BY event_id")
        return {r[0]: (r[1], r[2]) for r in rows}

    def test_rules_fire_for_scenario(self):
        ring = self.start()
        worker = EventWorker(self.store, app.make_process(self.store, self.alerter, Settings(), TZ))
        app.poll_once(ring, self.store, worker.handle)
        subjects = " | ".join(self.alerter.sent)
        self.assertEqual(len(self.alerter.sent), 7)
        self.assertIn("Doorbell at Front Door", subjects)      # 22:52 button_press
        self.assertIn("Person at Front Door", subjects)        # 22:50 motion human
        self.assertIn("Unusual activity at Back Yard", subjects)  # after 23:00, plus a burst of 3

    def test_monkey_chaos_every_event_handled_once(self):
        """Injected 500s, duplicated and shuffled history, Ring-style webhook re-deliveries and forged webhooks.
        Poll and signed webhooks race through the real endpoint into one worker: every door event is handled
        exactly once, by exactly one route, and every forgery is refused."""
        ring = self.start(chaos=0.5)
        worker = EventWorker(self.store, app.make_process(self.store, self.alerter, Settings(), TZ),
                             name_for=app.DeviceNames(ring)).start()
        server = web.make_server(self.store, TZ, 0, worker.submit, None, SECRET)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/webhook"

        def post(body, signature):
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-Signature": signature})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        forged = 0
        with mock.patch("builtins.print"):
            for _ in range(40):
                try:
                    app.poll_once(ring, self.store, worker.submit)
                except RuntimeError:
                    pass  # devices() hit an injected 500: the real loop logs it and polls again
                for e in self.fake.due_webhooks():
                    for body, signature, is_forged in self.fake.deliveries(e, SECRET):
                        self.assertEqual(post(body, signature), 401 if is_forged else 200)
                        forged += is_forged
            self.assertTrue(worker.drain(10))
            self.fake.chaos = 0
            app.poll_once(ring, self.store, worker.submit)  # quiet poll picks up anything missed
            self.assertTrue(worker.drain(10))

        self.assertGreater(self.fake.injected_failures, 0, "chaos should have injected some failures")
        self.assertGreater(forged, 0, "chaos should have sent some forged webhooks")
        status = dict(self.store.db.execute("SELECT id, status FROM events").fetchall())
        self.assertFalse({"pending", "failed"} & set(status.values()), status)
        for e in self.fake.events:  # each physical event: handled once, by the poll or by its webhook
            routes = [status.get(e["id"]), status.get(f"webhook:{e['request_id']}")]
            self.assertEqual(routes.count("done"), 1, (e["id"], routes))
        self.assertNotIn("forged", " ".join(status))  # forgeries never reach the store
        self.assertEqual(len(self.alerter.sent), len(self.fake.events))  # one email per event, no repeats
        for kinds, rows in self.alerts_per_event().values():
            self.assertEqual(kinds, rows)  # no alert kind recorded twice for the same event


if __name__ == "__main__":
    unittest.main()

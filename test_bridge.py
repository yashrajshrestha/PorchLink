"""Tests for the PorchLink bridge: automation matching, virtual devices, IoT Core publishing, and the full
Ring event -> rules -> actions -> email flow (fake Ring server, stub Bedrock). Run: python3 -m unittest test_bridge -v
"""
import json, os, unittest

import app
from bridge import Bridge, IotCorePublisher, LocalPublisher, MqttPublisher, VirtualDevices, describe_action, load_config, matches
from rules import Settings
from store import Store
from worker import EventWorker
from test_sim import TZ, SimTestCase

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = load_config(os.path.join(HERE, "automations.json"))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class MatchTest(unittest.TestCase):
    def test_conditions(self):
        self.assertTrue(matches({"kind": ["night"]}, {"night", "person"}, None, "Front Door"))
        self.assertFalse(matches({"kind": ["night"]}, {"doorbell"}, None, "Front Door"))
        self.assertTrue(matches({"description_has": ["box"]}, set(), "A Box on the step.", "x"))
        self.assertFalse(matches({"description_has": ["box"]}, set(), None, "x"))
        both = {"kind": ["night"], "description_has": ["car"]}
        self.assertFalse(matches(both, {"night"}, "A person at the door.", "x"))  # all conditions must hold
        self.assertTrue(matches(both, {"night"}, "A silver car is parked.", "x"))
        self.assertFalse(matches({"source": ["Back Yard"]}, set(), None, "Front Door"))

    def test_config_rejects_unknown_device(self):
        bad = os.path.join(HERE, "sim", "_bad_automations.json")
        with open(bad, "w") as f:
            json.dump({"devices": [], "automations": [{"name": "x", "when": {}, "do": [{"device": "nope", "action": "on"}]}]}, f)
        self.addCleanup(os.remove, bad)
        with self.assertRaises(ValueError):
            load_config(bad)


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.devices = VirtualDevices(CONFIG, self.clock)
        self.pub = LocalPublisher(self.devices)
        self.bridge = Bridge(CONFIG, self.pub, self.clock)
        self.ev = {"id": "e1", "start_ms": 1_000_000_000}

    def state(self, device_id):
        return next(d for d in self.devices.snapshot() if d["id"] == device_id)

    def test_night_turns_hallway_on_then_it_times_out(self):
        results = self.bridge.run(self.ev, {"night"}, None, "Front Door")
        self.assertEqual([r["device"] for r in results], ["hallway-light"])
        self.assertEqual(self.pub.sent[0][0], "porchlink/hallway-light/set")
        self.assertTrue(self.state("hallway-light")["on"])
        self.clock.t += 301
        self.assertFalse(self.state("hallway-light")["on"])

    def test_doorbell_flashes_living_room(self):
        self.bridge.run(self.ev, {"doorbell"}, None, "Front Door")
        self.assertTrue(self.state("living-room-light")["flashing"])
        self.clock.t += 4
        self.assertFalse(self.state("living-room-light")["flashing"])

    def test_package_needs_description_and_respects_cooldown(self):
        def at(minutes):
            return {"id": f"e{minutes}", "start_ms": 1_000_000_000 + minutes * 60_000}
        self.assertEqual(self.bridge.run(at(0), {"activity"}, None, "Front Door"), [])
        first = self.bridge.run(at(0), {"activity"}, "A box is on the doorstep.", "Front Door")
        self.assertEqual([r["device"] for r in first], ["porch-light"])
        self.assertEqual(self.bridge.run(at(1), {"activity"}, "Another box.", "Front Door"), [])  # cooldown 600 s
        self.assertEqual(len(self.bridge.run(at(11), {"activity"}, "A parcel.", "Front Door")), 1)

    def test_backlog_processed_at_once_uses_event_times(self):
        # Two packages 15 minutes apart, processed in the same instant: both must fire.
        for minutes in (0, 15):
            ev = {"id": f"e{minutes}", "start_ms": 1_000_000_000 + minutes * 60_000}
            self.assertEqual(len(self.bridge.run(ev, {"activity"}, "A box.", "Front Door")), 1)

    def test_night_vehicle_fires_two_automations(self):
        results = self.bridge.run(self.ev, {"night"}, "A silver car is parked in the driveway.", "Back Yard")
        self.assertEqual(sorted(r["device"] for r in results), ["floodlight", "hallway-light"])

    def test_failed_publish_is_reported_not_raised(self):
        class Broken:
            def publish(self, topic, payload):
                raise ConnectionError("broker down")
        results = Bridge(CONFIG, Broken(), self.clock).run(self.ev, {"night"}, None, "Front Door")
        self.assertFalse(results[0]["ok"])
        self.assertIn("broker down", results[0]["error"])
        self.assertTrue(describe_action(results[0]).endswith("FAILED"))

    def test_payload_shape(self):
        self.bridge.run(self.ev, {"night"}, None, "Front Door")
        payload = self.pub.sent[0][1]
        self.assertEqual({k: payload[k] for k in ("action", "duration_s", "automation", "event_id", "source")},
                         {"action": "on", "duration_s": 300, "automation": "Night path light",
                          "event_id": "e1", "source": "Front Door"})


class DeciderBridgeTest(unittest.TestCase):
    """Decider replaces keyword matching for automations with an `ask` question; a fake decide() stands in."""

    def setUp(self):
        self.clock, self.calls, self.answer = Clock(), [], {}
        self.devices = VirtualDevices(CONFIG, self.clock)
        self.pub = LocalPublisher(self.devices)

        def decide(state, questions):
            self.calls.append((state, questions))
            return None if self.answer is None else {n: self.answer.get(n, 0.0) for n in questions}
        self.bridge = Bridge(CONFIG, self.pub, self.clock, decide=decide, min_p=0.8)

    def at(self, minutes):
        return {"id": f"e{minutes}", "start_ms": 1_000_000_000 + minutes * 60_000}

    def test_confident_yes_fires_even_without_keywords(self):
        self.answer = {"Package porch light": 0.93}
        results = self.bridge.run(self.at(0), {"activity"}, "Something brown is leaning on the door.", "Front Door")
        self.assertEqual([(r["device"], r["skipped"], r["decision_p"]) for r in results], [("porch-light", False, 0.93)])
        self.assertIn("Decider p=0.93", describe_action(results[0]))
        self.assertEqual(len(self.calls), 1)  # one request per event

    def test_unsure_skips_without_publishing_or_using_the_cooldown(self):
        self.answer = {"Package porch light": 0.62}
        results = self.bridge.run(self.at(0), {"activity"}, "No package visible, just a cardboard box sign.", "Front Door")
        self.assertEqual([(r["device"], r["skipped"]) for r in results], [("porch-light", True)])
        self.assertEqual(self.pub.sent, [])
        self.assertIn("left alone (Package porch light): Decider unsure, p=0.62", describe_action(results[0]))
        self.answer = {"Package porch light": 0.95}
        again = self.bridge.run(self.at(1), {"activity"}, "A box sits on the doorstep.", "Front Door")
        self.assertEqual([r["skipped"] for r in again], [False])

    def test_confident_no_is_shown_only_when_keywords_would_have_fired(self):
        self.answer = {"Package porch light": 0.1}
        sign = self.bridge.run(self.at(0), {"activity"}, "No package, just a cardboard box sign.", "Front Door")
        self.assertIn("Decider said no, p=0.10", describe_action(sign[0]))
        self.assertEqual(self.bridge.run(self.at(1), {"activity"}, "A cat on the step.", "Front Door"), [])

    def test_decider_failure_falls_back_to_keywords(self):
        self.answer = None
        results = self.bridge.run(self.at(0), {"activity"}, "A box is on the doorstep.", "Front Door")
        self.assertEqual([(r["device"], r["decision_p"]) for r in results], [("porch-light", None)])

    def test_not_asked_without_description_or_when_kind_filter_fails(self):
        self.bridge.run(self.at(0), {"activity"}, None, "Front Door")
        self.bridge.run(self.at(1), {"activity"}, "A silver car.", "Front Door")  # vehicle rule needs night
        self.assertEqual([sorted(q) for _, q in self.calls], [["Package porch light"]])

    def test_skipped_action_round_trips_through_store(self):
        self.answer = {"Package porch light": 0.6}
        store = Store(":memory:")
        store.add_actions("e0", self.bridge.run(self.at(0), {"activity"}, "A doormat.", "Front Door"))
        saved = store.actions_for(["e0"])["e0"][0]
        self.assertEqual((saved["skipped"], saved["decision_p"]), (True, 0.6))


class IotCoreTest(unittest.TestCase):
    def test_publishes_json_qos1_to_ats_endpoint(self):
        class StubData:
            calls = []

            def publish(self, **kw):
                self.calls.append(kw)

        data = StubData()
        IotCorePublisher({}, data_client=data).publish("porchlink/porch-light/set", {"action": "on"})
        self.assertEqual(data.calls[0]["topic"], "porchlink/porch-light/set")
        self.assertEqual(data.calls[0]["qos"], 1)
        self.assertEqual(json.loads(data.calls[0]["payload"]), {"action": "on"})


class MqttPublisherTest(unittest.TestCase):
    class Info:
        def __init__(self, ok):
            self.ok = ok

        def wait_for_publish(self, timeout):
            pass

        def is_published(self):
            return self.ok

    class Client:
        def __init__(self, ok=True):
            self.ok, self.calls = ok, []

        def publish(self, topic, payload, qos):
            self.calls.append((topic, json.loads(payload), qos))
            return MqttPublisherTest.Info(self.ok)

    def test_publishes_json_qos1(self):
        client = self.Client()
        MqttPublisher({}, client).publish("porchlink/porch-light/set", {"action": "on"})
        self.assertEqual(client.calls, [("porchlink/porch-light/set", {"action": "on"}, 1)])

    def test_no_ack_raises_so_bridge_reports_failed(self):
        bridge = Bridge(CONFIG, MqttPublisher({}, self.Client(ok=False)), Clock())
        results = bridge.run({"id": "e1", "start_ms": 1_000_000_000}, {"doorbell"}, None, "Front Door")
        self.assertEqual([(r["ok"], "did not acknowledge" in r["error"]) for r in results], [(False, True)])


class EndToEndTest(SimTestCase):
    """Fake Ring server plays night_visit.json; a stub 'Bedrock' describes each event from its scenario image."""

    def test_scenario_drives_the_lights(self):
        ring = self.start()
        images = {e["id"]: os.path.basename(e["image_path"] or "") for e in self.fake.events}
        text = {"person.jpg": "A courier is holding a box at the door.", "package.jpg": "A box sits on the doorstep.",
                "vehicle.jpg": "A silver car is parked in the driveway.", "empty_porch.jpg": "Nothing notable."}

        def describe(ev):
            return text.get(images.get(ev["id"]))

        store, devices = Store(":memory:"), VirtualDevices(CONFIG)
        bodies = []

        class Alerter:
            def send(self, subject, body):
                bodies.append((subject, body))
                return True

        bridge = Bridge(CONFIG, LocalPublisher(devices))
        worker = EventWorker(store, app.make_process(store, Alerter(), Settings(), TZ, describe=describe, bridge=bridge))
        app.poll_once(ring, store, worker.handle)

        fired = store.db.execute("SELECT automation, COUNT(*) FROM actions GROUP BY automation").fetchall()
        fired = dict(fired)
        self.assertEqual(fired.get("Visual doorbell"), 1)          # 22:52 button_press
        self.assertGreaterEqual(fired.get("Night path light", 0), 4)  # every event after 23:00
        self.assertEqual(fired.get("Night vehicle floodlight"), 1)  # 23:11 car, back yard
        self.assertEqual(fired.get("Package porch light"), 2)       # 22:50 and 23:05; 22:52 and 23:12 in cooldown
        doorbell = next(b for s, b in bodies if s.startswith("PorchLink: Doorbell"))
        self.assertIn("Home actions:", doorbell)
        self.assertIn("Living room light flash x3 (Visual doorbell)", doorbell)


if __name__ == "__main__":
    unittest.main()

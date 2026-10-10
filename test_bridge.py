"""Tests for the PorchLink bridge: automation matching, virtual devices, IoT Core publishing, and the full
Ring event -> rules -> actions -> email flow (fake Ring server, stub Bedrock). Run: python3 -m unittest test_bridge -v
"""
import json, os, unittest
from unittest import mock
from datetime import datetime

import app
from bridge import (Bridge, IotCorePublisher, LocalPublisher, MqttPublisher, VirtualDevices, describe_action, load_config,
                    matches, mentions)
from decider import SEEN_OPTIONS, SEEN_QUESTION
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

    def test_decider_conditions(self):
        pkg = {"label": "package", "p": 0.7}
        self.assertTrue(matches({"seen": ["package"]}, set(), None, "x", pkg, 0.5))
        self.assertFalse(matches({"seen": ["package"]}, set(), None, "x", pkg, 0.8))  # below min_p
        self.assertFalse(matches({"seen": ["person"]}, set(), None, "x", pkg, 0.5))
        self.assertTrue(matches({"seen": ["package"], "description_has": ["car"]}, set(), "A box.", "x", pkg, 0.5))  # keywords ignored
        self.assertFalse(matches({"seen": ["package"]}, {"doorbell"}, "A box.", "x", pkg, 0.5))  # never for a doorbell press
        self.assertTrue(matches({"time": "night"}, {"night"}, None, "x"))
        self.assertFalse(matches({"time": "night"}, {"activity"}, None, "x"))
        self.assertTrue(matches({"time": "day"}, {"activity"}, None, "x"))
        self.assertTrue(matches({"unsure": True}, set(), "A shape.", "x", {"label": "person", "p": 0.4}, 0.5))
        self.assertFalse(matches({"unsure": True}, set(), "A box.", "x", pkg, 0.5))
        self.assertFalse(matches({"unsure": True}, set(), "A shape.", "x", None, 0.5))  # no answer is not "unsure"
        self.assertFalse(matches({"unsure": True}, {"doorbell"}, "A shape.", "x", {"label": "person", "p": 0.4}, 0.5))

    def test_no_decision_condition(self):
        when = {"kind": ["night"], "no_decision": True}
        self.assertTrue(matches(when, {"night"}, None, "x"))
        self.assertFalse(matches(when, {"night"}, "A cat.", "x", {"label": "animal", "p": 0.9}, 0.5))
        self.assertFalse(matches({"no_decision": True}, {"doorbell", "night"}, None, "x"))  # never for a doorbell press

    def test_keywords_are_whole_words_and_skip_negated_mentions(self):
        self.assertTrue(mentions("A box is on the doorstep.", ["box"]))
        self.assertTrue(mentions("Two parcels by the door.", ["parcel"]))
        self.assertTrue(mentions("Boxes on the step.", ["box"]))
        self.assertTrue(mentions("A man stands at the door, no packages.", ["man"]))  # the mention comes before the negation
        self.assertFalse(mentions("An empty porch; no person is visible.", ["person"]))
        self.assertFalse(mentions("Many leaves blow across the porch.", ["man"]))  # "man" inside "many"
        self.assertFalse(mentions("A cardboard box sits on the doorstep.", ["car"]))  # "car" inside "cardboard"
        self.assertFalse(mentions("No people, packages, vehicles or animals are visible at the door.",
                                  ["people", "package", "vehicle"]))
        self.assertFalse(mentions("There isn't a car here.", ["car"]))
        self.assertFalse(mentions(None, ["box"]))
        self.assertFalse(matches({"description_has": ["person"]}, set(), "An empty porch; no person is visible.", "x"))

    def test_without_a_decision_seen_falls_back_to_keywords(self):
        self.assertTrue(matches({"seen": ["package"], "description_has": ["box"]}, set(), "A box.", "x"))
        self.assertFalse(matches({"seen": ["package"], "description_has": ["box"]}, set(), "A cat.", "x"))
        self.assertFalse(matches({"seen": ["person"]}, set(), "A person.", "x"))  # no keywords to fall back on

    def test_config_rejects_unknown_device(self):
        bad = os.path.join(HERE, "sim", "_bad_automations.json")
        with open(bad, "w") as f:
            json.dump({"devices": [], "automations": [{"name": "x", "when": {}, "do": [{"device": "nope", "action": "on"}]}]}, f)
        self.addCleanup(os.remove, bad)
        with self.assertRaises(ValueError):
            load_config(bad)

    def test_config_warns_that_press_rules_cannot_use_the_description(self):
        path = os.path.join(HERE, "sim", "_press_words.json")
        with open(path, "w") as f:
            json.dump({"devices": [{"id": "l", "name": "Lamp", "type": "light"}], "automations": [
                {"name": "Courier ring", "when": {"kind": ["doorbell"], "description_has": ["courier"]},
                 "do": [{"device": "l", "action": "flash"}]}]}, f)
        self.addCleanup(os.remove, path)
        with mock.patch("builtins.print") as printed:
            load_config(path)
        self.assertIn("Courier ring", printed.call_args[0][0])
        self.assertIn("never matches a press", printed.call_args[0][0])


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.devices = VirtualDevices(CONFIG, self.clock)
        self.pub = LocalPublisher(self.devices)
        self.bridge = Bridge(CONFIG, self.pub, self.clock)
        self.ev = {"id": "e1", "start_ms": 1_000_000_000}

    def state(self, device_id):
        return next(d for d in self.devices.snapshot() if d["id"] == device_id)

    def test_package_light_turns_on_then_times_out(self):
        results = self.bridge.run(self.ev, {"activity"}, "A box is on the doorstep.", "Front Door")
        self.assertEqual([r["device"] for r in results], ["porch-light"])
        self.assertEqual(self.pub.sent[0][0], "porchlink/porch-light/set")
        self.assertTrue(self.state("porch-light")["on"])
        self.clock.t += 601
        self.assertFalse(self.state("porch-light")["on"])

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

    def test_night_vehicle_by_keywords(self):
        results = self.bridge.run(self.ev, {"night"}, "A silver car is parked in the driveway.", "Back Yard")
        self.assertEqual([(r["automation"], r["device"]) for r in results],
                         [("Night vehicle floodlight", "floodlight"), ("Night light (AI off)", "hallway-light")])
        self.assertEqual(self.bridge.run(self.ev, {"activity"}, "A silver car is parked.", "Back Yard"), [])  # day

    def test_cardboard_box_is_not_a_car_and_an_empty_porch_is_not_a_visitor(self):
        night = self.bridge.run(self.ev, {"night"}, "A cardboard box sits on the doorstep.", "Front Door")
        self.assertEqual(sorted(r["automation"] for r in night), ["Night light (AI off)", "Package porch light"])
        empty = self.bridge.run(self.ev, {"night"}, "An empty porch; no person is visible.", "Front Door")
        self.assertEqual([r["automation"] for r in empty], ["Night light (AI off)"])

    def test_without_decider_visitor_uses_keywords_and_unsure_never_fires(self):
        night = self.bridge.run(self.ev, {"night"}, "A man stands at the door.", "Front Door")
        self.assertEqual(sorted((r["automation"], r["device"]) for r in night),
                         [("Night light (AI off)", "hallway-light"), ("Visitor at night", "hallway-light"),
                          ("Visitor at night", "living-room-light"), ("Visitor at night", "porch-light")])
        self.assertEqual(self.bridge.run(self.ev, {"activity"}, "A man stands at the door.", "Front Door"), [])
        self.assertEqual(self.bridge.run(self.ev, {"activity"}, "Something moved.", "Front Door"), [])
        crowd = self.bridge.run({"id": "e2", "start_ms": 1_000_000_000 + 300_000}, {"night"}, "Two men and a woman.", "Front Door")
        self.assertEqual({r["automation"] for r in crowd}, {"Night light (AI off)", "Visitor at night"})  # plural "men"

    def test_night_without_an_answer_lights_the_hallway(self):
        """The house never goes silent because the AI is down: no description, or no answer, still lights the hallway."""
        results = self.bridge.run(self.ev, {"night"}, None, "Front Door")
        self.assertEqual([(r["automation"], r["device"], r["command"]) for r in results],
                         [("Night light (AI off)", "hallway-light", {"action": "on", "duration_s": 300})])
        self.assertTrue(self.state("hallway-light")["on"])
        self.assertEqual(self.state("hallway-light")["last"]["label"], "Night motion")
        self.assertEqual(self.bridge.run(self.ev, {"activity"}, None, "Front Door"), [])  # by day: nothing

    def test_visitor_flash_does_not_repeat_within_a_minute(self):
        def at(seconds):
            return {"id": f"e{seconds}", "start_ms": 1_000_000_000 + seconds * 1000}
        said = "A man stands at the door."
        self.assertEqual(len(self.bridge.run(at(0), {"night"}, said, "Front Door")), 4)  # 3 flashes + hallway on
        second = self.bridge.run(at(30), {"night"}, said, "Front Door")
        self.assertEqual([r["automation"] for r in second], ["Night light (AI off)"])
        third = self.bridge.run(at(61), {"night"}, said, "Front Door")
        self.assertEqual(sorted(r["automation"] for r in third),
                         ["Night light (AI off)", "Visitor at night", "Visitor at night", "Visitor at night"])

    def test_failed_publish_is_reported_not_raised(self):
        class Broken:
            def publish(self, topic, payload):
                raise ConnectionError("broker down")
        results = Bridge(CONFIG, Broken(), self.clock).run(self.ev, {"doorbell"}, None, "Front Door")
        self.assertFalse(results[0]["ok"])
        self.assertIn("broker down", results[0]["error"])
        self.assertTrue(describe_action(results[0]).endswith("FAILED"))

    def test_payload_shape(self):
        self.bridge.run(self.ev, {"doorbell"}, None, "Front Door")
        payload = self.pub.sent[0][1]
        self.assertEqual({k: payload[k] for k in ("action", "times", "automation", "event_id", "source", "label")},
                         {"action": "flash", "times": 3, "automation": "Visual doorbell",
                          "event_id": "e1", "source": "Front Door", "label": "Doorbell!"})


class DeciderBridgeTest(unittest.TestCase):
    """With a Decider answer, `seen`, `time` and `unsure` pick the automations; keywords are ignored."""

    def setUp(self):
        self.clock = Clock()
        self.devices = VirtualDevices(CONFIG, self.clock)
        self.pub = LocalPublisher(self.devices)
        self.bridge = Bridge(CONFIG, self.pub, self.clock, min_p=0.5)

    def run_at(self, minutes, kinds, decision, description="Something is at the door."):
        ev = {"id": f"e{minutes}", "start_ms": 1_000_000_000 + minutes * 60_000}
        return self.bridge.run(ev, kinds, description, "Front Door", decision)

    def fired(self, results):
        return sorted((r["automation"], r["device"]) for r in results)

    def test_package_fires_without_keywords(self):
        results = self.run_at(0, {"activity"}, {"label": "package", "p": 0.84}, "Something brown leans on the door.")
        self.assertEqual([(r["device"], r["decision_p"], r["skipped"]) for r in results], [("porch-light", 0.84, False)])
        self.assertIn("Decider p=0.84", describe_action(results[0]))

    def test_keywords_are_ignored_when_decider_answers(self):
        self.assertEqual(self.run_at(0, {"activity"}, {"label": "nothing", "p": 0.57}, "A pizza box lies on the steps."), [])

    def test_person_at_night_flashes_three_lights_and_by_day_pulses(self):
        night = self.run_at(0, {"night"}, {"label": "person", "p": 0.9})
        self.assertEqual(self.fired(night), [("Visitor at night", "hallway-light"), ("Visitor at night", "living-room-light"),
                                             ("Visitor at night", "porch-light")])
        self.assertTrue(all(r["command"] == {"action": "flash", "times": 3} for r in night))
        day = self.run_at(1, {"activity"}, {"label": "person", "p": 0.9})
        self.assertEqual(self.fired(day), [("Visitor by day", "living-room-light")])
        self.assertEqual(day[0]["command"], {"action": "pulse", "duration_s": 10})

    def test_vehicle_only_at_night(self):
        self.assertEqual(self.fired(self.run_at(0, {"night"}, {"label": "vehicle", "p": 0.92})),
                         [("Night vehicle floodlight", "floodlight")])
        self.assertEqual(self.run_at(1, {"activity"}, {"label": "vehicle", "p": 0.92}), [])

    def test_unsure_pulses_whatever_the_label(self):
        for minutes, label in enumerate(["person", "package", "nothing"]):
            results = self.run_at(minutes, {"night"}, {"label": label, "p": 0.43})
            self.assertEqual([(r["automation"], r["decision_p"]) for r in results], [("Not sure", 0.43)])
        lamp = next(d for d in self.devices.snapshot() if d["id"] == "living-room-light")
        self.assertTrue(lamp["pulsing"])
        self.assertEqual(lamp["last"]["label"], "Not sure")

    def test_cutoff_is_inclusive(self):
        self.assertEqual(self.fired(self.run_at(0, {"activity"}, {"label": "package", "p": 0.5})),
                         [("Package porch light", "porch-light")])
        self.assertEqual(self.fired(self.run_at(20, {"activity"}, {"label": "package", "p": 0.49})),
                         [("Not sure", "living-room-light")])

    def test_confident_animal_or_nothing_does_nothing(self):
        self.assertEqual(self.run_at(0, {"night"}, {"label": "animal", "p": 0.83}), [])  # with Decider on, a cat does not light the hallway
        self.assertEqual(self.run_at(1, {"night"}, {"label": "nothing", "p": 0.6}), [])

    def test_visitor_cooldown_with_the_decider(self):
        person = {"label": "person", "p": 0.9}
        self.assertEqual(len(self.bridge.run({"id": "a", "start_ms": 1_000_000_000}, {"night"}, "x", "Front Door", person)), 3)
        self.assertEqual(self.bridge.run({"id": "b", "start_ms": 1_000_000_000 + 30_000}, {"night"}, "x", "Front Door", person), [])
        self.assertEqual(len(self.bridge.run({"id": "c", "start_ms": 1_000_000_000 + 61_000}, {"night"}, "x", "Front Door", person)), 3)

    def test_doorbell_ignores_the_decision(self):
        results = self.run_at(0, {"doorbell", "night"}, {"label": "person", "p": 0.2})
        self.assertEqual(self.fired(results), [("Visual doorbell", "living-room-light")])
        self.assertIsNone(results[0]["decision_p"])

    def test_cooldown_still_applies(self):
        self.assertEqual(len(self.run_at(0, {"activity"}, {"label": "package", "p": 0.9})), 1)
        self.assertEqual(self.run_at(5, {"activity"}, {"label": "package", "p": 0.9}), [])  # 600 s cooldown
        self.assertEqual(len(self.run_at(11, {"activity"}, {"label": "package", "p": 0.9})), 1)

    def test_decision_p_round_trips_through_store(self):
        store = Store(":memory:")
        store.add_actions("e0", self.run_at(0, {"activity"}, {"label": "package", "p": 0.84}))
        saved = store.actions_for(["e0"])["e0"][0]
        self.assertEqual((saved["skipped"], saved["decision_p"]), (False, 0.84))


NIGHT_MS = int(datetime(2026, 10, 8, 23, 30, tzinfo=TZ).timestamp() * 1000)
DAY_MS = int(datetime(2026, 10, 8, 14, 5, tzinfo=TZ).timestamp() * 1000)


class DeciderFlowTest(unittest.TestCase):
    """app.handle_event asks Decider once per described motion event and hands the answer to the bridge."""

    class Decide:
        def __init__(self, answer):
            self.answer, self.calls = answer, []

        def choose(self, state, instructions, options):
            self.calls.append((state, instructions, options))
            return self.answer

    class Alerter:
        def __init__(self):
            self.sent = []

        def send(self, subject, body):
            self.sent.append((subject, body))
            return True

    def handle(self, event_type, start_ms, description, answer, sub_type=None):
        store, alerter, decide = Store(":memory:"), self.Alerter(), self.Decide(answer)
        ev = {"id": "e1", "device_id": "d1", "start_ms": start_ms, "end_ms": start_ms + 10_000,
              "event_type": event_type, "sub_type": sub_type, "source": "poll"}
        store.begin(ev, "Front Door")
        app.handle_event(ev, "Front Door", store, alerter, Settings(), TZ, describe=lambda e: description,
                         bridge=Bridge(CONFIG, LocalPublisher(VirtualDevices(CONFIG))), decide=decide)
        row = store.db.execute("SELECT decision, decision_p FROM events WHERE id='e1'").fetchone()
        automations = [r[0] for r in store.db.execute("SELECT automation FROM actions ORDER BY id")]
        return decide, tuple(row), automations, alerter.sent[0][1]

    def test_night_person_is_asked_once_stored_flashed_and_emailed(self):
        answer = {"label": "person", "p": 0.9, "probabilities": {"person": 0.9}}
        decide, decision, automations, body = self.handle("motion_detected", NIGHT_MS, "A man stands at the door.",
                                                          answer, sub_type="human")
        self.assertEqual(decide.calls, [('Doorbell camera "Front Door" saw motion at 23:30 (night). '
                                         "Ring's label: human. AI description of the camera frame: A man stands at the door.",
                                         SEEN_QUESTION, SEEN_OPTIONS)])
        self.assertEqual(decision, ("person", 0.9))
        self.assertEqual(automations, ["Visitor at night"] * 3)
        self.assertIn("Decider: person (0.90)", body)

    def test_day_state_text(self):
        decide, *_ = self.handle("motion_detected", DAY_MS, "A cat.", {"label": "animal", "p": 0.8, "probabilities": {}})
        self.assertIn("saw motion at 14:05 (day). Ring's label: none.", decide.calls[0][0])

    def test_doorbell_is_never_asked(self):
        decide, decision, automations, body = self.handle("button_press", NIGHT_MS, "A man stands at the door.",
                                                          {"label": "person", "p": 0.9, "probabilities": {}})
        self.assertEqual((decide.calls, decision, automations), ([], (None, None), ["Visual doorbell"]))
        self.assertNotIn("Decider:", body)

    def test_no_description_is_never_asked(self):
        decide, decision, automations, _ = self.handle("motion_detected", NIGHT_MS, None,
                                                       {"label": "person", "p": 0.9, "probabilities": {}})
        self.assertEqual((decide.calls, decision, automations), ([], (None, None), ["Night light (AI off)"]))

    def test_live_view_is_never_asked(self):
        decide, decision, automations, body = self.handle("on_demand", NIGHT_MS, "A man stands at the door.",
                                                          {"label": "person", "p": 0.9, "probabilities": {}})
        self.assertEqual((decide.calls, decision), ([], (None, None)))
        self.assertNotIn("Decider:", body)

    def test_decider_down_falls_back_to_keywords(self):
        decide, decision, automations, body = self.handle("motion_detected", NIGHT_MS, "A man stands at the door.", None)
        self.assertEqual(len(decide.calls), 1)
        self.assertEqual((decision, automations), ((None, None), ["Visitor at night"] * 3 + ["Night light (AI off)"]))
        self.assertNotIn("Decider:", body)


class DoorbellFirstTest(unittest.TestCase):
    """A doorbell press flashes before the AI looks at the picture: a deaf user must see the press now."""

    def handle(self, event_type):
        order, store, emails = [], Store(":memory:"), []

        class Lights:
            def publish(self, topic, payload):
                order.append("light")

        class Alerter:
            def send(self, subject, body):
                emails.append(body)
                return True

        def describe(e):
            recorded = store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
            order.append(f"describe (actions recorded: {recorded})")
            return "A man stands at the door."

        self.emails = emails
        ev = {"id": "e1", "device_id": "d1", "start_ms": NIGHT_MS, "end_ms": NIGHT_MS + 10_000,
              "event_type": event_type, "sub_type": None, "source": "poll"}
        store.begin(ev, "Front Door")
        app.handle_event(ev, "Front Door", store, Alerter(), Settings(), TZ, describe=describe, bridge=Bridge(CONFIG, Lights()))
        return order, store

    def test_doorbell_flashes_before_the_ai_describes(self):
        order, store = self.handle("button_press")
        self.assertEqual(order, ["light", "describe (actions recorded: 1)"])  # flashed and on the dashboard first
        self.assertEqual([r[0] for r in store.db.execute("SELECT automation FROM actions")], ["Visual doorbell"])
        self.assertEqual(store.db.execute("SELECT description FROM events").fetchone()[0], "A man stands at the door.")
        self.assertIn("Living room light flash x3 (Visual doorbell)", self.emails[0])  # still in the email

    def test_motion_is_described_before_its_lights(self):
        order, _ = self.handle("motion_detected")
        self.assertEqual(order[0], "describe (actions recorded: 0)")
        self.assertIn("light", order)


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
    """Fake Ring server plays night_visit.json; a stub 'Bedrock' describes each event from its scenario image. No Decider: keyword fallback."""

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

        fired = dict(store.db.execute("SELECT automation, COUNT(*) FROM actions GROUP BY automation").fetchall())
        self.assertEqual(fired, {"Visual doorbell": 1,              # 22:52 button_press
                                 "Package porch light": 2,          # 22:50 and 23:05 ("box"); 23:12 in cooldown
                                 "Night vehicle floodlight": 1,     # 23:11 car, back yard
                                 "Visitor at night": 3,             # 23:12 courier, back yard: three lights flash
                                 # night events without an answer: 23:05, 23:10, 23:11, 23:12 and the 23:40 live view
                                 "Night light (AI off)": 5})
        doorbell = next(b for s, b in bodies if s.startswith("PorchLink: Doorbell"))
        self.assertIn("Home actions:", doorbell)
        self.assertIn("Living room light flash x3 (Visual doorbell)", doorbell)

    def test_scenario_with_decider_picks_the_patterns(self):
        ring = self.start()
        where = {e["id"]: (e["device"], os.path.basename(e["image_path"] or "")) for e in self.fake.events}
        text = {("sim.device.front", "person.jpg"): "A courier is holding a box at the door.",
                ("sim.device.front", "package.jpg"): "A box sits on the doorstep.",
                ("sim.device.back", "empty_porch.jpg"): "Nothing notable.",
                ("sim.device.back", "vehicle.jpg"): "A silver car is parked in the driveway.",
                ("sim.device.back", "person.jpg"): "A figure in a dark coat stands by the fence."}
        answers = {"A courier is holding a box at the door.": ("person", 0.9), "A box sits on the doorstep.": ("package", 0.93),
                   "Nothing notable.": ("nothing", 0.85), "A silver car is parked in the driveway.": ("vehicle", 0.92),
                   "A figure in a dark coat stands by the fence.": ("person", 0.45)}

        class Decide:
            def __init__(self):
                self.asked = []

            def choose(self, state, instructions, options):
                self.asked.append(state)
                label, p = next(v for k, v in answers.items() if state.endswith(k))
                return {"label": label, "p": p, "probabilities": {label: p}}

        class Alerter:
            def send(self, subject, body):
                return True

        store, decide = Store(":memory:"), Decide()
        bridge = Bridge(CONFIG, LocalPublisher(VirtualDevices(CONFIG)))
        worker = EventWorker(store, app.make_process(store, Alerter(), Settings(), TZ, bridge=bridge, decide=decide,
                                                     describe=lambda ev: text.get(where.get(ev["id"]))))
        app.poll_once(ring, store, worker.handle)

        fired = dict(store.db.execute("SELECT automation, COUNT(*) FROM actions GROUP BY automation").fetchall())
        self.assertEqual(fired, {"Visitor by day": 1,             # 22:50 courier, before the 23:00 night window
                                 "Visual doorbell": 1,            # 22:52, Decider not asked
                                 "Package porch light": 1,        # 23:05
                                 "Night vehicle floodlight": 1,   # 23:11
                                 "Not sure": 1,                   # 23:12 figure by the fence, p 0.45
                                 "Night light (AI off)": 1})      # 23:40 live view: never asked, so no answer
        self.assertEqual(len(decide.asked), 5)  # not the doorbell press, not the live view (no description)
        rows = store.db.execute("SELECT decision, decision_p FROM events WHERE decision IS NOT NULL ORDER BY start_ms")
        self.assertEqual([tuple(r) for r in rows], [("person", 0.9), ("package", 0.93), ("nothing", 0.85),
                                                    ("vehicle", 0.92), ("person", 0.45)])


if __name__ == "__main__":
    unittest.main()

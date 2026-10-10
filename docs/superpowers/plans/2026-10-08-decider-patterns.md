# Decider Picks the Light Pattern Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** For each described motion event, Strands Decider answers one question ("what is at the door?"), and simple rules turn the answer and the time of day into a light pattern. When Decider is unsure, the house gives a gentle pulse. A doorbell press always flashes, without AI.

**Architecture:**
- `decider.py` gains `choose()` (one 5-option `choice` question) and the shared question, options, state text and cutoff.
- `app.handle_event` asks once per event and stores the answer on the event (`decision`, `decision_p`). It hands the answer to `Bridge.run`, where `matches()` understands the new `seen` / `time` / `unsure` conditions.
- The dashboard chip and the email show the answer.
- An eval script scores Decider on 42 labelled descriptions.

**Tech Stack:** Python 3 standard library only (`urllib`, `sqlite3`, `unittest`). The Strands Decider server runs separately in `.venv-decider`.

**Spec:** `docs/superpowers/specs/2026-10-08-decider-patterns-design.md`

## Global Constraints

- PorchLink stays standard-library only. Never import torch or strands in the app or tests.
- **Question** (exact): `What is at the door in this camera frame?`
- **Options** (exact, in this order):
  - `package`: `a package, parcel, box or envelope left at the door`
  - `person`: `a person is visible`
  - `vehicle`: `a real car, truck or van is visible`
  - `animal`: `an animal is visible and no person`
  - `nothing`: `no person, animal, vehicle or package: empty scene, shadows, signs, toys or food`
- **Cutoff:** `DEFAULT_MIN_P = 0.5`. `DECIDER_MIN_P` overrides it. Sure means p ≥ min_p.
- **State text** (exact): `Doorbell camera "<name>" saw motion at <HH:MM> (<day|night>). Ring's label: <sub_type or none>. AI description of the camera frame: <description>`
- **Formats:**
  - dashboard chip text: `Decider: <label> <p:.2f>`
  - email line: `Decider: <label> (<p:.2f>)`
- Decider is never asked about a doorbell press (alert kind `doorbell`), or about an event with no description.
- If Decider is off or fails, rules fall back to `description_has` keywords. A `seen` rule with no keywords does not fire, and `unsure` never fires.
- **Full suite**, which must pass after every task: `python3 -m unittest test_porchlink test_sim test_vision test_bridge test_decider test_webhooks test_worker test_web test_dashboard` (Task 5 adds `test_eval_decider`).
- Don't change `webhooks.py`, `worker.py` or `sim/`. Never commit `.env`.
- Match the surrounding style: short docstrings, sparse comments, no type hints.

---

### Task 1: `DeciderClient.choose` and the shared question

**Files:**
- Modify: `decider.py`
- Test: `test_decider.py`

**Interfaces:**
- Produces:
  - `decider.DEFAULT_MIN_P` (0.5), `decider.SEEN_QUESTION` (str), `decider.SEEN_OPTIONS` (dict, ordered).
  - `decider.state_text(camera, clock, night, ring_label, description) -> str`.
  - `DeciderClient.choose(state, instructions, options) -> {"label": str, "p": float (2 dp), "probabilities": {option: float}} | None`.
- `DeciderClient.__call__` stays for now; the app still uses it for warm-up until Task 3 removes it.

- [ ] **Step 1: Write the failing tests.** Add to `test_decider.py`. Change the import line to `from decider import DEFAULT_MIN_P, SEEN_OPTIONS, SEEN_QUESTION, DeciderClient, state_text`, then add this class above `if __name__`:

```python
class ChooseTest(unittest.TestCase):
    ANSWER = {"answers": {"seen": {"type": "choice", "choice": "package", "confidence": 0.8,
                                   "probabilities": {"package": 0.834, "person": 0.1, "vehicle": 0.02,
                                                     "animal": 0.02, "nothing": 0.026}}}}

    def test_sends_one_choice_question_and_returns_the_top_option(self):
        opener = FakeOpener(self.ANSWER)
        d = DeciderClient({"DECIDER_URL": "http://box:9000/"}, opener).choose("state text", SEEN_QUESTION, SEEN_OPTIONS)
        self.assertEqual((d["label"], d["p"]), ("package", 0.83))
        self.assertEqual(set(d["probabilities"]), set(SEEN_OPTIONS))
        url, body, _ = opener.requests[0]
        self.assertEqual(url, "http://box:9000/v1/systemone")
        self.assertEqual(body, {"state": "state text", "questions": {"seen": {
            "type": "choice", "instructions": SEEN_QUESTION, "criteria": SEEN_OPTIONS}}})

    def test_any_failure_returns_none(self):
        options = {"a": "first", "b": "second"}
        self.assertIsNone(DeciderClient({}, FakeOpener(error=ConnectionRefusedError("down"))).choose("s", "?", options))
        self.assertIsNone(DeciderClient({}, FakeOpener({"answers": {}})).choose("s", "?", options))
        partial = FakeOpener({"answers": {"seen": {"probabilities": {"a": 0.9}}}})  # option b has no probability
        self.assertIsNone(DeciderClient({}, partial).choose("s", "?", options))

    def test_shipped_question(self):
        self.assertEqual(list(SEEN_OPTIONS), ["package", "person", "vehicle", "animal", "nothing"])
        self.assertEqual(DEFAULT_MIN_P, 0.5)

    def test_state_text(self):
        self.assertEqual(state_text("Front Door", "23:30", True, "human", "A man stands at the door."),
                         'Doorbell camera "Front Door" saw motion at 23:30 (night). Ring\'s label: human. '
                         "AI description of the camera frame: A man stands at the door.")
        self.assertIn("at 14:05 (day). Ring's label: none.", state_text("Back Yard", "14:05", False, None, "A cat."))
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python3 -m unittest test_decider -v`
Expected: ImportError (`DEFAULT_MIN_P` / `state_text` not defined).

- [ ] **Step 3: Implement.** In `decider.py`, after `import json, urllib.request`, add:

```python
DEFAULT_MIN_P = 0.5  # top-option p below this means "not sure"; chosen by the probe in the 2026-10-08 Decider spec
SEEN_QUESTION = "What is at the door in this camera frame?"
SEEN_OPTIONS = {
    "package": "a package, parcel, box or envelope left at the door",
    "person": "a person is visible",
    "vehicle": "a real car, truck or van is visible",
    "animal": "an animal is visible and no person",
    "nothing": "no person, animal, vehicle or package: empty scene, shadows, signs, toys or food",
}


def state_text(camera, clock, night, ring_label, description):
    """The `state` Decider reads: which camera, when, Ring's own label and the AI description of the frame."""
    return (f'Doorbell camera "{camera}" saw motion at {clock} ({"night" if night else "day"}). '
            f"Ring's label: {ring_label or 'none'}. AI description of the camera frame: {description}")
```

Add this method to `DeciderClient`, after `__init__`:

```python
    def choose(self, state, instructions, options):
        """Ask one multiple-choice question ({option: criterion}). Returns the most likely option as
        {"label", "p", "probabilities"}, or None on any failure (the bridge then falls back to keywords)."""
        body = {"state": state, "questions": {"seen": {"type": "choice", "instructions": instructions, "criteria": options}}}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                probs = json.loads(resp.read())["answers"]["seen"]["probabilities"]
            probs = {name: float(probs[name]) for name in options}
        except Exception as e:
            print(f"(decider skipped: {type(e).__name__}: {e})")
            return None
        label = max(options, key=probs.get)
        return {"label": label, "p": round(probs[label], 2), "probabilities": probs}
```

Also change the `DECIDER_TIMEOUT` comment to: `# Apple MPS: about 0.4 s per call; CPU-only: about 10 s, first call 86 s`.

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `python3 -m unittest test_decider -v`. Expected: all pass, including the 2 existing tests.
Then run the full suite from Global Constraints. Expected: OK.

- [ ] **Step 5: Commit.**

```bash
git add decider.py test_decider.py
git commit -m "feat(decider): choose() asks one 5-option question; shared question, options, state text and 0.5 cutoff"
```

---

### Task 2: Rules understand `seen`, `time` and `unsure`; new shipped automations

**Files:**
- Modify: `bridge.py` (`matches`, `Bridge.__init__`, `Bridge.plan`, `Bridge.run`; remove `_without_text` and `_state`)
- Modify: `automations.json`
- Modify: `app.py` (only the `Bridge(...)` line in `main()` and one import)
- Test: `test_bridge.py`

**Interfaces:**
- Consumes: `decider.DEFAULT_MIN_P` (Task 1).
- Produces:
  - `matches(when, kinds, description, source_name, decision=None, min_p=DEFAULT_MIN_P) -> bool`.
  - `Bridge(config, publisher, clock=time.time, min_p=DEFAULT_MIN_P)`. The `decide=` parameter is removed.
  - `Bridge.plan(kinds, description, source_name, at, decision=None) -> [(automation, step, decision_p)]`.
  - `Bridge.run(ev, kinds, description, source_name, decision=None) -> [result]`. Each result has `decision_p`: the decision's p when the automation used `seen`/`unsure`, else None. `skipped` is always False now.
- `decision` is `{"label": str, "p": float, ...}` (the `choose` result) or None.
- Between this task and Task 3, nothing passes a decision yet, so the app runs on keyword fallback. That is expected.

- [ ] **Step 1: Write the failing tests** in `test_bridge.py`.

(a) Add to `MatchTest`:

```python
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

    def test_without_a_decision_seen_falls_back_to_keywords(self):
        self.assertTrue(matches({"seen": ["package"], "description_has": ["box"]}, set(), "A box.", "x"))
        self.assertFalse(matches({"seen": ["package"], "description_has": ["box"]}, set(), "A cat.", "x"))
        self.assertFalse(matches({"seen": ["person"]}, set(), "A person.", "x"))  # no keywords to fall back on
```

(b) In `BridgeTest`, replace `test_night_turns_hallway_on_then_it_times_out`, `test_night_vehicle_fires_two_automations` and `test_payload_shape` with the code below. In `test_failed_publish_is_reported_not_raised`, change `{"night"}, None` to `{"doorbell"}, None`.

```python
    def test_package_light_turns_on_then_times_out(self):
        results = self.bridge.run(self.ev, {"activity"}, "A box is on the doorstep.", "Front Door")
        self.assertEqual([r["device"] for r in results], ["porch-light"])
        self.assertEqual(self.pub.sent[0][0], "porchlink/porch-light/set")
        self.assertTrue(self.state("porch-light")["on"])
        self.clock.t += 601
        self.assertFalse(self.state("porch-light")["on"])

    def test_night_vehicle_by_keywords(self):
        results = self.bridge.run(self.ev, {"night"}, "A silver car is parked in the driveway.", "Back Yard")
        self.assertEqual([r["device"] for r in results], ["floodlight"])
        self.assertEqual(self.bridge.run(self.ev, {"activity"}, "A silver car is parked.", "Back Yard"), [])  # day

    def test_without_decider_visitor_uses_keywords_and_unsure_never_fires(self):
        night = self.bridge.run(self.ev, {"night"}, "A man stands at the door.", "Front Door")
        self.assertEqual(sorted(r["device"] for r in night), ["hallway-light", "living-room-light", "porch-light"])
        self.assertEqual({r["automation"] for r in night}, {"Visitor at night"})
        self.assertEqual(self.bridge.run(self.ev, {"activity"}, "A man stands at the door.", "Front Door"), [])
        self.assertEqual(self.bridge.run(self.ev, {"night"}, "Something moved.", "Front Door"), [])

    def test_payload_shape(self):
        self.bridge.run(self.ev, {"doorbell"}, None, "Front Door")
        payload = self.pub.sent[0][1]
        self.assertEqual({k: payload[k] for k in ("action", "times", "automation", "event_id", "source", "label")},
                         {"action": "flash", "times": 3, "automation": "Visual doorbell",
                          "event_id": "e1", "source": "Front Door", "label": "Doorbell!"})
```

(c) Replace the whole `DeciderBridgeTest` class with:

```python
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
        self.assertEqual(self.run_at(0, {"night"}, {"label": "animal", "p": 0.83}), [])
        self.assertEqual(self.run_at(1, {"night"}, {"label": "nothing", "p": 0.6}), [])

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
```

(d) In `EndToEndTest.test_scenario_drives_the_lights`, replace the four `fired.get(...)` assertions with:

```python
        self.assertEqual(fired.get("Visual doorbell"), 1)           # 22:52 button_press
        self.assertEqual(fired.get("Package porch light"), 2)       # 22:50 and 23:05 ("box"); 23:12 in cooldown
        self.assertEqual(fired.get("Night vehicle floodlight"), 1)  # 23:11 car, back yard
        self.assertEqual(fired.get("Visitor at night"), 3)          # 23:12 courier, back yard: three lights flash
        self.assertNotIn("Visitor by day", fired)                   # needs Decider
        self.assertNotIn("Not sure", fired)                         # needs Decider
```

Also change the class docstring line `Fake Ring server plays night_visit.json; a stub 'Bedrock' describes each event from its scenario image.` to `Fake Ring server plays night_visit.json; a stub 'Bedrock' describes each event from its scenario image. No Decider: keyword fallback.`

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python3 -m unittest test_bridge -v`
Expected: failures and errors, such as the `matches()` arity, the unknown `Visitor at night`, and `Bridge(... min_p=)`.

- [ ] **Step 3: Replace `automations.json`.** Keep `devices`; the `automations` list becomes:

```json
  "automations": [
    {"name": "Visual doorbell",
     "why": "Flash the lights for people who cannot hear the bell.",
     "label": "Doorbell!",
     "when": {"kind": ["doorbell"]},
     "do": [{"device": "living-room-light", "action": "flash", "times": 3}]},

    {"name": "Package porch light",
     "why": "Make a delivered package easy to see and hard to take unnoticed.",
     "label": "Package",
     "when": {"seen": ["package"], "description_has": ["package", "box", "parcel"]},
     "do": [{"device": "porch-light", "action": "on", "duration_s": 600}],
     "cooldown_s": 600},

    {"name": "Visitor at night",
     "why": "Someone is outside in the dark: make sure the whole house notices.",
     "label": "Someone outside",
     "when": {"seen": ["person"], "time": "night", "description_has": ["person", "people", "man", "woman", "courier"]},
     "do": [{"device": "hallway-light", "action": "flash", "times": 3},
            {"device": "living-room-light", "action": "flash", "times": 3},
            {"device": "porch-light", "action": "flash", "times": 3}]},

    {"name": "Visitor by day",
     "why": "Someone is at the door: a gentle signal, not an alarm.",
     "label": "Visitor",
     "when": {"seen": ["person"], "time": "day"},
     "do": [{"device": "living-room-light", "action": "pulse", "duration_s": 10}]},

    {"name": "Night vehicle floodlight",
     "why": "Light up the driveway when a car arrives at night.",
     "label": "Car arriving",
     "when": {"seen": ["vehicle"], "time": "night", "description_has": ["car", "vehicle", "truck", "van"]},
     "do": [{"device": "floodlight", "action": "on", "duration_s": 180}]},

    {"name": "Not sure",
     "why": "The AI could not tell what is outside: a gentle pulse says 'check the camera'.",
     "label": "Not sure",
     "when": {"unsure": true},
     "do": [{"device": "living-room-light", "action": "pulse", "duration_s": 10}]}
  ]
```

- [ ] **Step 4: Implement in `bridge.py`.**

Add `from decider import DEFAULT_MIN_P` after `import json, threading, time`.

Replace `matches` with:

```python
def matches(when, kinds, description, source_name, decision=None, min_p=DEFAULT_MIN_P):
    """All conditions in `when` must hold; each condition is satisfied by any of its values.
    `seen` and `unsure` use Decider's answer (`decision`, {"label", "p"}) and never fire for a doorbell press.
    With an answer, a `seen` automation ignores its `description_has` keywords; without one it falls back to
    them (and does not fire if it has none), and `unsure` never fires."""
    if "kind" in when and not set(when["kind"]) & kinds:
        return False
    if "time" in when and (when["time"] == "night") != ("night" in kinds):
        return False
    if "source" in when and source_name not in when["source"]:
        return False
    if ("seen" in when or when.get("unsure")) and "doorbell" in kinds:
        return False
    if when.get("unsure"):
        return decision is not None and decision["p"] < min_p
    if "seen" in when and decision is not None:
        return decision["label"] in when["seen"] and decision["p"] >= min_p
    if "seen" in when and "description_has" not in when:
        return False
    if "description_has" in when:
        text = (description or "").lower()
        if not any(word in text for word in when["description_has"]):
            return False
    return True
```

Replace `Bridge.__init__`, `plan` and `run` with:

```python
class Bridge:
    def __init__(self, config, publisher, clock=time.time, min_p=DEFAULT_MIN_P):
        self.config, self.publisher, self.clock = config, publisher, clock
        self.min_p = min_p  # Decider's top option needs at least this p for a `seen` automation
        self.devices = {d["id"]: d for d in config["devices"]}
        self.last_fired = {}

    def plan(self, kinds, description, source_name, at, decision=None):
        """Return (automation, step, decision_p) triples to run for an event at time `at` (epoch seconds).
        decision is Decider's answer or None; decision_p is its p for automations that used it (`seen` or
        `unsure`), else None. Cooldowns use event time, so a backlog processed at once is not wrongly suppressed."""
        out = []
        for a in self.config["automations"]:
            cooldown = a.get("cooldown_s", 0)
            # abs(): events from different cameras can arrive out of time order
            if cooldown and a["name"] in self.last_fired and abs(at - self.last_fired[a["name"]]) < cooldown:
                continue
            if not matches(a["when"], kinds, description, source_name, decision, self.min_p):
                continue
            self.last_fired[a["name"]] = at
            used = decision is not None and ("seen" in a["when"] or a["when"].get("unsure"))
            out += [(a, step, decision["p"] if used else None) for step in a["do"]]
        return out

    def run(self, ev, kinds, description, source_name, decision=None):
        """Send the commands for one event. Returns a list of result dicts (for the store, email and UI)."""
        results = []
        at = ev["start_ms"] / 1000 if ev.get("start_ms") else self.clock()
        for automation, step, decision_p in self.plan(kinds, description, source_name, at, decision):
            command = {k: v for k, v in step.items() if k != "device"}
            topic = f"porchlink/{step['device']}/set"
            base = {"automation": automation["name"], "device": step["device"],
                    "device_name": self.devices[step["device"]]["name"], "command": command, "topic": topic,
                    "decision_p": decision_p}
            payload = {**command, "automation": automation["name"], "event_id": ev["id"],
                       "source": source_name, "sent_at": int(self.clock() * 1000)}
            if automation.get("label"):
                payload["label"] = automation["label"]  # what the dashboard tile says ("Doorbell!", "Package")
            try:
                self.publisher.publish(topic, payload)
                ok, error = True, None
            except Exception as e:  # one failed device must not stop the others or the email
                ok, error = False, f"{type(e).__name__}: {e}"
                print(f"(action failed: {topic}: {error})")
            results.append({**base, "ok": ok, "error": error, "skipped": False})
        return results
```

Delete `_without_text` and `_state`. Leave `describe_action` unchanged; its `skipped` branch still renders old rows.

In `app.py`:
- Add `from decider import DEFAULT_MIN_P` after the `from bridge import ...` line.
- Replace `bridge = Bridge(config, publisher, decide=decide, min_p=float(env.get("DECIDER_MIN_P", 0.8)))` with `bridge = Bridge(config, publisher, min_p=float(env.get("DECIDER_MIN_P", DEFAULT_MIN_P)))`.

- [ ] **Step 5: Run the tests and confirm they pass.**

Run: `python3 -m unittest test_bridge -v`. Expected: all pass.
Then run the full suite from Global Constraints. Expected: OK. `test_dashboard`'s "every automation has a label" test also covers the new rules.

- [ ] **Step 6: Commit.**

```bash
git add bridge.py automations.json app.py test_bridge.py
git commit -m "feat(bridge): seen/time/unsure conditions pick the light pattern; visitor and not-sure rules replace night path"
```

---

### Task 3: The worker asks Decider once per event, stores the answer, and emails it

**Files:**
- Modify: `app.py` (`handle_event`, `make_process`, `main` warm-up)
- Modify: `store.py` (event columns `decision`, `decision_p`, and `set_decision`)
- Modify: `rules.py` (`format_alert` gains `decision=None`)
- Modify: `decider.py` (remove `__call__`; update the module docstring)
- Test: `test_bridge.py` (new `DeciderFlowTest`, plus a Decider end-to-end test), `test_decider.py` (remove the 2 `__call__` tests)

**Interfaces:**
- Consumes:
  - `decider.SEEN_QUESTION`, `SEEN_OPTIONS`, `state_text` and `DeciderClient.choose` (Task 1);
  - `Bridge.run(ev, kinds, description, source_name, decision=None)` (Task 2).
- Produces:
  - `app.handle_event(ev, name, store, alerter, settings, tz, describe=None, bridge=None, decide=None)`.
  - `app.make_process(store, alerter, settings, tz, describe=None, bridge=None, decide=None)`.
  - `Store.set_decision(event_id, label, p)`. `Store.timeline()` rows now include `decision` and `decision_p`.
  - `rules.format_alert(ev, name, reasons, tz, description=None, actions=(), decision=None)`.

- [ ] **Step 1: Write the failing tests.**

In `test_bridge.py`:
- Change the imports to `from datetime import datetime` plus the existing ones.
- Add `from decider import SEEN_OPTIONS, SEEN_QUESTION`.
- Add this class before `class IotCoreTest`:

```python
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
        self.assertEqual((decide.calls, decision, automations), ([], (None, None), []))

    def test_decider_down_falls_back_to_keywords(self):
        decide, decision, automations, body = self.handle("motion_detected", NIGHT_MS, "A man stands at the door.", None)
        self.assertEqual(len(decide.calls), 1)
        self.assertEqual((decision, automations), ((None, None), ["Visitor at night"] * 3))
        self.assertNotIn("Decider:", body)
```

Add this method to `EndToEndTest`:

```python
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
                                 "Not sure": 1})                  # 23:12 figure by the fence, p 0.45
        self.assertEqual(len(decide.asked), 5)  # not the doorbell press, not the live view (no description)
        rows = store.db.execute("SELECT decision, decision_p FROM events WHERE decision IS NOT NULL ORDER BY start_ms")
        self.assertEqual([tuple(r) for r in rows], [("person", 0.9), ("package", 0.93), ("nothing", 0.85),
                                                    ("vehicle", 0.92), ("person", 0.45)])
```

In `test_decider.py`, delete the `DeciderClientTest` class (the two `__call__` tests). Keep `FakeOpener` and `ChooseTest`.

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python3 -m unittest test_bridge test_decider -v`
Expected: `DeciderFlowTest` and the new end-to-end test fail (unexpected keyword argument `decide`).

- [ ] **Step 3: Implement.**

**`store.py`:** after the "Event status" migration loop in `__init__`, add:

```python
            for column, kind in (("decision", "TEXT"), ("decision_p", "REAL")):  # Decider's answer: top option and its p
                try:
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError:
                    pass
```

Then add this method after `set_description`:

```python
    def set_decision(self, event_id, label, p):
        with self.lock, self.db:
            self.db.execute("UPDATE events SET decision=?, decision_p=? WHERE id=?", (label, p, event_id))
```

**`rules.py`:** change the `format_alert` signature to `def format_alert(ev, name, reasons, tz, description=None, actions=(), decision=None):`. Then replace the line `seen = [f"Camera (AI description): {description}", ""] if description else []` with:

```python
    seen = [f"Camera (AI description): {description}"] if description else []
    if decision:
        seen.append(f"Decider: {decision['label']} ({decision['p']:.2f})")
    seen += [""] if seen else []
```

**`app.py`:**
- Change the decider import to `from decider import DEFAULT_MIN_P, SEEN_OPTIONS, SEEN_QUESTION, state_text`.
- Replace `handle_event` with:

```python
def handle_event(ev, name, store, alerter, settings, tz, describe=None, bridge=None, decide=None):
    """Apply rules to one new event, ask Decider what is at the door, run home automations, send one email,
    record everything. describe(ev) is optional (Bedrock); it runs only for events that will alert, to keep
    cost down. decide (a DeciderClient) is optional; it is asked once per described event, never for a doorbell press."""
    recent = store.starts_since(ev["device_id"], ev["start_ms"] - settings.burst_minutes * 60_000)
    reasons = evaluate(ev, recent, store.last_alert_start(ev["device_id"], "burst"), settings, tz)
    if not reasons:
        return
    kinds = {k for k, _ in reasons}
    description = describe(ev) if describe else None
    if description:
        store.set_description(ev["id"], description)
    decision = None
    if decide and description and "doorbell" not in kinds:
        local = datetime.fromtimestamp(ev["start_ms"] / 1000, tz)
        decision = decide.choose(state_text(name, f"{local:%H:%M}", "night" in kinds, ev.get("sub_type"), description),
                                 SEEN_QUESTION, SEEN_OPTIONS)
        if decision:
            store.set_decision(ev["id"], decision["label"], decision["p"])
    actions = bridge.run(ev, kinds, description, name, decision) if bridge else []
    if actions:
        store.add_actions(ev["id"], actions)
    subject, body = format_alert(ev, name, reasons, tz, description, [describe_action(a) for a in actions], decision)
    delivered = alerter.send(subject, body)
    for kind, reason in reasons:
        store.add_alert(ev, kind, reason, delivered)
```

- Replace `make_process` with:

```python
def make_process(store, alerter, settings, tz, describe=None, bridge=None, decide=None):
    """The worker's per-event step: rules, Decider, home actions, email, records."""
    def process(ev, name):
        handle_event(ev, name, store, alerter, settings, tz, describe, bridge, decide)
    return process
```

- In `main()`, replace the warm-up thread lines with:

```python
        # The first call after a server start is slow (86 s measured on CPU); take it now, not on a real event.
        threading.Thread(target=decide.choose, args=("Warm-up.", "Is this a warm-up?", {"yes": "it is", "no": "it is not"}),
                         daemon=True).start()
```

- Change the worker line to `worker = EventWorker(store, make_process(store, alerter, settings, tz, describe, bridge, decide),` and keep the next line unchanged.

**`decider.py`:**
- Delete the `__call__` method.
- Change the module docstring's first line to `"""Ask a local Strands Decider server what is at the door (POST /v1/systemone).`

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `python3 -m unittest test_bridge test_decider -v`. Expected: all pass.
Then run the full suite from Global Constraints. Expected: OK.
Also run `grep -n "decide(" *.py`. Expected: no callers of the removed `__call__`.

- [ ] **Step 5: Commit.**

```bash
git add app.py store.py rules.py decider.py test_bridge.py test_decider.py
git commit -m "feat(app): ask Decider once per described event, store the answer, pass it to the rules and the email"
```

---

### Task 4: The dashboard chip shows Decider's answer

**Files:**
- Modify: `web.py` (`summarize`, `make_server`, `serve`, the `CHIP_*` comments, and one import)
- Modify: `app.py` (`main()` passes `min_p` to `web.serve`)
- Modify: `dashboard_preview.py` (sample events use event-level decisions)
- Test: `test_dashboard.py`

**Interfaces:**
- Consumes:
  - `decider.DEFAULT_MIN_P`;
  - `Store.set_decision` and timeline rows with `decision` / `decision_p` (Task 3).
- Produces:
  - `web.summarize(e, actions, min_p=DEFAULT_MIN_P)`.
  - `web.make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None, min_p=DEFAULT_MIN_P)`. `web.serve(...)` takes the same parameters.

- [ ] **Step 1: Write the failing tests.** In `test_dashboard.py`, add this class after `SummarizeTest`:

```python
class DecisionChipTest(unittest.TestCase):
    """Events with Decider's answer (event-level decision) get a 'Decider: <label> <p>' chip."""

    def summarize(self, label, p, actions=(), min_p=0.5):
        return web.summarize({"kinds": "night", "event_type": "motion_detected", "description": "Something at the door.",
                              "decision": label, "decision_p": p}, list(actions), min_p)

    def test_confident_answer_that_fired_is_ai(self):
        s = self.summarize("package", 0.93, [action("Porch light ON for 10 min (Package porch light) (Decider p=0.93)", p=0.93)])
        self.assertEqual(s["chip"], {"kind": "ai", "text": "Decider: package 0.93"})

    def test_unsure_answer_with_its_pulse(self):
        s = self.summarize("person", 0.43, [action("Living room light gentle pulse (Not sure) (Decider p=0.43)", p=0.43)])
        self.assertEqual((s["chip"], s["detail"]), ({"kind": "unsure", "text": "Decider: person 0.43"},
                                                    "Living room light gentle pulse (Not sure) (Decider p=0.43)"))

    def test_confident_nothing_or_animal_is_a_skipped_false_alarm(self):
        for label in ("nothing", "animal"):
            s = self.summarize(label, 0.85)
            self.assertEqual((s["chip"], s["detail"]),
                             ({"kind": "skip", "text": f"Decider: {label} 0.85"}, "false alarm skipped"))

    def test_confident_package_in_cooldown_is_still_ai(self):
        s = self.summarize("package", 0.9)
        self.assertEqual((s["chip"]["kind"], s["detail"]), ("ai", "nothing done"))

    def test_cutoff_follows_min_p(self):
        self.assertEqual(self.summarize("package", 0.7, min_p=0.8)["chip"]["kind"], "unsure")
        self.assertEqual(self.summarize("nothing", 0.5)["chip"]["kind"], "skip")  # 0.5 is sure at min_p 0.5

    def test_event_decision_wins_over_old_action_p(self):
        self.assertEqual(self.summarize("package", 0.93, [action("x", p=0.2)])["chip"]["text"], "Decider: package 0.93")
```

In `ApiTest.start`, change the `make_server` call to `web.make_server(self.store, TZ, 0, None, kw.get("devices"), "", False, kw.get("status"), kw.get("min_p", 0.5))`. Then add:

```python
    def test_event_decision_round_trips_through_the_api(self):
        self.start(min_p=0.9)
        now = int(time.time() * 1000)
        for eid, label, p in (("e3", "nothing", 0.95), ("e4", "package", 0.85)):
            ev = {"id": eid, "device_id": "dev1", "start_ms": now, "end_ms": now, "event_type": "motion_detected",
                  "sub_type": None, "source": "poll"}
            self.store.begin(ev, "Back Yard")
            self.store.finish(eid, "done")
            self.store.add_alert(ev, "night", "Night.", True)
            self.store.set_decision(eid, label, p)
        chips = {e["id"]: (e["decision"], e["summary"]["chip"]) for e in json.loads(self.get("/api/events")[2])}
        self.assertEqual(chips["e3"], ("nothing", {"kind": "skip", "text": "Decider: nothing 0.95"}))
        self.assertEqual(chips["e4"], ("package", {"kind": "unsure", "text": "Decider: package 0.85"}))  # below min_p 0.9
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python3 -m unittest test_dashboard -v`
Expected: the new tests fail (`summarize()` takes 2 positional arguments; `make_server` takes too many).

- [ ] **Step 3: Implement in `web.py`.**
- Add `from decider import DEFAULT_MIN_P` after `from bridge import describe_action`.
- Replace the two `CHIP_*` lines with:

```python
CHIP_SURE = 0.8  # older rows (one yes/no Decider question per automation): below this was "not sure"
CHIP_NO = 0.5  # older rows: with nothing fired, below this was a confident "no", a skipped false alarm
```

- In `summarize`:
  - Change the signature to `def summarize(e, actions, min_p=DEFAULT_MIN_P):`.
  - Change the docstring's second line to `decided (a rule, or Decider's answer and how sure it was) and an icon key. Text is returned raw; the page escapes."""`.
  - Replace the block from `ps = [a["decision_p"] ...` through the end of the `if ps:` branch, up to and not including `elif "doorbell" in kinds:`, with:

```python
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
```

- `make_server`:
  - Change the signature to `def make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None, min_p=DEFAULT_MIN_P):`.
  - In `do_GET`, change `summarize(e, acts)` to `summarize(e, acts, min_p)`.
- `serve`:
  - Change the signature the same way, adding `min_p=DEFAULT_MIN_P`.
  - Change its body's call to `make_server(store, tz, port, enqueue, devices, webhook_secret, log_bodies, status, min_p)`.

**`app.py` `main()`:**
- Replace `bridge = Bridge(config, publisher, min_p=float(env.get("DECIDER_MIN_P", DEFAULT_MIN_P)))` with these two lines:

```python
    min_p = float(env.get("DECIDER_MIN_P", DEFAULT_MIN_P))
    bridge = Bridge(config, publisher, min_p=min_p)
```

- In the `web.serve` thread, change the args tuple's ending `webhook_secret, env.get("WEBHOOK_LOG") == "1", status)` to `webhook_secret, env.get("WEBHOOK_LOG") == "1", status, min_p)`.

**`dashboard_preview.py`:** replace the `events = [...]` list and the `for` loop in `seed` with:

```python
    events = [  # (id, minutes ago, type, description, alert kind, action, Decider's answer)
        ("p-doorbell", 0, "button_press", None, "doorbell",
         {"automation": "Visual doorbell", "device": "living-room-light", "command": {"action": "flash", "times": 3}}, None),
        ("p-unsure", 5, "motion_detected", "Something moved at the edge of the driveway.", "night",
         {"automation": "Not sure", "device": "floodlight", "command": {"action": "pulse", "duration_s": 10}}, ("person", 0.43)),
        ("p-package", 11, "motion_detected", "A cardboard package sits on the doorstep.", "activity",
         {"automation": "Package porch light", "device": "porch-light", "command": {"action": "on", "duration_s": 600}},
         ("package", 0.93)),
        ("p-cat", 22, "motion_detected", "A cat walks across the back yard.", "activity", None, ("animal", 0.83)),
    ]
    for eid, ago, kind, description, alert, act, decision in events:
        start = now_ms - ago * 60_000
        ev = {"id": eid, "device_id": "front" if eid != "p-cat" else "back", "start_ms": start, "end_ms": start,
              "event_type": kind, "sub_type": None, "source": "poll"}
        store.begin(ev, "Front Door" if eid != "p-cat" else "Back Yard")
        store.finish(eid, "done")
        if description:
            store.set_description(eid, description)
        if decision:
            store.set_decision(eid, *decision)
        store.add_alert(ev, alert, alert, True)
        if act:  # the cat has none: a confident "animal" runs nothing, shown as a skipped false alarm
            store.add_actions(eid, [{**act, "topic": f"porchlink/{act['device']}/set", "ok": True, "error": None,
                                     "decision_p": decision and decision[1]}])
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `python3 -m unittest test_dashboard -v`. Expected: all pass.
Then run the full suite from Global Constraints. Expected: OK.

Preview smoke check:

```bash
python3 dashboard_preview.py --port 8091 & sleep 2; curl -s 127.0.0.1:8091/api/events | python3 -c "import json,sys; [print(e['id'], e['summary']['chip']) for e in json.load(sys.stdin)]"; kill %1
```

Expected:

```
p-doorbell {'kind': 'rule', 'text': 'rule · no AI'}
p-unsure {'kind': 'unsure', 'text': 'Decider: person 0.43'}
p-package {'kind': 'ai', 'text': 'Decider: package 0.93'}
p-cat {'kind': 'skip', 'text': 'Decider: animal 0.83'}
```

- [ ] **Step 5: Commit.**

```bash
git add web.py app.py dashboard_preview.py test_dashboard.py
git commit -m "feat(dashboard): chip shows Decider's answer ('Decider: package 0.93'), cutoff follows DECIDER_MIN_P"
```

---

### Task 5: Eval script, 42 labelled cases, report and docs

**Files:**
- Create: `eval/decider_cases.json`
- Create: `eval_decider.py`
- Create: `test_eval_decider.py`
- Create (by running the script): `docs/decider-eval.md`
- Modify: `README.md`, `.env.example`

**Interfaces:**
- Consumes: `decider.DEFAULT_MIN_P`, `SEEN_OPTIONS`, `SEEN_QUESTION`, `state_text`, `DeciderClient.choose`.
- Produces:
  - `eval_decider.score(cases, answers, min_p) -> dict` with the keys `cases`, `right`, `failed`, `unsure`, `per_class`, `confusion`, `misses`, `p50_ms`, `p95_ms`.
  - `eval_decider.report(summary, min_p, model, hardware, day) -> str` (Markdown).
  - `eval_decider.CASES` (the path).

- [ ] **Step 1: Write the cases file** `eval/decider_cases.json`. Each case has `expect`: acceptable labels, with the first one being the class it counts under.

```json
[
  {"description": "A cardboard box sits on the doorstep.", "time": "night", "sub_type": null, "expect": ["package"]},
  {"description": "Two parcels are stacked by the front door.", "time": "day", "sub_type": null, "expect": ["package"]},
  {"description": "An Amazon envelope leans against the door.", "time": "day", "sub_type": null, "expect": ["package"]},
  {"description": "A small brown package rests on the doormat.", "time": "night", "sub_type": null, "expect": ["package"]},
  {"description": "A padded mailer lies on the welcome mat.", "time": "day", "sub_type": null, "expect": ["package"]},
  {"description": "A large box wrapped in tape is next to the door.", "time": "day", "sub_type": null, "expect": ["package"]},
  {"description": "A grocery delivery bag sits on the porch step.", "time": "day", "sub_type": null, "expect": ["package", "nothing"]},
  {"description": "A parcel with a shipping label is propped against the wall by the door.", "time": "night", "sub_type": null, "expect": ["package"]},
  {"description": "A man in a hoodie stands near the door, looking around.", "time": "night", "sub_type": "human", "expect": ["person"]},
  {"description": "A woman rings the doorbell holding flowers.", "time": "day", "sub_type": "human", "expect": ["person"]},
  {"description": "A courier is holding a box at the door.", "time": "day", "sub_type": "human", "expect": ["person", "package"]},
  {"description": "A child waves at the camera.", "time": "day", "sub_type": "human", "expect": ["person"]},
  {"description": "Two people walk up the path toward the door.", "time": "night", "sub_type": "human", "expect": ["person"]},
  {"description": "A delivery driver in a uniform walks away from the porch.", "time": "day", "sub_type": "human", "expect": ["person"]},
  {"description": "Someone in a dark coat stands by the fence.", "time": "night", "sub_type": null, "expect": ["person"]},
  {"description": "A person is crouching near the porch steps.", "time": "night", "sub_type": "human", "expect": ["person"]},
  {"description": "An older man is reading the house number.", "time": "day", "sub_type": "human", "expect": ["person"]},
  {"description": "A silver car is parked in the driveway.", "time": "night", "sub_type": null, "expect": ["vehicle"]},
  {"description": "A delivery van stops at the curb.", "time": "day", "sub_type": null, "expect": ["vehicle"]},
  {"description": "A pickup truck pulls into the driveway with its headlights on.", "time": "night", "sub_type": null, "expect": ["vehicle"]},
  {"description": "A red hatchback is reversing out of the driveway.", "time": "day", "sub_type": null, "expect": ["vehicle"]},
  {"description": "Headlights of a car sweep across the garage door.", "time": "night", "sub_type": null, "expect": ["vehicle"]},
  {"description": "A motorcycle is parked by the gate.", "time": "night", "sub_type": null, "expect": ["vehicle"]},
  {"description": "A taxi waits in front of the house with its engine running.", "time": "night", "sub_type": null, "expect": ["vehicle"]},
  {"description": "A cat walks across the back yard.", "time": "night", "sub_type": null, "expect": ["animal"]},
  {"description": "A dog sniffs the doormat.", "time": "day", "sub_type": null, "expect": ["animal"]},
  {"description": "A fox crosses the lawn.", "time": "night", "sub_type": null, "expect": ["animal"]},
  {"description": "Two birds peck at the porch steps.", "time": "day", "sub_type": null, "expect": ["animal"]},
  {"description": "A raccoon is digging in the trash can.", "time": "night", "sub_type": null, "expect": ["animal"]},
  {"description": "A squirrel runs along the fence.", "time": "day", "sub_type": null, "expect": ["animal"]},
  {"description": "A deer stands at the edge of the driveway.", "time": "night", "sub_type": null, "expect": ["animal"]},
  {"description": "An empty porch with a doormat.", "time": "night", "sub_type": null, "expect": ["nothing"]},
  {"description": "A pizza box lies on the porch steps.", "time": "night", "sub_type": null, "expect": ["nothing"]},
  {"description": "A cardboard sign reading 'Garage sale' leans on the fence.", "time": "day", "sub_type": null, "expect": ["nothing"]},
  {"description": "Shadows of tree branches move across the porch.", "time": "night", "sub_type": null, "expect": ["nothing"]},
  {"description": "A child's toy truck lies on the lawn.", "time": "day", "sub_type": null, "expect": ["nothing"]},
  {"description": "Rain falls on an empty driveway.", "time": "night", "sub_type": null, "expect": ["nothing"]},
  {"description": "A plastic bag blows across the yard.", "time": "night", "sub_type": null, "expect": ["nothing"]},
  {"description": "Nothing notable at this location.", "time": "night", "sub_type": null, "expect": ["nothing"]},
  {"description": "Leaves are scattered on the front steps.", "time": "day", "sub_type": null, "expect": ["nothing"]},
  {"description": "A sprinkler is watering the lawn.", "time": "day", "sub_type": null, "expect": ["nothing"]},
  {"description": "A flower pot and a bench beside the door; no one is there.", "time": "day", "sub_type": null, "expect": ["nothing"]}
]
```

- [ ] **Step 2: Write the failing tests** in `test_eval_decider.py`:

```python
"""Tests for the Decider eval scoring (no server needed). Run: python3 -m unittest test_eval_decider -v"""
import json, unittest

import eval_decider
from decider import SEEN_OPTIONS


class ScoreTest(unittest.TestCase):
    CASES = [{"description": "A box.", "expect": ["package"]},
             {"description": "A courier with a box.", "expect": ["person", "package"]},
             {"description": "A cat.", "expect": ["animal"]},
             {"description": "Shadows.", "expect": ["nothing"]}]
    ANSWERS = [{"label": "package", "p": 0.9, "ms": 300}, {"label": "package", "p": 0.73, "ms": 400},
               {"label": "nothing", "p": 0.45, "ms": 350}, None]

    def test_score(self):
        s = eval_decider.score(self.CASES, self.ANSWERS, 0.5)
        self.assertEqual((s["cases"], s["right"], s["failed"], s["unsure"]), (4, 2, 1, 1))
        self.assertEqual(s["per_class"]["person"], {"cases": 1, "right": 1})  # package is acceptable for the courier
        self.assertEqual(s["per_class"]["animal"], {"cases": 1, "right": 0})
        self.assertEqual(s["confusion"]["animal"], {"nothing": 1})
        self.assertEqual(s["misses"], [("A cat.", ["animal"], "nothing", 0.45)])
        self.assertEqual((s["p50_ms"], s["p95_ms"]), (350, 400))

    def test_report(self):
        md = eval_decider.report(eval_decider.score(self.CASES, self.ANSWERS, 0.5), 0.5, "m", "hw", "2026-10-08")
        self.assertIn("**2/4 right**", md)
        self.assertIn("| animal | 0 | 0 | 0 | 0 | 1 |", md)
        self.assertIn("- A cat. — expected animal, got nothing 0.45", md)

    def test_report_when_every_call_failed(self):
        md = eval_decider.report(eval_decider.score(self.CASES[:1], [None], 0.5), 0.5, "m", "hw", "2026-10-08")
        self.assertIn("latency n/a", md)

    def test_cases_file(self):
        with open(eval_decider.CASES) as f:
            cases = json.load(f)
        self.assertGreaterEqual(len(cases), 40)
        for c in cases:
            self.assertTrue(c["expect"] and set(c["expect"]) <= set(SEEN_OPTIONS), c)
            self.assertIn(c["time"], ("day", "night"))
        for label in SEEN_OPTIONS:
            self.assertGreaterEqual(sum(c["expect"][0] == label for c in cases), 4, label)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run them and confirm they fail.**

Run: `python3 -m unittest test_eval_decider -v`
Expected: `ModuleNotFoundError: No module named 'eval_decider'`.

- [ ] **Step 4: Implement `eval_decider.py`:**

```python
#!/usr/bin/env python3
"""Score Strands Decider on labelled doorbell descriptions and write docs/decider-eval.md.

Run (Decider server running, see README):
  python3 eval_decider.py [--url http://127.0.0.1:8000] [--min-p 0.5] [--hardware "Apple M-series GPU (MPS)"]
"""
import argparse, json, os, platform, time
from datetime import date

from decider import DEFAULT_MIN_P, SEEN_OPTIONS, SEEN_QUESTION, DeciderClient, state_text

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "eval", "decider_cases.json")
REPORT = os.path.join(HERE, "docs", "decider-eval.md")


def score(cases, answers, min_p):
    """answers[i] is {"label", "p", "ms"} for cases[i], or None when the call failed. A case counts under its
    first expected label and is right when Decider's top option is any of them."""
    per_class, confusion, misses = {}, {}, []
    right = failed = unsure = 0
    for case, a in zip(cases, answers):
        want = case["expect"][0]
        stats = per_class.setdefault(want, {"cases": 0, "right": 0})
        stats["cases"] += 1
        if a is None:
            failed += 1
            continue
        row = confusion.setdefault(want, {})
        row[a["label"]] = row.get(a["label"], 0) + 1
        unsure += a["p"] < min_p
        if a["label"] in case["expect"]:
            right += 1
            stats["right"] += 1
        else:
            misses.append((case["description"], case["expect"], a["label"], a["p"]))
    ms = sorted(a["ms"] for a in answers if a)
    pct = lambda q: ms[min(len(ms) - 1, int(q * len(ms)))] if ms else None
    return {"cases": len(cases), "right": right, "failed": failed, "unsure": unsure, "per_class": per_class,
            "confusion": confusion, "misses": misses, "p50_ms": pct(0.5), "p95_ms": pct(0.95)}


def report(s, min_p, model, hardware, day):
    latency = f"p50 {s['p50_ms']:.0f} ms, p95 {s['p95_ms']:.0f} ms" if s["p50_ms"] is not None else "n/a"
    options = list(SEEN_OPTIONS)
    return "\n".join([
        f"# Strands Decider eval — {day}", "",
        f"Model `{model}` on {hardware}. One `choice` question, \"{SEEN_QUESTION}\", with the 5 options in "
        f"`decider.SEEN_OPTIONS`; state text as in `decider.state_text`. Cases: `eval/decider_cases.json` "
        f"(hand-written descriptions in the style of Bedrock Nova; the option wording was picked on 15 of them).", "",
        f"**{s['right']}/{s['cases']} right** · unsure (p < {min_p}, the house pulses \"Not sure\") {s['unsure']} · "
        f"failed calls {s['failed']} · latency {latency}", "",
        "| class | cases | right |", "|---|---|---|",
        *[f"| {c} | {v['cases']} | {v['right']} |" for c, v in s["per_class"].items()], "",
        "Confusion (rows: expected, columns: Decider's top option)", "",
        "| expected | " + " | ".join(options) + " |", "|---" * (len(options) + 1) + "|",
        *[f"| {c} | " + " | ".join(str(row.get(o, 0)) for o in options) + " |" for c, row in s["confusion"].items()], "",
        "Misses", "",
        *([f"- {d} — expected {'/'.join(e)}, got {label} {p:.2f}" for d, e, label, p in s["misses"]] or ["- none"]), ""])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--min-p", type=float, default=DEFAULT_MIN_P)
    ap.add_argument("--model", default="strands-decider-2B-hobson-v21")
    ap.add_argument("--hardware", default=f"{platform.system()} {platform.machine()}")
    a = ap.parse_args()
    with open(CASES) as f:
        cases = json.load(f)
    client, answers = DeciderClient({"DECIDER_URL": a.url, "DECIDER_TIMEOUT": "60"}), []
    for case in cases:
        clock = "23:30" if case["time"] == "night" else "14:05"
        t = time.perf_counter()
        d = client.choose(state_text("Front Door", clock, case["time"] == "night", case.get("sub_type"), case["description"]),
                          SEEN_QUESTION, SEEN_OPTIONS)
        answers.append(d and {**d, "ms": (time.perf_counter() - t) * 1000})
    s = score(cases, answers, a.min_p)
    with open(REPORT, "w") as f:
        f.write(report(s, a.min_p, a.model, a.hardware, date.today().isoformat()))
    print(f"{s['right']}/{s['cases']} right, {s['unsure']} unsure, {s['failed']} failed -> {REPORT}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run the tests and confirm they pass.**

Run: `python3 -m unittest test_eval_decider -v`. Expected: 4 pass.
Then run the full suite plus `test_eval_decider`. Expected: OK.

- [ ] **Step 6: Run the eval against the real server.**

Check that the server is up with `curl -s -m 5 127.0.0.1:8000/v1/systemone -H 'Content-Type: application/json' -d '{"state":"x","questions":{"q":{"type":"noul","instructions":"ok?"}}}'`. It should return JSON.
- If the server isn't running, report BLOCKED with that reason. Don't start one: the controller owns it.

Then run: `python3 eval_decider.py --hardware "Apple Silicon Mac, PyTorch MPS"`
Expected:
- It prints `N/42 right, ...`.
- It writes `docs/decider-eval.md`.
- Report the numbers in your report file, including the right count, the unsure count and p50/p95. Don't tune the cases or options to raise the score.

- [ ] **Step 7: Update the docs.**

**`.env.example`:**
- Replace the 2 comment lines above `DECIDER_ENABLED=0` with:

```
# Strands Decider: a local decision model that reads the camera description and says what is at the door
# (package / person / vehicle / animal / nothing). Start the server first (see README). Off = keyword matching.
```

- Change `DECIDER_MIN_P=0.8` to `DECIDER_MIN_P=0.5`.

**`README.md`:**
- Replace the bullet starting `- Each automation has \`when\`` (3 lines) with:

```
- Each automation has `when` (all must hold: `kind` such as `night` / `doorbell` / `person` / `burst`,
  `description_has` words from the Bedrock description, optional `source` camera name, `time` `day` or `night`,
  `seen` Decider answers such as `["package"]`, or `"unsure": true`) and `do` (device commands).
  `cooldown_s` is measured in event time.
```

- Replace `` `{"action": "on", "duration_s": 300, "automation": "Night path light", ...}`. `` with `` `{"action": "on", "duration_s": 600, "automation": "Package porch light", "label": "Package", ...}`. ``

- In "## Strands Decider check (optional)", replace the first paragraph (from `Keyword matching fires the porch light` through `The app sends one warm-up request at startup.`) with:

```
With `DECIDER_ENABLED=1`, PorchLink asks a local [Strands Decider 2B](https://github.com/strands-labs/strands-decider)
server one question per described motion event: "What is at the door in this camera frame?", with the options
package / person / vehicle / animal / nothing. The answer and the time of day pick the light pattern in `automations.json`:
package → porch light on, person at night → three lights flash, person by day → gentle pulse, vehicle at night →
floodlight, animal or nothing → nothing ("false alarm skipped" on the dashboard). When the top answer's p is below
`DECIDER_MIN_P` (default 0.5) the house gives a gentle "Not sure" pulse instead. Doorbell presses never ask Decider.
If the server is down, rules fall back to their keywords. The app sends one warm-up request at startup.
Scores on 42 labelled descriptions: [docs/decider-eval.md](docs/decider-eval.md) (`python3 eval_decider.py`).
```

- In the code block below it, change `--device cpu` to `--device mps` and add the comment `# Apple Silicon; use --device cpu elsewhere`.

- [ ] **Step 8: Commit.**

```bash
git add eval/decider_cases.json eval_decider.py test_eval_decider.py docs/decider-eval.md README.md .env.example
git commit -m "feat(eval): score Decider on 42 labelled doorbell descriptions; report, README and env docs"
```

# Decider picks the light pattern — design

**Date:** 2026-10-08 · **Status:** approved in chat. The user approved the diagram (`docs/diagrams/decider-design.html`), "gentle pulse" when unsure, and "start the decider work". · **Branch:** `feat/decider-patterns` · **Hackathons:** Ring track (Oct 23) and Agent Experience / Strands Decider (Oct 30).

## Goal

For each motion event, Strands Decider answers one question: **what is at the door?** Simple, editable rules then turn the answer and the time of day into a light pattern. A doorbell press always flashes, without AI. When Decider isn't sure, the house gives a gentle pulse. A confident "nothing" (a cat, a pizza box, a sign) does nothing, and the dashboard shows "false alarm skipped".

Out of scope:
- Running Decider on AWS (SageMaker endpoint, days 7–8).
- Bedrock Nova on real snapshots (days 7–8). Until then descriptions come from the simulator stub or Ollama.
- Watch live (day 9).
- Decider looking at images directly. This is an optional experiment in the eval script only.

## Decision flow (per event, inside the worker)

1. **Doorbell press** (`button_press` → alert kind `doorbell`): the existing "Visual doorbell" automation flashes ×3. **Decider is not called.**
2. **Motion with a description** (Nova, Ollama or a stub). Ask Decider one `choice` question:
   - **State text:** `Doorbell camera "<name>" saw motion at <HH:MM> (<day|night>). Ring's label: <sub_type or "none">. AI description of the camera frame: <description>`
   - **Question `seen`:** "What is at the door in this camera frame?"
   - **Options and criteria** (exact; wording chosen by the probe below):
     - `package`: "a package, parcel, box or envelope left at the door"
     - `person`: "a person is visible, even one carrying or delivering a box" (changed 2026-10-10, see below)
     - `vehicle`: "a real car, truck or van is visible"
     - `animal`: "an animal is visible and no person"
     - `nothing`: "no person, animal, vehicle or package: empty scene, shadows, signs, toys or food"
   - The answer is the top option and its probability, recorded on the event as `decision` and `decision_p`.
3. **Rules.** Automations in `automations.json` gain two conditions, ANDed with the existing ones:
   - `seen`: a list of options; matches when Decider's top option is listed and its p ≥ `min_p` (default `DECIDER_MIN_P` = **0.5**).
   - `time`: `"day"` or `"night"`; `"night"` means the alert kinds include `night`.
   - Doorbell presses never match `seen` or `unsure` automations: Decider is not asked about them.
4. **Unsure.** When the top p < `min_p`, no `seen` automation matches. Instead, the automation marked `"when": {"unsure": true}` fires: a gentle `pulse` on the living-room light, label "Not sure". This applies whatever the top option is, so a real visitor is never silently missed.
5. **Fallback.** When Decider is off or unreachable, or there is no description:
   - Automations match on their existing `description_has` keywords, as today.
   - A `seen` automation uses its `description_has` keywords instead; one without keywords ("Visitor by day") does not fire.
   - `unsure` never matches.
   - The event records no decision.
   - The house never goes silent because the AI is down.

### Probe (2026-10-08, hobson-v21 on Apple MPS)

15 hand-written descriptions, night state text, 2 wordings:

| | right | p ≥ 0.8 | latency |
|---|---|---|---|
| first wording (box in "nothing") | 12/15 | 5 | about 0.37 s |
| **shipped wording** | **14/15** | 4 | about 0.37 s |

- With 5 options, right answers mostly land at p 0.5–0.9. At a cutoff of 0.8, most events would pulse "Not sure". At 0.5, only the pizza box (p 0.43) pulsed.
- So the default cutoff is 0.5, and `DECIDER_MIN_P` still overrides it.
- The one miss, "A courier is holding a box at the door." → package 0.73, is a reasonable answer. The eval set accepts either label for such cases.
- The docs' "0.9+ calibration" was not seen for 5-way choices on this model.

### Shipped rules (`automations.json`)

| Automation | when | do | label |
|---|---|---|---|
| Visual doorbell | kind doorbell | living-room flash ×3 | Doorbell! |
| Package porch light | seen package (fallback: description_has package/box/parcel) | porch on 10 min, cooldown 10 min | Package |
| Visitor at night | seen person, time night (fallback: kind night + description_has person/people/man/woman/courier) | flash ×3 on hallway + living room + porch | Someone outside |
| Visitor by day | seen person, time day | living-room pulse 10 s | Visitor |
| Night vehicle floodlight | seen vehicle, time night (fallback: kind night + description_has car/vehicle/truck/van) | floodlight on 3 min | Car arriving |
| Not sure | unsure | living-room pulse 10 s | Not sure |

The old "Night path light" (any night motion → hallway on) is **replaced** by "Night light (AI off)" (`"no_decision": true`: night motion with no Decider answer → hallway on 5 min, label "Night motion"), so the house is never silent when the AI is down, and a cat never lights the hallway when Decider is on. "Visitor at night" and "Visitor by day" have a 60 s cooldown. The per-automation yes/no `ask` questions are **replaced** by the single `seen` question, and `bridge.py`'s `ask` handling is removed.

### Changes after the final review (2026-10-08)

- `description_has` matches whole words (plural -s/-es allowed) and ignores anything after a negation (no/not/nobody/none/without/never/n't) in the same sentence. Example: "no person is visible" does not mention a person, and "cardboard" is not "car".
- Decider is asked only for `motion_detected` events. Live views and doorbell presses never ask it.
- `DECIDER_TIMEOUT` defaults to 30 s, because calls run in the worker's main lane, one at a time. Since 2026-10-10, doorbell presses have their own lane and never wait on Decider.
- The eval headline also reports "right and sure" (p ≥ min_p).

### Change after the first full simulator run (2026-10-10)

- A man holding a box at the back door at night came out as "package 0.51", so the 3-light visitor flash never fired.
- The `person` wording now says "even one carrying or delivering a box": someone at the door outranks their parcel.
- On 48 cases (the 42 eval cases plus 6 real qwen2.5vl:3b descriptions from the run), the old wording got 45/48 right with 37 right and sure. The new wording got 48/48 right, with 43 right and sure.
- Rewording `package` as well ("with no person in view") was rejected. It turned a pizza box, a plastic bag and leaves into "package".

## Components

- **`decider.py`:** `DeciderClient.choose(state, instructions, options) -> {"label", "p", "probabilities"} | None`. It POSTs one `choice` question to `/v1/systemone`. Any failure returns None and logs `(decider skipped: …)`. The warm-up call uses `choose`.
- **`bridge.py`:**
  - `Bridge.plan` and `Bridge.run` take a `decision` (the `choose` result or None) instead of calling Decider per automation.
  - `matches()` learns `seen`, `time` and `unsure`.
  - Results carry `decision_p` (top p) when the automation used `seen` or `unsure`.
  - Cooldowns are unchanged.
- **`app.handle_event`:**
  - It builds the state text.
  - It calls `decide.choose(...)` once per motion event that has a description. Doorbell presses never call it.
  - It stores the decision (`store.set_decision(event_id, label, p)`) and passes it to `bridge.run`.
  - The email gains a line `Decider: <label> (<p>)`.
- **`store.py`:** new `events` columns `decision TEXT` and `decision_p REAL`, using the existing migration pattern, plus `set_decision`. `timeline()` returns them.
- **`web.summarize`:** uses the event-level decision when present.
  - **Chip text:** `Decider: <label> <p>`, for example "Decider: package 0.93".
  - **Chip kind** (`summarize(e, actions, min_p)`; the web server gets the same `min_p` as the Bridge):
    - `unsure` when p < min_p;
    - `skip` when the label is `nothing` or `animal` and no action ran, with detail "false alarm skipped";
    - `ai` otherwise (Decider decided; a cooldown may have meant nothing ran).
  - Action-level `decision_p` still works for old rows, with the old 0.8 / 0.5 chip rule.
- **`dashboard_preview.py`:** sample events use event-level decisions.

## Evidence for judges

- **`eval_decider.py`** runs a labelled set against a running Decider server and writes `docs/decider-eval.md`. The set is about 40 Nova-style descriptions in `eval/decider_cases.json`, each with an expected label, and covers:
  - packages, including "a box held by a courier";
  - people by day and at night;
  - vehicles;
  - animals;
  - false alarms: pizza box, cardboard sign, shadow, empty porch, toy;
  - ambiguous cases.
- The report gives:
  - accuracy per class and a confusion table;
  - how often it was unsure (below 0.8);
  - p50 and p95 latency;
  - the hardware, recorded honestly.
- A case may list several acceptable labels (for example a courier holding a box: person or package).
- **Later, not in this plan:** a `--vision` mode that compares Decider reading the simulator's images directly against reading the descriptions.

## Testing

All tests are offline; Decider is stubbed in unit tests.

- **`choose`:** request shape, parsing the top option, failure returns None.
- **Bridge:**
  - `seen` matches at p ≥ min_p and not below;
  - `time` day/night;
  - `unsure` fires only when a decision exists and p < min_p;
  - the fallback to `description_has` when the decision is None;
  - a doorbell never needs a decision;
  - cooldowns are unchanged.
- **`handle_event`:**
  - one `choose` call per motion event with a description;
  - none for a doorbell;
  - the decision is stored and shows in `/api/events` with the right chip.
- **End to end (`night_visit.json` with a stub Decider keyed by description):**
  - a person by day (22:50) → "Visitor" pulse;
  - the doorbell → flash, Decider not asked;
  - package → porch;
  - empty porch → nothing, with chip "false alarm skipped";
  - car at night → floodlight;
  - an ambiguous figure at night → "Not sure" pulse.
  - Person at night → three lights flash is covered by the Bridge and `handle_event` tests: the scenario has only one person image.
- **Eval script:** a unit test of its scoring on a tiny fixed answer set (no server).
- The existing tests are updated where they used `ask`.

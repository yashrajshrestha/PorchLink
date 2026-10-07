# PorchLink — hackathon plan

**Event:** Build, Ship, Shape: Amazon Developer Hackathon 2026 (Devpost), Ring track + AWS Builder mini challenge
**Deadline:** Fri 23 Oct 2026, 12:00 PT (17 days from 6 Oct) · AWS credits form by 21 Oct · Office hours 19 Oct 9:00 PT
**Prizes we target:** Ring 1st $12K / 2nd $8K · AWS Builder mini challenge $5K + $5K credits (can win both)
**Judging:** 4 equal criteria — Tech Implementation, Design, Potential Impact, Quality of Idea. Stage 1 is pass/fail (fits theme, really uses Ring APIs). Friction log = up to 10% bonus.

## 0. Other entries from the same build

| Hackathon | Deadline | Prize | What it adds |
|---|---|---|---|
| **Agent Experience Hackathon** (AWS Builder Center, [page](https://builder.aws.com/build/hackathons/4f50957c-1d08-37ab-b11c-97516b9adeb4/agent-experience-hackathon)) — "build a working agent on AWS, powered by Strands Decider 2B" | Oct 16–30, 2026 (page also says 16–23; confirm) | 3 × $5,000 AWS credits + swag | Decider becomes the decision step (picks the light pattern, not only "package yes/no") and runs **on AWS** (small GPU instance, ~$0.50/h, on only for testing + demo). Its own video. Submission rules "coming soon". |
| OpenCV AI Competition 2026, powered by AWS | Oct 27, 12:30 | $5K/$3K/$2K + $1K Agentic Vision award | An OpenCV 5 step (crop Ring's watermark band, cheap "did anything change?" pre-filter before Bedrock), technical report, ≤5 min video, working endpoint. Optional. |

**Decider work (folds into days 4–6 + 7–8):**
1. Days 4–6: one Decider `choice` question per event — options `doorbell_flash`, `package_amber`, `vehicle_floodlight`, `stranger_all_rooms`, `nothing` — built from the Ring event + Nova's JSON description. Deterministic safety rules still win (a doorbell press always flashes). Measure accuracy on the labelled frames, including the "pizza box / cardboard sign → nothing" cases.
2. Days 7–8: run `strands-decider serve` on an AWS GPU instance (g4dn/g5 class, stop it when idle), reached over HTTPS from PorchLink; record p50 latency (target < 1 s) and cost.
3. Video: show the decision with its probability on screen, then the pizza box → nothing happens.

## 1. What problem PorchLink solves

Ring's Partner API is **read-only**: it tells you what the camera saw, but cannot make anything happen. Ring's own Smart Alerts and Alexa Routines can turn a light on when motion fires, but they are coarse (any motion, any time) and tied to Alexa.

**The real customer:** someone who is deaf or hard of hearing. A phone notification in another room is useless; the *house* has to show them who is at the door. Ring has no built-in flasher — Ring's own community has asked for one for years — and the hobby fixes (Alexa routine + Hue, IFTTT) can only blink on "motion", not on *what* was seen.

**PorchLink's one-line pitch:** *The doorbell you can see.* Ring events, verified by Amazon Bedrock vision, drive distinct light patterns through AWS IoT Core — a different pattern for a doorbell press, a delivered package, a vehicle, or an unknown person at night — and nothing flashes for a pizza box or a cardboard sign.

Secondary wins that fall out of the same pipeline (mention, don't lead with): package visibility, night path lighting, caretaking alerts.

## 2. Competitor landscape (visible on GitHub, 6 Oct; the Devpost gallery is still hidden)

| Lane | Entries seen | Verdict |
|---|---|---|
| "AI describes the doorbell event" | Doorstep (blind/low-vision, Nova Pro, crops watermark, demo video), Threshold ×2, Vigilant, HomeLedger (AgentCore, 11 real webhook bodies, 141 tests) | **Crowded.** Ring itself now ships Video Descriptions. |
| Caretaking / elder care | CareDoor, Kinwatch, CareSentinel-Plus, StillThere, CareRelay, Attest | **Crowded** (7+). "Absence of activity" and "family briefing" are taken. |
| Business systems | ShopSense (footfall, HMAC webhooks, Vercel demo) | One strong entry, retail only. |
| Access control / "security sentry" | GuardAgent AI, Ring-Cerberus | Mostly simulated; Ring can't unlock anything. |
| **IoT home automation with real actuation** | **none** | **Empty lane.** Nobody uses IoT Core or drives a real light. |
| **Accessibility for deaf / hard of hearing** | **none** (Doorstep owns blind/low-vision) | **Empty lane.** |

What the strong rivals have that we don't yet: real Ring webhook fixtures, 130+ tests, measured numbers, a demo video, watermark handling. What we have that they don't: a real actuator path (IoT Core), a false-alarm gate (Decider), and a chaos-mode simulator.

**Honest novelty today: ~4/10.** "Event → light" is a solved consumer trick. Positioned as *vision-verified accessible visual alerts over IoT Core*, it becomes the only entry in two under-served priority areas at once.

## 3. Facts that shape the build (verified from Ring docs / release notes)

- Webhooks: HTTPS POST, must return 200 within 5 s, signed with `X-Signature: sha256=<hex>` = HMAC-SHA256 over the **raw body** with the HMAC key from app credentials. Dedupe on `meta.request_id` (at-least-once delivery). Payload (likely): `{"data":{"type":"motion_detected","attributes":{"source":"ava1.ring.device.…","source_type":"devices","timestamp":1786715596787,"sub_type":"motion","component_ids":["0"]}},"meta":{"request_id":…,"time":…}}`. Log a real delivery before trusting the parser.
- Motion `sub_type` (5 Oct note): with Smart Alerts **off**, every event is plain `motion`. With them on: `human`, `vehicle`, `package_delivery`, `other_motion`. Our vision pass must not depend on sub_type.
- Media since 8 Jun carries a **watermark band** (Ring logo, device id, app name, time), ~top 15% of the frame. Crop it before Bedrock; Doorstep found Nova refused uncropped frames.
- E2EE cameras are invisible to the API; TAKE cameras return unreadable media. Snapshot failure must degrade gracefully (we already do).
- Tokens: Playground token = 30 min, no refresh. Real OAuth: 4 h access / 30-day refresh. A **private app** (no certification, 5–10 allowlisted accounts, needs a Ring account + plan or trial) is the only way to a long-lived demo.
- Playground may or may not deliver simulated events to a real webhook (sources conflict). Test on day 1; fall back to our simulator posting **signed** payloads.
- Ring MCP server = docs search only (no device data). Any "agent with Ring tools" must wrap our own Ring calls.
- Content policy: no identity/age/mood guessing (our prompt already complies), no training on Ring data, no alarm-service triggering.
- Only Ring control endpoint found: play audio on a Chime. Everything else is read-only — say so explicitly as an architecture decision.

## 4. Winning stack (feasible solo in 17 days, < $10 total)

| Layer | Choice | Why |
|---|---|---|
| Vision | **Bedrock Nova 2 Lite** (GA; 230 tokens/image, ~$0.0005/event), structured JSON via `toolConfig`, Guardrails on output text | Cheapest GA multimodal; bounding boxes; Nova Premier is retired, Nova 2 Pro is preview. Optional Nova Pro "second opinion" on low confidence. |
| Actuation | **AWS IoT Core**: MQTT publish + **Device Shadows** (desired/reported per light) + a Rule fanning `porchlink/+/alert` to SNS/Lambda | The real actuator Ring lacks; free tier covers it; shadows prove the light really changed. |
| Ingress | **Lambda + API Gateway** webhook receiver: verify HMAC, dedupe via DynamoDB conditional put, ack < 5 s, forward to the worker | Clean, documentable, exactly what judges can read in 2 minutes. |
| Decision (stretch, days 9–11) | **Strands agent on AgentCore Runtime** choosing among tools `flash`, `light_on`, `alert`, `log_only`; hard safety rules stay deterministic | Checks Bedrock + Strands + AgentCore boxes in one story; only if the core is done. |

Skip: Greengrass/edge (if the internet is down Ring's event never arrives either, so "offline-first" is hollow here), Kinesis Video Streams (needs a WHEP bridge), IoT Events (retired May 2026), Pinpoint (retiring Oct 2026), Amazon Connect calls (1–2 days of setup), Alexa Smart Home skill (unverified under Alexa+, needs certification-grade account linking).

**Real hardware for the video:** one ESP32 (~NPR 1,000) or a Raspberry Pi with a relay/LED strip subscribed to IoT Core. A real bulb flashing on camera beats any dashboard. Keep the virtual-lights dashboard as the labelled fallback.

## 5. What to build, in order

**Days 1–3 — credibility (must).** Signed-webhook verification (`X-Signature`, constant-time compare, raw body), real payload parser + `request_id` dedupe, body-size cap, JSON-only; simulator signs its webhooks and emits the real shape. Fix the three correctness bugs: event marked done only after handling, one worker queue (no poll/webhook race), per-device error isolation. SMTP TLS verification. Playground webhook test (day 1).

**Days 4–6 — the differentiator (must).** Light *patterns*, not just on/off: `flash ×3` (doorbell), `pulse amber` (package), `steady 10 min` (vehicle at night), `all rooms flash` (unknown person at night), `nothing` (vision says no). Watermark crop → Nova 2 Lite structured JSON `{person, package, vehicle, animal, confidence}` → gate → IoT Core shadow update. Build a labelled set of 40 frames (sim images + Playground/sandbox frames) and publish an **accuracy table** (precision/recall per class, p50 press-to-light latency).

**Days 7–8 — AWS proof (must for the $5K).** Real Bedrock calls, real IoT Core (shadows + rule → SNS), Lambda/API GW receiver deployed. IAM least-privilege policy in the repo. Budget alert $5. Screenshots for the video.

**Days 9–11 — stretch.** (1) **TV mode**: a `/tv` page served by PorchLink's web server — huge text, the snapshot, full-screen flash per event, pushed live over the existing connection. Opened in Fire TV's Silk browser (or any TV browser / Chromecast) it turns the living-room TV into a second visual alert, which matters for deaf/HoH users. No app store needed; a thin Fire OS wrapper app with heads-up notifications only if time allows (Fire OS allows heads-up over playing video; Vega OS does not). Competitor note: AccessBell already does Ring→Fire TV captions, so TV mode is a feature in our Ring entry, not a separate Fire TV submission. (2) Strands agent on AgentCore for triage/wording, if the core is done. Otherwise spend these days on tests (target 100+) and dashboard polish (pattern legend, per-event timeline with snapshot + verdict).

**Days 12–14 — ship.** Demo video (script in §6), architecture diagram, README rewrite around the pitch, product feedback (per tool: Ring Playground, webhooks, Event History, Bedrock Nova 2 Lite, IoT Core, Strands/AgentCore), friction log, "what changed during the window" section (one commit before 31 Aug would be a problem — ours is 6 Oct, but document it anyway).

**Days 15–17 — buffer.** Office hours 19 Oct. Submit by 22 Oct, not the last hour.

## 6. Demo video (< 3:00, English, no third-party trademarks/music)

0:00 Problem — a doorbell nobody hears; Ring has no flasher; community has asked for years. One sentence on who this is for.
0:25 Architecture card — Ring (read-only) → signed webhook → Bedrock vision → IoT Core → lights. "Ring can't act; PorchLink acts."
0:45 Live: doorbell press → living-room lamp flashes 3× — show the latency number on screen.
1:05 Live: courier leaves a box → Nova: "package at door, 0.93" → porch light pulses amber.
1:25 Live: night, unknown person → all rooms flash; night, car → floodlight steady.
1:45 False alarm: pizza box / cardboard sign → verdict "no package" → **nothing happens**. This is the moment that beats Alexa Routines.
2:05 Chaos mode: duplicate webhooks, 500s, out-of-order events → exactly one flash. Show the test count.
2:25 AWS proof: Bedrock console call, IoT Core shadow flipping, accuracy table, cost ($0.0005/event).
2:45 Close: what Ring could add (a flasher capability, webhook payload docs, sub_type without Smart Alerts) → ties into the feedback form.

## 7. Submission checklist

- [ ] Public repo, MIT license (have), README with pitch + diagram + numbers + run steps
- [ ] Video on YouTube, public, < 3 min
- [ ] Text description; track = Ring; mini challenge = AWS Builder (describe each AWS service and how, in the feedback answer)
- [ ] Product feedback per tool (specific: what worked, pain, onboarding, would use again)
- [ ] Friction log (errors hit, docs gaps — e.g. 404 doc pages, Playground webhook ambiguity, sub_type note)
- [ ] AWS credits form (by 21 Oct) · $5 budget alert · least-privilege IAM user, keys only in ~/.aws
- [ ] Snapshot/watermark shown or disclosed honestly; no claim that we control Ring devices

## 8. Risks

| Risk | Mitigation |
|---|---|
| Playground sends no webhooks | Simulator posts signed real-shape payloads; say so in the video |
| Watermark breaks Nova | Crop top 15%; measure on the labelled set |
| 30-min token during recording | Record in segments; or private app + real Ring account/trial |
| Agent non-determinism | Agent only picks wording/triage; patterns are deterministic rules |
| Time (booking app in parallel) | Days 1–8 are the product; everything after is bonus |

# PorchLink

**Ring events drive the rest of the smart home.** When a Ring camera sees activity, PorchLink applies
rules (night time, doorbell, bursts, and what Amazon Bedrock sees in the frame) and sends commands to
other devices through AWS IoT Core, for example:

- motion after 23:00 → hallway light on for 5 minutes, so nobody walks to the door in the dark
- doorbell pressed → living-room light flashes 3 times, for people who cannot hear the bell
- Bedrock sees a package → porch light on for 10 minutes
- Bedrock sees a car at night → driveway floodlight on

The Ring Partner API is read-only, so PorchLink never controls Ring devices: it bridges Ring events
to other devices. Automations live in `automations.json`. The core runs on the Python 3.9+ standard library.

## Run

```bash
cp .env.example .env              # then fill in email settings
RING_FAKE=1 python3 app.py        # local test mode: a fake event every 3 polls
RING_TOKEN=<playground token> python3 app.py
```

Dashboard: http://127.0.0.1:8080 (virtual lights update every second, plus the event timeline)

Preview the dashboard without Ring or AWS (every light state + sample events): `python3 dashboard_preview.py`, then open http://127.0.0.1:8090

## Home actions (`bridge.py`, `automations.json`)

- Each automation has `when` (all must hold: `kind` such as `night` / `doorbell` / `person` / `burst`,
  `description_has` words from the Bedrock description, optional `source` camera name) and `do`
  (device commands). `cooldown_s` is measured in event time.
- An automation can have an optional `label`, the word shown on its dashboard tile (for example "Doorbell!").
- The `pulse` action is a gentle glow, used when the AI isn't sure; it lasts `duration_s` (default 10).
- Commands are JSON on MQTT topics `porchlink/<device_id>/set`, for example
  `{"action": "on", "duration_s": 300, "automation": "Night path light", ...}`.
- `BRIDGE=local` (default): virtual lights in this process, shown on the dashboard. No AWS needed.
- `BRIDGE=iot`: publish to **AWS IoT Core** with the `iot-data` API (IAM credentials, `IOT_REGION`).
  Not yet run against a real account.
- A failed command is logged and shown as FAILED; it never blocks the other commands or the email.

## How it works

1. Polls Ring **Event History** (`/v1/history/devices/{id}/events`) every `POLL_SECONDS`. Signed webhooks
   and polls feed one worker, which handles each event once.
2. Stores new events in SQLite (`porchlink.db`). Each camera's existing history is recorded once, without
   alerts, on that camera's first successful poll.
3. Rules (`rules.py`):
   - activity between `NIGHT_START` and `NIGHT_END` → night alert
   - `BURST_COUNT` events within `BURST_MINUTES` → burst alert, once per window
   - anything else → plain activity email (`ALERT_EVERY_EVENT=1`)
4. Sends one email per event over SMTP (`alerts.py`). With no SMTP settings, it prints only.

## Ring Playground notes (tested 2026-10-05)

- The token lasts 30 minutes, is read-only and has no refresh. The app exits with a message when it expires.
- The 2 Playground events seen so far were `event_type: on_demand` (someone opened live view).
  They had no stored snapshot or clip (`MEDIA_NOT_FOUND`) and no `cv_detections`.
  Inferred, not yet confirmed: the Playground's Package / Vehicle / Motion simulations are live-view sessions.
- On a real device, door activity arrives as motion or doorbell events. v1 alerts on every event type,
  so `on_demand` stands in for them in the Playground demo.
- Snapshot request format: `{"type": "at_timestamp", "timestamp": <epoch ms>}`.

## Local Ring simulator (`sim/`)

A fake Ring Partner API that plays a scenario file, so you can test events the Playground cannot make
(`motion_detected` with `sub_type: human`, `button_press`, webhooks, snapshots).

```bash
# Terminal 1: start the app first, so it is ready for the first webhook
RING_WEBHOOK_SECRET=sim-secret RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token python3 app.py
# Terminal 2: 60x speed (the 50-minute scenario plays in about 50 s), 20% injected faults
python3 sim/fake_ring_server.py sim/scenarios/night_visit.json --speed 60 --chaos 0.2 \
    --webhook http://127.0.0.1:8080/webhook
```

- Chaos mode injects 500 errors, slow replies, duplicate and out-of-order events, and duplicate webhooks.
  `--token-ttl` makes every call return 401 after N seconds.
- Snapshot requests follow the real body format and error codes; images come from `sim/images/`
  (real Pexels stock photos, see `sim/images/CREDITS.md`).
- Webhooks follow Ring's documented shape and signature: `X-Signature: sha256=<HMAC-SHA256 of the raw body>` with
  `RING_WEBHOOK_SECRET` (the app refuses unsigned or wrongly signed webhooks, and all webhooks while the secret is unset).
  The id is `meta.request_id`, so Ring's re-deliveries are stored once, and a press seen by both the webhook and the
  history poll is handled once (`worker.py`). Chaos mode also sends forged webhooks; the app answers them 401.
- Write your own scenario: copy `sim/scenarios/night_visit.json` and edit `start_at` and `events`.

## Test

```bash
python3 -m unittest test_porchlink test_sim test_vision test_bridge test_decider test_webhooks test_worker test_web -v   # app, sim, chaos, Bedrock/Ollama/Decider/MQTT (stubbed), bridge
.venv/bin/python -m unittest test_grab_frame -v     # v2 frame grab (needs requirements-v2.txt)
```

## Bedrock snapshot descriptions (optional)

For each event that alerts, the app downloads the snapshot (`ring_client.snapshot`) and asks Amazon Bedrock
**Nova Lite** for one factual sentence ("One person is leaving a box at the door"). The sentence goes into the
email and the timeline. It never guesses identity, age, ethnicity or mood.

```bash
.venv/bin/pip install -r requirements-v2.txt
BEDROCK_ENABLED=1 AWS_PROFILE=<your own account> .venv/bin/python app.py
```

- Model: `us.amazon.nova-lite-v1:0` (US geo inference profile), region `us-east-1`. Turn on Nova Lite
  model access in the Bedrock console first. Cost: about $0.0001 per image.
- Use your **own** AWS account. If Bedrock fails, the alert is still sent without a description.
- Playground events have no stored media, so descriptions only appear with the simulator
  (`sim/images/`) or a real device. Next step: describe a WHEP live-view frame (`grab_frame.py`) instead.

## Open-source test run (no AWS)

Same flow with open-source parts: Ollama vision model instead of Bedrock, Mosquitto instead of IoT Core.
It proves the pipeline end to end, not the AWS calls (IoT Core is reached over its HTTP API, not MQTT),
so still do one `VISION=bedrock BRIDGE=iot` run before the demo.

```bash
sudo apt install -y mosquitto mosquitto-clients
curl -fsSL https://ollama.com/install.sh | sh && ollama pull qwen2.5vl:3b
mosquitto_sub -t 'porchlink/#' -v                      # terminal 1: watch the light commands
rm -f porchlink.db && RING_WEBHOOK_SECRET=sim-secret RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token \
  VISION=ollama BRIDGE=mqtt .venv/bin/python app.py     # terminal 2
.venv/bin/python sim/fake_ring_server.py sim/scenarios/night_visit.json --speed 60 \
  --webhook http://127.0.0.1:8080/webhook               # terminal 3
```

## Strands Decider check (optional)

Keyword matching fires the porch light for "No package, just a cardboard sign". With `DECIDER_ENABLED=1`, automations
that have an `ask` question in `automations.json` ask a local [Strands Decider 2B](https://github.com/strands-labs/strands-decider)
server instead, and fire only when P(yes) >= `DECIDER_MIN_P` (default 0.8, or `min_p` per automation). A skip shows in
the email and timeline as "Porch light left alone (Package porch light): Decider unsure, p=0.77" (0.5 <= p < min_p)
or "...: Decider said no, p=0.10" (only when the keywords would have fired). Skips do not use up the cooldown. If the
server is down, PorchLink falls back to the keywords. The app sends one warm-up request at startup.

```bash
.venv/bin/pip install strands-decider    # separate install: pulls torch (about 5 GB)
.venv/bin/strands-decider serve StrandsAgents/strands-decider-2B-hobson-v21 --device cpu --port 8000
DECIDER_ENABLED=1 BEDROCK_ENABLED=1 AWS_PROFILE=<your own account> .venv/bin/python app.py
```

Measured on a 12-core CPU, no GPU (2026-10-06): first start about 18 min (downloads Qwen3.5-2B-Base), 7.8 GB RAM,
**7 to 17 s per event**, first call 86 s (the published 115 ms is on an RTX 3090). Calls run in the poll loop, so a burst
of events queues. On 8 package and 5 vehicle descriptions written by hand, a 2-option `choice` with descriptive yes/no
options scored better than `noul`, and 0.8 separated them all, but narrowly (pizza box 0.77 vs "A box sits on the
doorstep." 0.82; "A toy car lies on the doormat." 0.78). The questions and threshold were tuned on those same
descriptions, not on real Nova Lite output: re-check with Bedrock on before trusting 0.8.

## Next (v2)

Take a frame from the WHEP live stream → Amazon Bedrock (Nova Lite) describes it → richer alert.
Then move the poller to AWS Lambda + DynamoDB + SNS.

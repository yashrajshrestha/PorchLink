#!/usr/bin/env python3
"""PorchLink: Ring events -> rules -> smart-home actions (IoT) + email alerts.

Run:  RING_FAKE=1 python3 app.py          (local test mode, no Ring token)
      RING_TOKEN=... python3 app.py       (Ring Playground token)
Settings come from the environment or a .env file next to this script (see .env.example).
"""
import os, sys, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

from alerts import EmailAlerter
from bridge import Bridge, IotCorePublisher, LocalPublisher, MqttPublisher, VirtualDevices, describe_action, load_config
from ring_client import FakeRing, RingClient, TokenExpired, device_name, parse_event
from rules import Settings, evaluate, format_alert
from store import Store
from worker import EventWorker
import web

HERE = os.path.dirname(os.path.abspath(__file__))
RETRY_MAX_AGE_MS = 600_000  # after a restart, do not replay a failed/pending event older than this


def load_env_file(path):
    """Read KEY=VALUE lines into os.environ without overriding values already set."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def settings_from_env(env):
    return Settings(night_start=int(env.get("NIGHT_START", 23)), night_end=int(env.get("NIGHT_END", 6)),
                    burst_count=int(env.get("BURST_COUNT", 3)), burst_minutes=int(env.get("BURST_MINUTES", 30)),
                    alert_every_event=env.get("ALERT_EVERY_EVENT", "1") == "1")


def handle_event(ev, name, store, alerter, settings, tz, describe=None, bridge=None):
    """Apply rules to one new event, run home automations, send one email, record everything.
    describe(ev) is optional (Bedrock); it runs only for events that will alert, to keep cost down."""
    recent = store.starts_since(ev["device_id"], ev["start_ms"] - settings.burst_minutes * 60_000)
    reasons = evaluate(ev, recent, store.last_alert_start(ev["device_id"], "burst"), settings, tz)
    if not reasons:
        return
    description = describe(ev) if describe else None
    if description:
        store.set_description(ev["id"], description)
    actions = bridge.run(ev, {k for k, _ in reasons}, description, name) if bridge else []
    if actions:
        store.add_actions(ev["id"], actions)
    subject, body = format_alert(ev, name, reasons, tz, description, [describe_action(a) for a in actions])
    delivered = alerter.send(subject, body)
    for kind, reason in reasons:
        store.add_alert(ev, kind, reason, delivered)


class DeviceNames:
    """Device id -> name. Webhooks only carry the id; refresh from Ring on a miss, never raise."""

    def __init__(self, ring):
        self.ring, self.names = ring, {}

    def __call__(self, ev):
        device_id = ev["device_id"]
        if device_id not in self.names:
            try:
                self.names.update({d["id"]: device_name(d) for d in self.ring.devices()})
            except Exception as e:
                print(f"(device names unavailable: {type(e).__name__}: {e})")
        return self.names.get(device_id, f"Ring device ...{device_id[-6:]}")


def make_process(store, alerter, settings, tz, describe=None, bridge=None):
    """The worker's per-event step: rules, home actions, email, records."""
    def process(ev, name):
        handle_event(ev, name, store, alerter, settings, tz, describe, bridge)
    return process


def poll_once(ring, store, submit, alert_new=True, seeded=None):
    """Fetch every device's events (oldest first). Events not yet handled go to submit(ev, name), the
    worker. History is recorded without alerting (alert_new=False for every device, or, when `seeded` is a
    set, for each device the first time its events are fetched successfully: its id is then added to
    `seeded`, so a device that failed on the first poll is seeded later and never floods old alerts).
    One device failing is logged and skipped; an expired token stops everything.
    Return how many events were seen for the first time."""
    new = 0
    for device in ring.devices():
        name = device_name(device)
        try:
            raws = ring.events(device["id"])
        except TokenExpired:
            raise
        except Exception as e:
            print(f"poll error on {name}: {e}")
            continue
        record_only = (device["id"] not in seeded) if seeded is not None else not alert_new
        recorded = 0
        parsed = []
        for raw in raws:
            try:
                parsed.append(parse_event(raw, device["id"]))
            except TokenExpired:
                raise
            except Exception as e:  # one malformed item must not hide the rest, or the other devices
                print(f"bad event from {name}: {type(e).__name__}: {e}")
        for ev in sorted(parsed, key=lambda e: e["start_ms"]):
            if record_only:
                recorded += store.record_history(ev, name)
                continue
            status = store.status(ev["id"])
            if status is None:
                new += 1
            # the worker decides; it never handles an event twice. Old failed/pending events are not retried.
            if status is None or (status in ("failed", "pending") and ev["start_ms"] >= time.time() * 1000 - RETRY_MAX_AGE_MS):
                submit(ev, name)
        if seeded is not None and record_only:
            seeded.add(device["id"])
            if recorded:
                print(f"Recorded {recorded} existing event(s) for {name} without alerts.")
        new += recorded
    return new


def main():
    load_env_file(os.path.join(HERE, ".env"))
    env = os.environ
    tz = ZoneInfo(env["ALERT_TZ"]) if env.get("ALERT_TZ") else datetime.now().astimezone().tzinfo
    fake = env.get("RING_FAKE") == "1"
    if not fake and not env.get("RING_TOKEN"):
        sys.exit("Set RING_TOKEN (Playground token) or RING_FAKE=1 for local test mode.")
    ring = FakeRing() if fake else RingClient(env["RING_TOKEN"], env.get("RING_BASE_URL", "https://api.amazonvision.com"))
    store = Store(env.get("DB_PATH") or os.path.join(HERE, "porchlink.db"))
    alerter, settings = EmailAlerter(env), settings_from_env(env)
    poll_seconds = int(env.get("POLL_SECONDS", 10))
    describe = None
    vision_backend = env.get("VISION") or ("bedrock" if env.get("BEDROCK_ENABLED") == "1" else "")
    if vision_backend == "bedrock":
        from vision import BedrockDescriber
        describe = BedrockDescriber(ring, env)
    elif vision_backend == "ollama":
        from vision import OllamaDescriber
        describe = OllamaDescriber(ring, env)

    config = load_config(env.get("AUTOMATIONS_PATH") or os.path.join(HERE, "automations.json"))
    devices = None
    if env.get("BRIDGE", "local") == "iot":
        publisher = IotCorePublisher(env)  # real AWS IoT Core; devices live outside this process
    elif env.get("BRIDGE") == "mqtt":
        publisher = MqttPublisher(env)  # local broker such as Mosquitto
    else:
        devices = VirtualDevices(config)
        publisher = LocalPublisher(devices)
    decide = None
    if env.get("DECIDER_ENABLED") == "1":
        from decider import DeciderClient
        decide = DeciderClient(env)
        # The first call after a server start is slow (86 s measured on CPU); take it now, not on a real event.
        threading.Thread(target=decide, args=("Warm-up.", {"warm_up": {"question": "Is this a warm-up?"}}),
                         daemon=True).start()
    bridge = Bridge(config, publisher, decide=decide, min_p=float(env.get("DECIDER_MIN_P", 0.8)))

    worker = EventWorker(store, make_process(store, alerter, settings, tz, describe, bridge),
                         name_for=DeviceNames(ring)).start()
    webhook_secret = env.get("RING_WEBHOOK_SECRET", "")
    threading.Thread(target=web.serve, args=(store, tz, int(env.get("WEB_PORT", 8080)), worker.submit, devices,
                                             webhook_secret, env.get("WEBHOOK_LOG") == "1"), daemon=True).start()
    print(f"PorchLink | {'FAKE mode' if fake else 'Ring API'} | poll every {poll_seconds}s | "
          f"email {'ON -> ' + alerter.to if alerter.enabled else 'OFF (printing only)'} | "
          f"webhooks {'signed' if webhook_secret else 'OFF (set RING_WEBHOOK_SECRET)'} | "
          f"vision {vision_backend + ' ' + describe.model if describe else 'OFF'} | Decider {decide.url if decide else 'OFF'} | "
          f"devices: {'virtual (local)' if devices else env.get('BRIDGE')}, {len(config['automations'])} automations")

    # Each device's first successful poll only records its existing history, so a restart does not re-send old
    # alerts, even when one device fails on the first poll and is seeded later.
    seeded = None if env.get("ALERT_ON_STARTUP") == "1" else set()
    while True:
        try:
            poll_once(ring, store, worker.submit, seeded=seeded)
        except TokenExpired:
            sys.exit("\nRing token expired. Generate a new one in the Playground and restart with the new RING_TOKEN.")
        except Exception as e:
            print(f"poll error: {e}")
        time.sleep(poll_seconds)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")

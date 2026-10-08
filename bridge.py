"""The PorchLink bridge: match an event to automations, then send device commands.

Commands go to MQTT-style topics  porchlink/<device_id>/set  with a JSON body.
Publishers: LocalPublisher (virtual devices in this process, no AWS),
IotCorePublisher (AWS IoT Core through the iot-data API, IAM credentials, no device certificates) and
MqttPublisher (any MQTT broker, e.g. open-source Mosquitto, for testing without AWS).
"""
import json, threading, time

from decider import DEFAULT_MIN_P


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    ids = {d["id"] for d in cfg["devices"]}
    for a in cfg["automations"]:
        for step in a["do"]:
            if step["device"] not in ids:
                raise ValueError(f"automation '{a['name']}' uses unknown device '{step['device']}'")
    return cfg


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


def describe_action(r):
    sure = f" (Decider p={r['decision_p']:.2f})" if r.get("decision_p") is not None else ""
    if r.get("skipped"):
        verdict = "Decider said no" if r["decision_p"] < 0.5 else "Decider unsure"
        return f"{r['device_name']} left alone ({r['automation']}): {verdict}, p={r['decision_p']:.2f}"
    c = r["command"]
    if c["action"] == "on":
        d = c.get("duration_s", 0)
        what = f"ON for {d // 60} min" if d >= 60 else f"ON for {d} s"
    elif c["action"] == "flash":
        what = f"flash x{c.get('times', 1)}"
    elif c["action"] == "pulse":
        what = "gentle pulse"
    else:
        what = c["action"].upper()
    return f"{r['device_name']} {what} ({r['automation']}){sure}" + ("" if r["ok"] else " - FAILED")


class VirtualDevices:
    """Pretend smart lights. They act on commands like real ones would, and report their state."""

    def __init__(self, config, clock=time.time):
        self.clock, self.lock = clock, threading.Lock()
        self.state = {d["id"]: {"id": d["id"], "name": d["name"], "type": d["type"], "on_until": 0,
                                "flash_until": 0, "pulse_until": 0, "last": None} for d in config["devices"]}

    def apply(self, device_id, cmd):
        now = self.clock()
        with self.lock:
            s = self.state[device_id]
            if cmd["action"] == "on":
                s["on_until"] = max(s["on_until"], now + cmd.get("duration_s", 60))
            elif cmd["action"] == "off":
                s["on_until"] = 0
            elif cmd["action"] == "flash":
                s["flash_until"] = now + 1.0 * cmd.get("times", 1)  # one flash per second
            elif cmd["action"] == "pulse":
                s["pulse_until"] = now + cmd.get("duration_s", 10)  # a soft, slow glow: "the AI is not sure"
            s["last"] = {"automation": cmd.get("automation"), "label": cmd.get("label"), "at": int(now * 1000)}

    def snapshot(self):
        now = self.clock()
        with self.lock:
            return [{**s, "on": s["on_until"] > now, "flashing": s["flash_until"] > now, "pulsing": s["pulse_until"] > now,
                     "seconds_left": max(0, int(s["on_until"] - now))} for s in self.state.values()]


class LocalPublisher:
    """Delivers commands straight to the virtual devices. Topic format matches IoT Core."""

    def __init__(self, devices):
        self.devices, self.sent = devices, []

    def publish(self, topic, payload):
        device_id = topic.split("/")[1]
        self.devices.apply(device_id, payload)
        self.sent.append((topic, payload))
        print(f"(mqtt local) {topic} {json.dumps(payload)}")


class IotCorePublisher:
    """Publishes to AWS IoT Core with the iot-data API. Looks up the account's ATS data endpoint once."""

    def __init__(self, env, iot_client=None, data_client=None):
        if data_client is None:
            import boto3
            session = boto3.Session(profile_name=env.get("AWS_PROFILE") or None,
                                    region_name=env.get("IOT_REGION", "us-east-1"))
            iot_client = iot_client or session.client("iot")
            endpoint = iot_client.describe_endpoint(endpointType="iot:Data-ATS")["endpointAddress"]
            data_client = session.client("iot-data", endpoint_url=f"https://{endpoint}")
        self.client = data_client

    def publish(self, topic, payload):
        self.client.publish(topic=topic, qos=1, payload=json.dumps(payload).encode())
        print(f"(mqtt iot-core) {topic} {json.dumps(payload)}")


class MqttPublisher:
    """Publishes to a plain MQTT broker such as Mosquitto (paho-mqtt, requirements-v2.txt).
    Same topics and JSON as IoT Core, so `mosquitto_sub -t 'porchlink/#' -v` shows the commands."""

    def __init__(self, env, client=None):
        if client is None:
            import paho.mqtt.client as mqtt
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="porchlink")
            client.connect(env.get("MQTT_HOST", "127.0.0.1"), int(env.get("MQTT_PORT", 1883)))
            client.loop_start()  # background network thread; publish() below waits for the broker's ack
        self.client = client

    def publish(self, topic, payload):
        info = self.client.publish(topic, json.dumps(payload), qos=1)
        info.wait_for_publish(timeout=5)
        if not info.is_published():
            raise ConnectionError(f"MQTT broker did not acknowledge {topic}")
        print(f"(mqtt broker) {topic} {json.dumps(payload)}")

"""The PorchLink bridge: match an event to automations, then send device commands.

Commands go to MQTT-style topics  porchlink/<device_id>/set  with a JSON body.
Publishers: LocalPublisher (virtual devices in this process, no AWS),
IotCorePublisher (AWS IoT Core through the iot-data API, IAM credentials, no device certificates) and
MqttPublisher (any MQTT broker, e.g. open-source Mosquitto, for testing without AWS).
"""
import json, threading, time


def load_config(path):
    with open(path) as f:
        cfg = json.load(f)
    ids = {d["id"] for d in cfg["devices"]}
    for a in cfg["automations"]:
        for step in a["do"]:
            if step["device"] not in ids:
                raise ValueError(f"automation '{a['name']}' uses unknown device '{step['device']}'")
    return cfg


def matches(when, kinds, description, source_name):
    """All conditions in `when` must hold; each condition is satisfied by any of its values."""
    if "kind" in when and not set(when["kind"]) & kinds:
        return False
    if "description_has" in when:
        text = (description or "").lower()
        if not any(word in text for word in when["description_has"]):
            return False
    if "source" in when and source_name not in when["source"]:
        return False
    return True


class Bridge:
    def __init__(self, config, publisher, clock=time.time, decide=None, min_p=0.8):
        self.config, self.publisher, self.clock = config, publisher, clock
        self.decide, self.min_p = decide, min_p  # optional DeciderClient; None = keyword matching only
        self.devices = {d["id"]: d for d in config["devices"]}
        self.last_fired = {}

    def plan(self, kinds, description, source_name, at):
        """Return (automation, step, decision) triples to run for an event at time `at` (epoch seconds).
        decision is None, or {"p", "fired"} when Decider answered the automation's `ask` question;
        unsure automations come back with fired=False. Cooldowns use event time, so a backlog
        processed at once is not wrongly suppressed."""
        now, ready = at, []
        for a in self.config["automations"]:
            cooldown = a.get("cooldown_s", 0)
            # abs(): events from different cameras can arrive out of time order
            if cooldown and a["name"] in self.last_fired and abs(now - self.last_fired[a["name"]]) < cooldown:
                continue
            ready.append(a)

        # Decider replaces `description_has` for automations with an `ask` question; one request per event.
        asks = {a["name"]: a["when"]["ask"] for a in ready
                if "ask" in a["when"] and matches(_without_text(a["when"]), kinds, description, source_name)}
        probs = self.decide(_state(kinds, description, source_name), asks) if self.decide and asks and description else None

        out = []
        for a in ready:
            decision = None
            if probs is not None and a["name"] in probs:
                p = probs[a["name"]]
                decision = {"p": round(p, 2), "fired": p >= a["when"].get("min_p", self.min_p)}
                # Report a skip only if it is news: the keywords would have fired, or Decider was unsure.
                if not decision["fired"] and p < 0.5 and not matches(a["when"], kinds, description, source_name):
                    continue
            elif not matches(a["when"], kinds, description, source_name):
                continue
            if decision is None or decision["fired"]:  # an unsure answer must not use up the cooldown
                self.last_fired[a["name"]] = now
            out += [(a, step, decision) for step in a["do"]]
        return out

    def run(self, ev, kinds, description, source_name):
        """Send the commands for one event. Returns a list of result dicts (for the store, email and UI)."""
        results = []
        at = ev["start_ms"] / 1000 if ev.get("start_ms") else self.clock()
        for automation, step, decision in self.plan(kinds, description, source_name, at):
            command = {k: v for k, v in step.items() if k != "device"}
            topic = f"porchlink/{step['device']}/set"
            base = {"automation": automation["name"], "device": step["device"],
                    "device_name": self.devices[step["device"]]["name"], "command": command, "topic": topic,
                    "decision_p": decision and decision["p"]}
            if decision and not decision["fired"]:
                results.append({**base, "ok": True, "error": None, "skipped": True})
                continue
            payload = {**command, "automation": automation["name"], "event_id": ev["id"],
                       "source": source_name, "sent_at": int(self.clock() * 1000)}
            try:
                self.publisher.publish(topic, payload)
                ok, error = True, None
            except Exception as e:  # one failed device must not stop the others or the email
                ok, error = False, f"{type(e).__name__}: {e}"
                print(f"(action failed: {topic}: {error})")
            results.append({**base, "ok": ok, "error": error, "skipped": False})
        return results


def _without_text(when):
    return {k: v for k, v in when.items() if k not in ("description_has", "ask", "min_p")}


def _state(kinds, description, source_name):
    return (f"A home doorbell camera named '{source_name}' recorded an event ({', '.join(sorted(kinds)) or 'motion'}). "
            f"An AI description of the camera frame says: {description}")


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
    else:
        what = c["action"].upper()
    return f"{r['device_name']} {what} ({r['automation']}){sure}" + ("" if r["ok"] else " - FAILED")


class VirtualDevices:
    """Pretend smart lights. They act on commands like real ones would, and report their state."""

    def __init__(self, config, clock=time.time):
        self.clock, self.lock = clock, threading.Lock()
        self.state = {d["id"]: {"id": d["id"], "name": d["name"], "type": d["type"], "on_until": 0,
                                "flash_until": 0, "last": None} for d in config["devices"]}

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
            s["last"] = {"automation": cmd.get("automation"), "at": int(now * 1000)}

    def snapshot(self):
        now = self.clock()
        with self.lock:
            return [{**s, "on": s["on_until"] > now, "flashing": s["flash_until"] > now,
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

#!/usr/bin/env python3
"""Show the PorchLink dashboard with every light state and sample events, without Ring or AWS.

Run: python3 dashboard_preview.py [--port 8090]   then open http://127.0.0.1:8090
With a fake camera: .venv/bin/python sim/fake_whep.py & python3 dashboard_preview.py --whep http://127.0.0.1:9100
Lights: hallway off, living room flashing (doorbell), porch amber (package), driveway gentle pulse (not sure).
The states re-apply every few seconds so the preview stays lively for screenshots and the demo video.
"""
import argparse, threading, time
from zoneinfo import ZoneInfo

import web
from bridge import VirtualDevices, load_config
from store import Store

TZ = ZoneInfo("Asia/Kathmandu")
STATES = [("living-room-light", {"action": "flash", "times": 6, "automation": "Visual doorbell", "label": "Doorbell!"}),
          ("porch-light", {"action": "on", "duration_s": 480, "automation": "Package porch light", "label": "Package"}),
          ("floodlight", {"action": "pulse", "duration_s": 6, "automation": "Not sure", "label": "Not sure"})]


def seed(store, now_ms):
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

def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--whep", help="fake camera URL (sim/fake_whep.py) to turn on Watch live")
    a = p.parse_args()
    devices, store = VirtualDevices(load_config("automations.json")), Store(":memory:")
    seed(store, int(time.time() * 1000))

    def keep_lively():
        while True:
            for device_id, cmd in STATES:
                devices.apply(device_id, cmd)
            time.sleep(5)

    threading.Thread(target=keep_lively, daemon=True).start()
    status = lambda: {"mode": "Preview", "ring_ok": True, "night": True,
                      "time": time.strftime("%I:%M %p").lstrip("0"), "day": time.strftime("%a")}
    live = None
    if a.whep:
        from live import LiveViews
        live = LiveViews("preview-token", a.whep, lambda: [{"id": "front", "name": "Front Door"},
                                                          {"id": "back", "name": "Back Yard"}])
    web.serve(store, TZ, a.port, None, devices, "", False, status, live=live)


if __name__ == "__main__":
    main()

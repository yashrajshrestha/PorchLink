"""Ask a local Strands Decider server what is at the door (POST /v1/systemone).

Optional: enabled with DECIDER_ENABLED=1. Start the server separately (see README). Standard library
only, so PorchLink itself does not need torch. Any failure returns None and the bridge falls back
to keyword matching.
"""
import json, threading, urllib.request

DEFAULT_MIN_P = 0.5  # top-option p below this means "not sure"; chosen by the probe in the 2026-10-08 Decider spec
SEEN_QUESTION = "What is at the door in this camera frame?"
SEEN_OPTIONS = {
    "package": "a package, parcel, box or envelope left at the door",
    "person": "a person is visible, even one carrying or delivering a box",  # someone at the door outranks their parcel
    "vehicle": "a real car, truck or van is visible",
    "animal": "an animal is visible and no person",
    "nothing": "no person, animal, vehicle or package: empty scene, shadows, signs, toys or food",
}


def state_text(camera, clock, night, ring_label, description):
    """The `state` Decider reads: which camera, when, Ring's own label and the AI description of the frame."""
    return (f'Doorbell camera "{camera}" saw motion at {clock} ({"night" if night else "day"}). '
            f"Ring's label: {ring_label or 'none'}. AI description of the camera frame: {description}")


class DeciderClient:
    def __init__(self, env, opener=urllib.request.urlopen):
        self.url = env.get("DECIDER_URL", "http://127.0.0.1:8000").rstrip("/") + "/v1/systemone"
        self.timeout = float(env.get("DECIDER_TIMEOUT", 30))  # main worker lane: a slow call delays later motion; MPS about 0.3 s, CPU 7-17 s
        self.opener = opener
        # One question at a time: overlapping requests crash the server on Apple MPS. The timeout is per socket
        # read, so a call that times out frees the lock while the server may still be computing (slow CPU warm-up).
        self.lock = threading.Lock()

    def choose(self, state, instructions, options):
        """Ask one multiple-choice question ({option: criterion}). Returns the most likely option as
        {"label", "p", "probabilities"}, or None on any failure (the bridge then falls back to keywords)."""
        body = {"state": state, "questions": {"seen": {"type": "choice", "instructions": instructions, "criteria": options}}}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with self.lock, self.opener(req, timeout=self.timeout) as resp:
                probs = json.loads(resp.read())["answers"]["seen"]["probabilities"]
            probs = {name: float(probs[name]) for name in options}
        except Exception as e:
            print(f"(decider skipped: {type(e).__name__}: {e})")
            return None
        label = max(options, key=probs.get)
        return {"label": label, "p": round(probs[label], 2), "probabilities": probs}

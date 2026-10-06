"""Ask a local Strands Decider server yes/no questions about an event (POST /v1/systemone).

Optional: enabled with DECIDER_ENABLED=1. Start the server separately (see README). Standard library
only, so PorchLink itself does not need torch. Any failure returns None and the bridge falls back
to keyword matching.
"""
import json, urllib.request


class DeciderClient:
    def __init__(self, env, opener=urllib.request.urlopen):
        self.url = env.get("DECIDER_URL", "http://127.0.0.1:8000").rstrip("/") + "/v1/systemone"
        self.timeout = float(env.get("DECIDER_TIMEOUT", 120))  # CPU-only: about 10 s per call, first call 86 s
        self.opener = opener

    def __call__(self, state, questions):
        """questions: {name: {"question", "yes", "no"}}. Returns {name: P(yes)}, or None on any failure.
        A 2-option choice with descriptive yes/no options scored better than `noul` in our tests."""
        body = {"state": state, "questions": {
            name: {"type": "choice", "instructions": q["question"],
                   "criteria": {"yes": q.get("yes", "the answer is yes"), "no": q.get("no", "the answer is no")}}
            for name, q in questions.items()}}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                answers = json.loads(resp.read())["answers"]
            return {name: float(answers[name]["probabilities"]["yes"]) for name in questions}
        except Exception as e:
            print(f"(decider skipped: {type(e).__name__}: {e})")
            return None

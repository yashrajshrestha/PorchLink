"""Describe a Ring snapshot in one sentence: Amazon Bedrock (Nova Lite) or a local Ollama vision model.

Optional: VISION=bedrock (or BEDROCK_ENABLED=1) needs boto3 (requirements-v2.txt) and AWS credentials for
your own account; VISION=ollama needs a local Ollama server, standard library only. Any failure returns
None, so the alert still goes out without a description.
"""
import base64, json, urllib.request

PROMPT = ("This is a frame from a home doorbell camera. In one sentence under 25 words, say factually what "
          "is visible at the door: how many people and what they are doing, packages, vehicles, animals. "
          "If nothing notable is visible, say so. Do not guess anyone's identity, age, ethnicity or mood.")


class Describer:
    """Fetches the event's snapshot and asks a model about it; subclasses implement describe(jpeg)."""

    def __call__(self, ev):
        """Return a one-line description of the event's snapshot, or None."""
        try:
            jpeg = self.ring.snapshot(ev["device_id"], (ev["start_ms"] + ev["end_ms"]) // 2)
            if not jpeg:
                return None
            return self.describe(jpeg)
        except Exception as e:
            print(f"(vision skipped: {type(e).__name__}: {e})")
            return None

    def describe(self, jpeg):
        raise NotImplementedError


class BedrockDescriber(Describer):
    def __init__(self, ring, env, client=None):
        self.ring = ring
        self.model = env.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")
        if client is None:
            import boto3  # only needed when Bedrock is on
            session = boto3.Session(profile_name=env.get("AWS_PROFILE") or None,
                                    region_name=env.get("BEDROCK_REGION", "us-east-1"))
            client = session.client("bedrock-runtime")
        self.client = client

    def describe(self, jpeg):
        resp = self.client.converse(
            modelId=self.model,
            messages=[{"role": "user", "content": [
                {"image": {"format": "jpeg", "source": {"bytes": jpeg}}},
                {"text": PROMPT}]}],
            inferenceConfig={"maxTokens": 120, "temperature": 0.2})
        return resp["output"]["message"]["content"][0]["text"].strip()


class OllamaDescriber(Describer):
    """Open-source stand-in for Bedrock: a vision model served by Ollama (POST /api/chat), no AWS needed."""

    def __init__(self, ring, env, opener=urllib.request.urlopen):
        self.ring, self.opener = ring, opener
        self.model = env.get("OLLAMA_MODEL", "qwen2.5vl:3b")
        self.url = env.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
        self.timeout = float(env.get("OLLAMA_TIMEOUT", 180))  # CPU-only vision models are slow

    def describe(self, jpeg):
        body = {"model": self.model, "stream": False, "options": {"temperature": 0.2, "num_predict": 120},
                "messages": [{"role": "user", "content": PROMPT, "images": [base64.b64encode(jpeg).decode()]}]}
        req = urllib.request.Request(self.url, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with self.opener(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())["message"]["content"].strip()

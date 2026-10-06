"""Tests for snapshot download + Bedrock descriptions, using the fake Ring server and a stub Bedrock client.
No AWS account or boto3 needed. Run: python3 -m unittest test_vision -v
"""
import unittest

import app
from rules import Settings
from store import Store
from test_sim import TZ, CaptureAlerter, SimTestCase
from vision import BedrockDescriber, OllamaDescriber


class StubBedrock:
    def __init__(self, text=" One person is standing at the door. ", fail=False):
        self.calls, self.text, self.fail = [], text, fail

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("ThrottlingException")
        return {"output": {"message": {"content": [{"text": self.text}]}}}


class BodyAlerter(CaptureAlerter):
    def send(self, subject, body):
        self.sent.append(subject)
        self.bodies = getattr(self, "bodies", []) + [body]
        return True


class SnapshotTest(SimTestCase):
    def test_direct_jpeg(self):
        ring = self.start()
        ev = self.fake.events[0]
        self.assertEqual(ring.snapshot(ev["device"], ev["start"] + 1000)[:2], b"\xff\xd8")

    def test_303_followed_without_bearer_header(self):
        # The fake media URL rejects requests that carry Authorization, like a pre-signed URL would.
        ring = self.start(redirect_media=True)
        ev = self.fake.events[0]
        self.assertEqual(ring.snapshot(ev["device"], ev["start"] + 1000)[:2], b"\xff\xd8")

    def test_no_media_returns_none(self):
        ring = self.start()
        self.assertIsNone(ring.snapshot("sim.device.front", 1000))


class DescribeTest(SimTestCase):
    def run_scenario(self, bedrock):
        ring = self.start()
        store, alerter = Store(":memory:"), BodyAlerter()
        describe = BedrockDescriber(ring, {}, client=bedrock)
        app.poll_once(ring, store, alerter, Settings(), TZ, describe=describe)
        return store, alerter

    def test_description_in_email_and_store(self):
        bedrock = StubBedrock()
        store, alerter = self.run_scenario(bedrock)
        with_image = [e for e in self.fake.events if e["image_path"]]
        self.assertEqual(len(bedrock.calls), len(with_image))  # on_demand event has no media: no call
        call = bedrock.calls[0]
        self.assertEqual(call["modelId"], "us.amazon.nova-lite-v1:0")
        image = call["messages"][0]["content"][0]["image"]
        self.assertEqual((image["format"], image["source"]["bytes"][:2]), ("jpeg", b"\xff\xd8"))
        described = [b for b in alerter.bodies if "Camera (AI description): One person is standing at the door." in b]
        self.assertEqual(len(described), len(with_image))
        stored = store.db.execute("SELECT COUNT(*) FROM events WHERE description IS NOT NULL").fetchone()[0]
        self.assertEqual(stored, len(with_image))

    def test_bedrock_failure_still_alerts(self):
        store, alerter = self.run_scenario(StubBedrock(fail=True))
        self.assertEqual(len(alerter.sent), len(self.fake.events))
        self.assertFalse(any("AI description" in b for b in alerter.bodies))


if __name__ == "__main__":
    unittest.main()


class OllamaTest(unittest.TestCase):
    def test_sends_base64_image_and_reads_message(self):
        import base64, io, json as js
        sent = []

        def opener(req, timeout):
            sent.append((req.full_url, js.loads(req.data)))
            return io.BytesIO(js.dumps({"message": {"content": " A box sits on the doorstep. "}}).encode())

        d = OllamaDescriber(None, {"OLLAMA_URL": "http://gpu:11434/", "OLLAMA_MODEL": "moondream"}, opener)
        self.assertEqual(d.describe(b"\xff\xd8jpeg"), "A box sits on the doorstep.")
        url, body = sent[0]
        self.assertEqual(url, "http://gpu:11434/api/chat")
        self.assertEqual((body["model"], body["stream"]), ("moondream", False))
        self.assertEqual(base64.b64decode(body["messages"][0]["images"][0]), b"\xff\xd8jpeg")

    def test_server_down_returns_none_through_call(self):
        class Ring:
            def snapshot(self, device_id, at_ms):
                return b"jpeg"

        def opener(req, timeout):
            raise ConnectionRefusedError("ollama not running")

        ev = {"device_id": "d", "start_ms": 0, "end_ms": 1000}
        self.assertIsNone(OllamaDescriber(Ring(), {}, opener)(ev))

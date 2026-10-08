"""Tests for the Strands Decider HTTP client (no server needed). Run: python3 -m unittest test_decider -v"""
import io, json, unittest

from decider import DEFAULT_MIN_P, SEEN_OPTIONS, SEEN_QUESTION, DeciderClient, state_text


class FakeOpener:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.requests = reply, error, []

    def __call__(self, req, timeout):
        self.requests.append((req.full_url, json.loads(req.data), timeout))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.reply).encode())


class ChooseTest(unittest.TestCase):
    ANSWER = {"answers": {"seen": {"type": "choice", "choice": "package", "confidence": 0.8,
                                   "probabilities": {"package": 0.834, "person": 0.1, "vehicle": 0.02,
                                                     "animal": 0.02, "nothing": 0.026}}}}

    def test_sends_one_choice_question_and_returns_the_top_option(self):
        opener = FakeOpener(self.ANSWER)
        d = DeciderClient({"DECIDER_URL": "http://box:9000/"}, opener).choose("state text", SEEN_QUESTION, SEEN_OPTIONS)
        self.assertEqual((d["label"], d["p"]), ("package", 0.83))
        self.assertEqual(set(d["probabilities"]), set(SEEN_OPTIONS))
        url, body, _ = opener.requests[0]
        self.assertEqual(url, "http://box:9000/v1/systemone")
        self.assertEqual(body, {"state": "state text", "questions": {"seen": {
            "type": "choice", "instructions": SEEN_QUESTION, "criteria": SEEN_OPTIONS}}})

    def test_any_failure_returns_none(self):
        options = {"a": "first", "b": "second"}
        self.assertIsNone(DeciderClient({}, FakeOpener(error=ConnectionRefusedError("down"))).choose("s", "?", options))
        self.assertIsNone(DeciderClient({}, FakeOpener({"answers": {}})).choose("s", "?", options))
        partial = FakeOpener({"answers": {"seen": {"probabilities": {"a": 0.9}}}})  # option b has no probability
        self.assertIsNone(DeciderClient({}, partial).choose("s", "?", options))

    def test_shipped_question(self):
        self.assertEqual(list(SEEN_OPTIONS), ["package", "person", "vehicle", "animal", "nothing"])
        self.assertEqual(DEFAULT_MIN_P, 0.5)

    def test_state_text(self):
        self.assertEqual(state_text("Front Door", "23:30", True, "human", "A man stands at the door."),
                         'Doorbell camera "Front Door" saw motion at 23:30 (night). Ring\'s label: human. '
                         "AI description of the camera frame: A man stands at the door.")
        self.assertIn("at 14:05 (day). Ring's label: none.", state_text("Back Yard", "14:05", False, None, "A cat."))


if __name__ == "__main__":
    unittest.main()

"""Tests for the Strands Decider HTTP client (no server needed). Run: python3 -m unittest test_decider -v"""
import io, json, unittest

from decider import DeciderClient


class FakeOpener:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.requests = reply, error, []

    def __call__(self, req, timeout):
        self.requests.append((req.full_url, json.loads(req.data), timeout))
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.reply).encode())


class DeciderClientTest(unittest.TestCase):
    def test_sends_one_choice_question_per_name_and_reads_p_yes(self):
        opener = FakeOpener({"answers": {"pkg": {"type": "choice", "choice": "yes", "confidence": 0.9,
                                                 "probabilities": {"yes": 0.9, "no": 0.1}}}})
        p = DeciderClient({"DECIDER_URL": "http://box:9000/"}, opener)("state text", {"pkg": {"question": "Is there a package?", "yes": "a box"}})
        self.assertEqual(p, {"pkg": 0.9})
        url, body, _ = opener.requests[0]
        self.assertEqual(url, "http://box:9000/v1/systemone")
        self.assertEqual(body["state"], "state text")
        self.assertEqual(body["questions"]["pkg"]["type"], "choice")
        self.assertEqual(body["questions"]["pkg"]["criteria"], {"yes": "a box", "no": "the answer is no"})

    def test_any_failure_returns_none(self):
        self.assertIsNone(DeciderClient({}, FakeOpener(error=ConnectionRefusedError("down")))("s", {"q": {"question": "?"}}))
        self.assertIsNone(DeciderClient({}, FakeOpener({"answers": {}}))("s", {"q": {"question": "?"}}))


if __name__ == "__main__":
    unittest.main()

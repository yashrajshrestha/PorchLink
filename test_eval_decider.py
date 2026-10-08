"""Tests for the Decider eval scoring (no server needed). Run: python3 -m unittest test_eval_decider -v"""
import json, unittest

import eval_decider
from decider import SEEN_OPTIONS


class ScoreTest(unittest.TestCase):
    CASES = [{"description": "A box.", "expect": ["package"]},
             {"description": "A courier with a box.", "expect": ["person", "package"]},
             {"description": "A cat.", "expect": ["animal"]},
             {"description": "Shadows.", "expect": ["nothing"]}]
    ANSWERS = [{"label": "package", "p": 0.9, "ms": 300}, {"label": "package", "p": 0.73, "ms": 400},
               {"label": "nothing", "p": 0.45, "ms": 350}, None]

    def test_score(self):
        s = eval_decider.score(self.CASES, self.ANSWERS, 0.5)
        self.assertEqual((s["cases"], s["right"], s["failed"], s["unsure"]), (4, 2, 1, 1))
        self.assertEqual(s["per_class"]["person"], {"cases": 1, "right": 1})  # package is acceptable for the courier
        self.assertEqual(s["per_class"]["animal"], {"cases": 1, "right": 0})
        self.assertEqual(s["confusion"]["animal"], {"nothing": 1})
        self.assertEqual(s["misses"], [("A cat.", ["animal"], "nothing", 0.45)])
        self.assertEqual((s["p50_ms"], s["p95_ms"]), (350, 400))

    def test_report(self):
        md = eval_decider.report(eval_decider.score(self.CASES, self.ANSWERS, 0.5), 0.5, "m", "hw", "2026-10-08")
        self.assertIn("**2/4 right**", md)
        self.assertIn("| animal | 0 | 0 | 0 | 0 | 1 |", md)
        self.assertIn("- A cat. — expected animal, got nothing 0.45", md)

    def test_report_when_every_call_failed(self):
        md = eval_decider.report(eval_decider.score(self.CASES[:1], [None], 0.5), 0.5, "m", "hw", "2026-10-08")
        self.assertIn("latency n/a", md)

    def test_cases_file(self):
        with open(eval_decider.CASES) as f:
            cases = json.load(f)
        self.assertGreaterEqual(len(cases), 40)
        for c in cases:
            self.assertTrue(c["expect"] and set(c["expect"]) <= set(SEEN_OPTIONS), c)
            self.assertIn(c["time"], ("day", "night"))
        for label in SEEN_OPTIONS:
            self.assertGreaterEqual(sum(c["expect"][0] == label for c in cases), 4, label)


if __name__ == "__main__":
    unittest.main()

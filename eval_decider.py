#!/usr/bin/env python3
"""Score Strands Decider on labelled doorbell descriptions and write docs/decider-eval.md.

Run (Decider server running, see README):
  python3 eval_decider.py [--url http://127.0.0.1:8000] [--min-p 0.5] [--hardware "Apple M-series GPU (MPS)"]
"""
import argparse, json, os, platform, time
from datetime import date

from decider import DEFAULT_MIN_P, SEEN_OPTIONS, SEEN_QUESTION, DeciderClient, state_text

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "eval", "decider_cases.json")
REPORT = os.path.join(HERE, "docs", "decider-eval.md")


def score(cases, answers, min_p):
    """answers[i] is {"label", "p", "ms"} for cases[i], or None when the call failed. A case counts under its
    first expected label and is right when Decider's top option is any of them. `sure_right` counts the right
    answers with p >= min_p: the ones that would fire a light pattern rather than pulse "Not sure"."""
    per_class, confusion, misses = {}, {}, []
    right = sure_right = failed = unsure = 0
    for case, a in zip(cases, answers):
        want = case["expect"][0]
        stats = per_class.setdefault(want, {"cases": 0, "right": 0})
        stats["cases"] += 1
        if a is None:
            failed += 1
            continue
        row = confusion.setdefault(want, {})
        row[a["label"]] = row.get(a["label"], 0) + 1
        unsure += a["p"] < min_p
        if a["label"] in case["expect"]:
            right += 1
            sure_right += a["p"] >= min_p
            stats["right"] += 1
        else:
            misses.append((case["description"], case["expect"], a["label"], a["p"]))
    ms = sorted(a["ms"] for a in answers if a)
    pct = lambda q: ms[min(len(ms) - 1, int(q * len(ms)))] if ms else None
    return {"cases": len(cases), "right": right, "sure_right": sure_right, "failed": failed, "unsure": unsure, "per_class": per_class,
            "confusion": confusion, "misses": misses, "p50_ms": pct(0.5), "p95_ms": pct(0.95)}


def report(s, min_p, model, hardware, day):
    latency = f"p50 {s['p50_ms']:.0f} ms, p95 {s['p95_ms']:.0f} ms" if s["p50_ms"] is not None else "n/a"
    options = list(SEEN_OPTIONS)
    return "\n".join([
        f"# Strands Decider eval — {day}", "",
        f"Model `{model}` on {hardware}. One `choice` question, \"{SEEN_QUESTION}\", with the 5 options in "
        f"`decider.SEEN_OPTIONS`; state text as in `decider.state_text`. Cases: `eval/decider_cases.json` "
        f"(42 hand-written descriptions in the style of Bedrock Nova, plus 6 written by the local vision model "
        f"qwen2.5vl:3b for the simulator's pictures). The option wording was tuned on these same cases, so this is "
        f"not a held-out score.", "",
        f"**{s['right']}/{s['cases']} right** · **{s['sure_right']}/{s['cases']} right and sure** (p ≥ {min_p}: the "
        f"light pattern fires) · unsure (p < {min_p}, the house pulses \"Not sure\") {s['unsure']} · "
        f"failed calls {s['failed']} · latency {latency}", "",
        "| class | cases | right |", "|---|---|---|",
        *[f"| {c} | {v['cases']} | {v['right']} |" for c, v in s["per_class"].items()], "",
        "Confusion (rows: expected, columns: Decider's top option)", "",
        "| expected | " + " | ".join(options) + " |", "|---" * (len(options) + 1) + "|",
        *[f"| {c} | " + " | ".join(str(row.get(o, 0)) for o in options) + " |" for c, row in s["confusion"].items()], "",
        "Misses", "",
        *([f"- {d} — expected {'/'.join(e)}, got {label} {p:.2f}" for d, e, label, p in s["misses"]] or ["- none"]), ""])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--min-p", type=float, default=DEFAULT_MIN_P)
    ap.add_argument("--model", default="strands-decider-2B-hobson-v21")
    ap.add_argument("--hardware", default=f"{platform.system()} {platform.machine()}")
    a = ap.parse_args()
    with open(CASES) as f:
        cases = json.load(f)
    client, answers = DeciderClient({"DECIDER_URL": a.url, "DECIDER_TIMEOUT": "60"}), []
    for case in cases:
        clock = "23:30" if case["time"] == "night" else "14:05"
        t = time.perf_counter()
        d = client.choose(state_text("Front Door", clock, case["time"] == "night", case.get("sub_type"), case["description"]),
                          SEEN_QUESTION, SEEN_OPTIONS)
        answers.append(d and {**d, "ms": (time.perf_counter() - t) * 1000})
    s = score(cases, answers, a.min_p)
    with open(REPORT, "w") as f:
        f.write(report(s, a.min_p, a.model, a.hardware, date.today().isoformat()))
    print(f"{s['right']}/{s['cases']} right, {s['sure_right']} right and sure, {s['unsure']} unsure, {s['failed']} failed -> {REPORT}")


if __name__ == "__main__":
    main()

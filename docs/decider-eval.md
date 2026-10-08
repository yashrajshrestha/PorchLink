# Strands Decider eval — 2026-10-08

Model `strands-decider-2B-hobson-v21` on Apple Silicon Mac, PyTorch MPS. One `choice` question, "What is at the door in this camera frame?", with the 5 options in `decider.SEEN_OPTIONS`; state text as in `decider.state_text`. Cases: `eval/decider_cases.json` (hand-written descriptions in the style of Bedrock Nova; the option wording was picked on 15 of them).

**42/42 right** · **35/42 right and sure** (p ≥ 0.5: the light pattern fires) · unsure (p < 0.5, the house pulses "Not sure") 7 · failed calls 0 · latency p50 305 ms, p95 310 ms

| class | cases | right |
|---|---|---|
| package | 8 | 8 |
| person | 9 | 9 |
| vehicle | 7 | 7 |
| animal | 7 | 7 |
| nothing | 11 | 11 |

Confusion (rows: expected, columns: Decider's top option)

| expected | package | person | vehicle | animal | nothing |
|---|---|---|---|---|---|
| package | 8 | 0 | 0 | 0 | 0 |
| person | 1 | 8 | 0 | 0 | 0 |
| vehicle | 0 | 0 | 7 | 0 | 0 |
| animal | 0 | 0 | 0 | 7 | 0 |
| nothing | 0 | 0 | 0 | 0 | 11 |

Misses

- none

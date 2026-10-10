# Strands Decider eval — 2026-10-10

Model `strands-decider-2B-hobson-v21` on Apple Silicon Mac, PyTorch MPS. One `choice` question, "What is at the door in this camera frame?", with the 5 options in `decider.SEEN_OPTIONS`; state text as in `decider.state_text`. Cases: `eval/decider_cases.json` (42 hand-written descriptions in the style of Bedrock Nova, plus 6 written by the local vision model qwen2.5vl:3b for the simulator's pictures). The option wording was tuned on these same cases, so this is not a held-out score.

**48/48 right** · **43/48 right and sure** (p ≥ 0.5: the light pattern fires) · unsure (p < 0.5, the house pulses "Not sure") 5 · failed calls 0 · latency p50 299 ms, p95 306 ms

| class | cases | right |
|---|---|---|
| package | 10 | 10 |
| person | 12 | 12 |
| vehicle | 8 | 8 |
| animal | 7 | 7 |
| nothing | 11 | 11 |

Confusion (rows: expected, columns: Decider's top option)

| expected | package | person | vehicle | animal | nothing |
|---|---|---|---|---|---|
| package | 10 | 0 | 0 | 0 | 0 |
| person | 0 | 12 | 0 | 0 | 0 |
| vehicle | 0 | 0 | 8 | 0 | 0 |
| animal | 0 | 0 | 0 | 7 | 0 |
| nothing | 0 | 0 | 0 | 0 | 11 |

Misses

- none

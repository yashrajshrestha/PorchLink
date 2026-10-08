"""Alert rules. Pure functions so they are easy to test."""
from dataclasses import dataclass
from datetime import datetime


@dataclass
class Settings:
    night_start: int = 23        # local hour when "night" begins
    night_end: int = 6           # local hour when "night" ends
    burst_count: int = 3         # this many events...
    burst_minutes: int = 30      # ...within this many minutes is a burst
    alert_every_event: bool = True


def is_night(hour, s):
    if s.night_start > s.night_end:  # window wraps past midnight, e.g. 23 -> 6
        return hour >= s.night_start or hour < s.night_end
    return s.night_start <= hour < s.night_end


def evaluate(ev, recent_starts, last_burst_start, s, tz):
    """Return a list of (kind, reason) for one new event.

    recent_starts: start times (ms) of this device's events, including this one.
    last_burst_start: event start (ms) of the last burst alert, or None.
    """
    reasons = []
    if ev["event_type"] == "button_press":
        reasons.append(("doorbell", "Doorbell pressed."))
    if ev.get("sub_type") == "human":
        reasons.append(("person", "Ring detected a person."))
    local = datetime.fromtimestamp(ev["start_ms"] / 1000, tz)
    if is_night(local.hour, s):
        reasons.append(("night", f"Unusual night-time activity at {local:%H:%M}."))

    window_start = ev["start_ms"] - s.burst_minutes * 60_000
    count = sum(1 for t in recent_starts if window_start <= t <= ev["start_ms"])
    # One burst alert per window, so a busy doorway does not send an email every event.
    if count >= s.burst_count and (last_burst_start is None or last_burst_start < window_start):
        reasons.append(("burst", f"{count} door events in the last {s.burst_minutes} minutes."))

    if not reasons and s.alert_every_event:
        reasons.append(("activity", "Door activity."))
    return reasons


def format_alert(ev, name, reasons, tz, description=None, actions=(), decision=None):
    local = datetime.fromtimestamp(ev["start_ms"] / 1000, tz)
    secs = max(0, round((ev["end_ms"] - ev["start_ms"]) / 1000))
    kinds = {k for k, _ in reasons}
    headline = ("Unusual activity" if kinds & {"night", "burst"} else "Doorbell" if "doorbell" in kinds
                else "Person" if "person" in kinds else "Activity")
    subject = f"PorchLink: {headline} at {name}, {local:%I:%M %p}"
    kind = ev["event_type"] + (f" ({ev['sub_type']})" if ev.get("sub_type") else "")
    seen = [f"Camera (AI description): {description}"] if description else []
    if decision:
        seen.append(f"Decider: {decision['label']} ({decision['p']:.2f})")
    seen += [""] if seen else []
    body = "\n".join([*seen, f"Where: {name}", f"When:  {local:%a %d %b, %I:%M:%S %p %Z}",
                      f"How long: {secs} seconds", f"Type: {kind}", "",
                      *[f"- {r}" for _, r in reasons],
                      *(["", "Home actions:", *[f"- {a}" for a in actions]] if actions else [])])
    return subject, body

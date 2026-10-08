# Night-mode dashboard — design

**Date:** 2026-10-08 · **Status:** approved by the user in the visual companion ("B night mode", then "looks right, build it") · **Branch:** `feat/night-dashboard`

## Goal

Replace the plain dashboard at `http://127.0.0.1:8080` with the approved night-mode design. It should look good on video and from across a room, and lights should visibly glow amber when they are on.

Out of scope:
- Decider choosing patterns. That is the next piece, but this UI already shows Decider's confidence when an action carries it.
- Snapshot thumbnails. No snapshots are stored yet, so each event gets an icon instead.
- Any change to the webhook, worker or store behaviour.

## What the user sees

- **Header:**
  - "Porch**Link**", with "Link" in amber.
  - A status line: a green dot and "Ring connected" when the last poll succeeded recently; a red dot and "Ring not reachable" after a failed poll; a grey dot and the mode name before the first poll.
  - The local weekday and time, and "Night" or "Day" (from the app's night window).
- **One tile per light** (virtual devices), in a responsive grid:
  - Room name, a large bulb, a big state word, and a short "why" line.
  - **On:** the bulb glows amber and slowly "breathes". The state is the automation's label (for example "Package"; "On" if there's no label). The why line reads "amber · 8 min left", or "· 40 s left" under a minute.
  - **Flashing:** the bulb blinks amber fast. The state is the label (for example "Doorbell!"; "Alert!" if there's no label). The why line reads "flashing · just now".
  - **Pulsing** (new device action `pulse`, used later for "AI not sure"): a soft, slow, paler pulse. The state is the label ("Not sure" if there's no label). The why line reads "gentle pulse · check camera".
  - **Off:** a dark bulb and the state "Off".
  - Updating a tile must not restart its animation. Tiles are created once and only their class and text change.
  - With `prefers-reduced-motion`, animations stop, and a flashing bulb shows a steady bright ring instead.
- **Legend** of the four light states.
- **Door events · last 24 hours:** one row per event, newest first. Each row has:
  - an icon (🔔 doorbell, 📦 package, 🚗 vehicle, 🧍 person, 🐾 animal, 👣 motion, 📹 live view);
  - a headline;
  - a meta line: camera · time · what the house did;
  - a coloured chip saying who decided.
  - If there are no events, it shows "No events yet."
- When BRIDGE isn't local (IoT Core), the tiles area shows "Lights are on AWS IoT Core: see their state in the IoT console" and the legend is hidden.
- The page polls every second. The events list re-renders only when the data changes.
- All text from the store or the AI is HTML-escaped.

## Event summary rules (server-side, testable)

`web.summarize(event, actions)` returns `{"headline", "detail", "chip": {"kind", "text"}, "icon"}`.

- **Headline:**
  - "Doorbell pressed" when the alert kinds include `doorbell`;
  - else the AI description, if present;
  - else "Live view opened" for `on_demand`;
  - else "Person at the door" when the kinds include `person`;
  - else "Motion".
- **Icon:**
  - `doorbell` for a doorbell event;
  - otherwise matched on whole words in the description: package / parcel / box(es) → `package`; car / vehicle / truck / van → `vehicle`; cat / dog / animal / bird → `animal`; person / people / man / woman / courier → `person`;
  - `live` for `on_demand`;
  - else `motion`.
- **Detail:**
  - the actions that ran, each described with `describe_action`, joined with "; ";
  - "nothing done" when the event alerted but no action ran;
  - "recorded at startup" for `history` events;
  - "" otherwise.
- **Chip:**
  - When any action carries `decision_p`, using the highest p:
    - `unsure` "Decider 0.61" if p < 0.8;
    - else `ai` "Decider 0.93" if an action ran;
    - else `skip` "Decider 0.88".
  - Else, in this order:
    - a doorbell event → `rule` "rule · no AI";
    - an action ran → `rule` "rule";
    - a history event → `skip` "before start";
    - otherwise → `skip` "no action".

`/api/events` returns each event with `actions` (each gaining `text` = `describe_action` with the device's display name) and `summary`.

## API and code changes

- **`bridge.VirtualDevices`:**
  - New action `pulse` sets `pulse_until = now + duration_s` (default 10). The snapshot gains `pulsing`.
  - `last` gains `label`, taken from the command.
- **`bridge.Bridge.run`:** the published payload includes `label` when the automation has one.
- **`bridge.describe_action`:** `pulse` → "gentle pulse".
- **`automations.json`:** a `label` on each automation:
  - "Night path"
  - "Doorbell!"
  - "Package"
  - "Car arriving"
- **`web.make_server(..., status=None)`** and **`web.serve(..., status=None)`:**
  - New `GET /api/status` returns `status()`, or `{}` when no `status` is passed.
  - `GET /` returns the new static page.
  - The old server-rendered rows are removed, along with the `html` and `datetime` imports if they become unused.
- **`app.make_status(health, settings, tz, poll_seconds, mode)`** returns a callable giving `{"mode", "ring_ok", "night", "time", "day"}`.
  - `ring_ok` is None before any poll result. It is True while the last successful poll is within `3 × poll_seconds + 5` s, otherwise False.
  - `main()` records `health["last_ok"]` after each successful poll and `health["failed"] = True` on a poll error, then passes `status` to `web.serve`.

## Testing

- **Unit tests (`test_dashboard.py`):**
  - pulse on and off over time with an injected clock;
  - the label is stored in `last` and published by the Bridge;
  - `describe_action` describes pulse;
  - every `summarize` rule above, including HTML-unsafe description text being returned verbatim (escaping happens in the page);
  - `make_status` states;
  - `/api/status` and `/api/events` shapes over a real server on port 0;
  - `GET /` contains the tiles, legend and events containers;
  - every automation in `automations.json` has a label.
- **Preview script:** `dashboard_preview.py` serves the real dashboard on a free port with virtual lights in every state (on, flashing, pulsing, off) and sample events (doorbell rule, Decider 0.93, Decider 0.61, skipped cat). It's used for screenshots and the demo video, and needs no Ring.
- **Visual check (Sonnet browser run)** of the preview. Screenshots must show:
  - an amber glowing tile and a flashing tile;
  - a pulse tile;
  - event rows with chips;
  - no horizontal scroll at 1280 px or 390 px.

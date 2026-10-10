# Watch live: Ring video on the dashboard (design)

**Date:** 2026-10-08
**Status:** the user approved three choices in chat: buttons in both places, a 3-minute cap with "Keep watching", and a fake camera for local tests, then a real Playground check.
**Branch:** `feat/watch-live`, stacked on `feat/decider-patterns`.
**Plan:** day 9 in `docs/hackathon-plan.md`.

## Goal

A deaf user can't answer the door by voice, so seeing the visitor answers "who's there?". The dashboard gets a **Watch live** button that plays the Ring camera inside the page.

- The video uses WebRTC through Ring's WHEP endpoint.
- The Ring token never reaches the browser.
- Video stops by itself after 3 minutes, and "Keep watching" starts a fresh session.
- The demo moment: doorbell → light flashes → tap Watch live → see the visitor.

Out of scope:
- Audio and talk-back. The video is muted, since a deaf user doesn't need sound.
- Recording or saving frames.
- More than 3 viewers at once.
- Watch live on the TV page.

## How it works

1. The browser makes a WebRTC offer: video recvonly plus audio recvonly, Google STUN, and it waits for ICE gathering to finish (3 s at most). This matches Ring's sample app (AmazonAppDev/ring-api-helloworld).
2. The browser sends `POST /api/live/<device_id>` with the raw SDP offer as `Content-Type: application/sdp`.
3. PorchLink forwards the offer to `<RING_WHEP_BASE>/v1/devices/<device_id>/media/streaming/whep/sessions`. It adds `Authorization: Bearer <RING_TOKEN>` and `Content-Type: application/sdp`.
4. Ring answers `201` with an SDP answer and a `Location` header.
5. PorchLink keeps the Location server-side under a random session id. It replies `201 {"session", "answer", "expires_in": 180, "camera"}`.
6. The browser sets the answer, and video flows **directly** from Ring's media servers to the browser. Media does not pass through PorchLink.
7. To stop: the browser sends `DELETE /api/live/session/<id>`. When the page closes, it sends `navigator.sendBeacon('/api/live/session/<id>/stop')` instead. PorchLink sends `DELETE <Location>` to Ring.
8. PorchLink also closes every session itself after 180 s, so a closed laptop lid never leaves a Ring session open.

Unlike Ring's sample, the browser never sees or sends back the Ring session URL. It only holds PorchLink's random id, so it can't make PorchLink call arbitrary URLs.

## Components

### `live.py` (new, standard library)

`LiveViews(token, base_url, cameras, opener=urllib.request.urlopen, timer=threading.Timer, cap_s=180, max_sessions=3)`

- **`cameras()`** returns `[{"id", "name"}]`, the cameras that can be watched.
- **`start(device_id, offer_sdp)`** returns `{"session", "answer", "expires_in", "camera"}`.
  - Errors raise `LiveError(status, message)` with a message a person can read. The HTTP status is the one PorchLink returns.
  - An unknown device gives 404, "No such camera".
  - An empty offer, or one over 64 KB, gives 400.
  - When the session limit is reached, it closes the oldest session.
  - Ring 401 gives 502, "Ring token expired: get a new Playground token and restart PorchLink".
  - Ring 403 or 404 gives 502, "This camera can't stream here (it may use end-to-end encryption)".
  - Any other Ring status, or a network error, gives 502, "Ring live view failed (<status>)".
  - A relative `Location` is resolved against the WHEP URL. A missing `Location` still works, but stop then has nothing to delete.
  - A timer of `cap_s` closes the session.
- **`stop(session_id)`** sends DELETE to the session's Location, at most once, and forgets the session.
  - It returns True if the session existed.
  - An unknown id returns False and makes no call.
  - DELETE errors are logged, not raised.
- Sessions sit in a dict behind a lock, because the web server is threaded.

### `web.py`

`make_server(..., live=None)` and `serve(..., live=None)`:
- `GET /api/live` returns `{"available": true, "cameras": [...]}`, or `{"available": false, "cameras": []}` when there is no `live`.
- `POST /api/live/<device_id>`:
  - The body must be `application/sdp`; anything else gives 415. This also blocks cross-site form posts, because a non-simple content type needs CORS, which PorchLink never grants.
  - The body is limited to 64 KB, with the same Content-Length handling as the webhook (400 or 413).
  - No `live` gives 503.
  - A `LiveError` returns its status and `{"error": message}`.
- `DELETE /api/live/session/<id>` and `POST /api/live/session/<id>/stop` both give 204.
- **The page:**
  - It moves from the `PAGE` string into `dashboard.html`, read once at import. The file is getting long, and HTML is easier to edit as HTML.
  - **Header:**
    - An amber **▶ Watch live** button opens the camera of the newest event, or the first camera.
    - If live view isn't available, the button is disabled with the hint "Live view needs a Ring token".
  - **Event rows:** each row whose camera can be watched gets a small **▶ Watch** button. The page uses event delegation, because the list re-renders.
  - **Player panel** (an overlay that you dismiss with Stop or Esc):
    - the camera name;
    - a muted, `playsinline`, autoplaying `<video>`;
    - a status line:
      - "Connecting…"
      - "Live · 2:47 left"
      - "Stopped after 3 minutes" with **Keep watching**
      - or the error text from the server.
  - The page stops the session on Stop, Esc, a failed connection, or `pagehide`.

### `app.py`

- When `RING_TOKEN` is set, `main()` builds `LiveViews`:
  - The token comes from `RING_TOKEN`.
  - The base is `RING_WHEP_BASE` or `RING_BASE_URL`.
  - The cameras come from `DeviceNames`, which gains `all() -> {id: name}`. It refreshes from Ring when its cache is empty, and it never raises.
- In `RING_FAKE=1` mode there is no WHEP, so Watch live is off, unless `RING_WHEP_BASE` is set (see the fake camera).
- Startup prints `| live view ON/OFF`.

### `sim/fake_whep.py` (new; test and demo only; needs `.venv`, which has aiortc and aiohttp)

- A WHEP-shaped server: `POST /v1/devices/<id>/media/streaming/whep/sessions` and `DELETE /whep/sessions/<sid>`.
- It checks for the bearer token and the `application/sdp` content type.
- It answers `201` with an SDP answer and a `Location` header.
- It streams `sim/images/*.jpg` in a loop as 640×360 video at 15 fps, changing picture every 3 s. Each frame is stamped "SIMULATED CAMERA · <device> · <time>", so nobody mistakes it for Ring.
- Run: `.venv/bin/python sim/fake_whep.py --port 9100`. The app or preview then uses `RING_WHEP_BASE=http://127.0.0.1:9100`.
- `dashboard_preview.py --whep http://127.0.0.1:9100` turns Watch live on in the preview, with the preview's devices as cameras and a dummy token.

## Error handling

- The server never forwards Ring's error body to the browser. It logs it, truncated to 300 characters, and sends the readable message.
- If ICE fails, or no video arrives within 15 s, the browser shows "Couldn't connect to the camera" and stops the session.
- The Ring token expires after 30 minutes; the 401 message tells the user what to do.

## Testing

- **`test_live.py`** (standard library, fake opener and fake timer):
  - Request shape: URL, bearer token, content type, body.
  - A 201 with a relative Location gives an absolute URL and an id that isn't the Location.
  - Each Ring status maps to its message, including a network error.
  - Unknown device gives 404; an empty or oversized offer gives 400.
  - `stop` sends DELETE once, and a second `stop` is a no-op.
  - The cap timer stops the session.
  - The oldest session is closed when the limit is reached.
  - The token never appears in any return value or error message.
- **`test_web.py` / `test_dashboard.py`** (a real server on port 0 with a stub `LiveViews`):
  - `GET /api/live` both available and unavailable.
  - POST gives 201 JSON, 415 on the wrong type, 413 when too large, 503 without `live`, and passes `LiveError` through.
  - DELETE and the beacon stop give 204.
  - The page contains the Watch live button, the player, `RTCPeerConnection`, `recvonly` and `sendBeacon`.
- **`test_live_e2e.py`** (`.venv`, like `test_grab_frame`; skipped when aiortc is missing): an aiortc client goes through the real PorchLink proxy to `fake_whep`, then receives frames that aren't black. A DELETE then reaches the fake.
- **Browser check** (Playwright, run with the Sonnet model): the preview plus the fake camera. Click Watch live, see moving video and the countdown, press Stop, and the session closes. Take screenshots at 1280 px and 390 px.
- **Real check** (needs the user's Playground token; the user pastes it into `.env`): Watch live on the real camera, with Ring's watermark visible. If the camera is E2EE, check that the friendly error appears.

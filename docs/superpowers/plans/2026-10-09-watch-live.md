# Watch Live Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Watch live" button to the dashboard that plays the Ring camera in the page. The video goes over WebRTC, and PorchLink proxies Ring's WHEP endpoint so the Ring token never reaches the browser.

**Architecture:**
- **Session handling:** the new `live.py` module (`LiveViews`) forwards the browser's SDP offer to Ring with the bearer token. It keeps Ring's session URL server-side under a random id, and closes each session on Stop or after 180 s.
- **Web server:** `web.py` adds `/api/live` endpoints. The page moves into `dashboard.html`, which gains the button, the player panel and the browser WebRTC code.
- **Fake camera:** `sim/fake_whep.py` (aiortc, `.venv` only) behaves like Ring's WHEP endpoint and streams the simulator pictures, so everything can be tested without a camera.

**Tech Stack:**
- The PorchLink app stays standard-library Python 3 (`urllib`, `http.server`, `threading`, `secrets`).
- Browser: plain JavaScript with WebRTC.
- Fake camera and the end-to-end test: `aiortc` 1.15, `aiohttp`, `Pillow` and `numpy`, installed in `.venv` from `requirements-v2.txt`.

**Spec:** `docs/superpowers/specs/2026-10-08-watch-live-design.md`

## Global Constraints

- **Standard library only.** `app.py`, `web.py` and `live.py` must not import aiortc, aiohttp, Pillow or numpy. Only `sim/fake_whep.py` and `test_live_e2e.py` use them.
- **Ring WHEP:** `POST <base>/v1/devices/<device_id>/media/streaming/whep/sessions` with `Authorization: Bearer <token>`, `Content-Type: application/sdp`, and the raw SDP offer as the body.
  - Success is `201`, with the SDP answer as the body and a `Location` header. Close the session with `DELETE <Location>` and the same bearer header.
- **The Ring token and Ring's session URL never go to the browser:**
  - not in any JSON;
  - not in any error message;
  - not in any page text.
- **Time and size limits:**
  - Cap: 180 s (`expires_in`).
  - At most 3 sessions; starting a 4th closes the oldest.
  - The offer is limited to 64 KB (`MAX_OFFER_BYTES = 65_536`).
- **Error messages** (exact):
  - `No such camera` (404)
  - `Bad video offer` (400)
  - `Ring token expired: get a new Playground token and restart PorchLink` (502 for Ring 401)
  - `This camera can't stream here (it may use end-to-end encryption)` (502 for Ring 403 or 404)
  - `Ring live view failed (<status>)` (502 for any other status)
  - `Ring live view failed (no answer)` (502 for a network error or timeout)
  - `Live view needs a Ring token` (503 when live view is off)
- **Base suite** (plain `python3`), which must pass after every task: `python3 -m unittest test_porchlink test_sim test_vision test_bridge test_decider test_webhooks test_worker test_web test_dashboard test_eval_decider test_live`. Task 1 adds `test_live`; until then, leave it out.
- **Video suite** (`.venv`): `.venv/bin/python -m unittest test_grab_frame test_live_e2e`. Task 4 adds `test_live_e2e`.
- Don't change `webhooks.py` or `worker.py`. Never commit `.env`.
- Match the surrounding style: short docstrings, sparse comments, no type hints, and `print("(…)")` for logged problems.

---

### Task 1: `live.py`, the WHEP session proxy

**Files:**
- Create: `live.py`
- Create: `test_live.py`

**Interfaces:**
- Produces:
  - `live.MAX_OFFER_BYTES` (65_536).
  - `live.LiveError(status, message)`, with attributes `.status` and `.message`.
  - `live.LiveViews(token, base_url, cameras, opener=urllib.request.urlopen, timer=threading.Timer, cap_s=180, max_sessions=3, timeout=15)`:
    - `.cameras()` returns `[{"id", "name"}]`. It is the `cameras` callable that was passed in.
    - `.start(device_id, offer_sdp)` returns `{"session", "answer", "expires_in", "camera"}`, or raises `LiveError`.
    - `.stop(session)` returns a bool and never raises.

- [ ] **Step 1: Write the failing tests** in `test_live.py`:

```python
"""Tests for Watch live (no network, no browser). Run: python3 -m unittest test_live -v"""
import io, unittest, urllib.error
from unittest import mock

from live import LiveError, LiveViews

CAMERAS = [{"id": "ava1.ring.device.front", "name": "Front Door"}]
FRONT = "ava1.ring.device.front"


class Response(io.BytesIO):
    def __init__(self, status, body=b"", headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class Opener:
    """Records every request. POSTs get `reply` (a Response, or an exception to raise); DELETEs get 204,
    or raise `delete_error` when set."""

    def __init__(self, reply, delete_error=None):
        self.reply, self.delete_error, self.requests = reply, delete_error, []

    def __call__(self, req, timeout):
        self.requests.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        if req.get_method() == "DELETE":
            if self.delete_error:
                raise self.delete_error
            return Response(204)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class FakeTimer:
    def __init__(self, interval, fn, args):
        self.interval, self.fn, self.args = interval, fn, args
        self.daemon = self.started = self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        self.fn(*self.args)


class LiveViewsTest(unittest.TestCase):
    def setUp(self):
        self.timers = []
        patcher = mock.patch("builtins.print")  # failures are logged; keep test output clean
        patcher.start()
        self.addCleanup(patcher.stop)

    def timer(self, interval, fn, args):
        t = FakeTimer(interval, fn, args)
        self.timers.append(t)
        return t

    def views(self, reply, **kw):
        self.opener = Opener(reply, kw.pop("delete_error", None))
        return LiveViews("secret-token", "https://api.example.com/", lambda: CAMERAS, self.opener, self.timer, **kw)

    def ok(self, location="/v1/whep/sessions/abc"):
        return Response(201, b"v=0 answer", {"Location": location})

    def deletes(self):
        return [(url, headers["Authorization"]) for method, url, headers, _ in self.opener.requests if method == "DELETE"]

    def test_start_forwards_the_offer_with_the_token(self):
        out = self.views(self.ok()).start(FRONT, "v=0 offer")
        self.assertEqual((out["answer"], out["expires_in"], out["camera"]), ("v=0 answer", 180, "Front Door"))
        method, url, headers, body = self.opener.requests[0]
        self.assertEqual((method, url, body), (
            "POST", "https://api.example.com/v1/devices/ava1.ring.device.front/media/streaming/whep/sessions", b"v=0 offer"))
        self.assertEqual((headers["Authorization"], headers["Content-type"]), ("Bearer secret-token", "application/sdp"))

    def test_browser_never_sees_the_token_or_ring_session_url(self):
        out = self.views(self.ok()).start(FRONT, "v=0 offer")
        self.assertGreaterEqual(len(out["session"]), 16)
        for secret in ("secret-token", "/v1/whep/sessions/abc", "abc"):
            self.assertNotIn(secret, out["session"])
        self.assertNotIn("secret-token", repr(out))

    def test_stop_deletes_the_absolute_location_once(self):
        live = self.views(self.ok())
        sid = live.start(FRONT, "v=0 offer")["session"]
        self.assertTrue(live.stop(sid))
        self.assertFalse(live.stop(sid))
        self.assertEqual(self.deletes(), [("https://api.example.com/v1/whep/sessions/abc", "Bearer secret-token")])
        self.assertTrue(self.timers[0].cancelled)

    def test_cap_timer_closes_the_session(self):
        live = self.views(self.ok(), cap_s=60)
        sid = live.start(FRONT, "v=0 offer")["session"]
        self.assertEqual((self.timers[0].interval, self.timers[0].started, self.timers[0].daemon), (60, True, True))
        self.timers[0].fire()
        self.assertFalse(live.stop(sid))  # the cap already closed it
        self.assertEqual(len(self.deletes()), 1)

    def test_oldest_session_is_closed_at_the_limit(self):
        live = self.views(self.ok(), max_sessions=2)
        a, b, c = (live.start(FRONT, "v=0 offer")["session"] for _ in range(3))
        self.assertFalse(live.stop(a))
        self.assertTrue(live.stop(b))
        self.assertTrue(live.stop(c))

    def test_ring_errors_become_readable_messages(self):
        expected = {401: "Ring token expired: get a new Playground token and restart PorchLink",
                    403: "This camera can't stream here (it may use end-to-end encryption)",
                    404: "This camera can't stream here (it may use end-to-end encryption)",
                    500: "Ring live view failed (500)"}
        for status, message in expected.items():
            error = urllib.error.HTTPError("https://x", status, "no", {}, io.BytesIO(b"Bearer secret-token rejected"))
            with self.assertRaises(LiveError) as cm:
                self.views(error).start(FRONT, "v=0 offer")
            self.assertEqual((cm.exception.status, cm.exception.message), (502, message))
        with self.assertRaises(LiveError) as cm:
            self.views(Response(200, b"v=0 answer")).start(FRONT, "v=0 offer")  # Ring answers 201, nothing else
        self.assertEqual(cm.exception.message, "Ring live view failed (200)")
        with self.assertRaises(LiveError) as cm:
            self.views(ConnectionRefusedError("down")).start(FRONT, "v=0 offer")
        self.assertEqual((cm.exception.status, cm.exception.message), (502, "Ring live view failed (no answer)"))
        self.assertEqual(self.timers, [])  # no session, no timer

    def test_bad_requests_never_reach_ring(self):
        live = self.views(self.ok())
        with self.assertRaises(LiveError) as cm:
            live.start("ava1.ring.device.back", "v=0 offer")
        self.assertEqual((cm.exception.status, cm.exception.message), (404, "No such camera"))
        for offer in ("", "v" * 65_537):
            with self.assertRaises(LiveError) as cm:
                live.start(FRONT, offer)
            self.assertEqual((cm.exception.status, cm.exception.message), (400, "Bad video offer"))
        self.assertEqual(self.opener.requests, [])

    def test_missing_location_still_plays_and_stop_makes_no_call(self):
        live = self.views(Response(201, b"v=0 answer"))
        sid = live.start(FRONT, "v=0 offer")["session"]
        self.assertTrue(live.stop(sid))
        self.assertEqual([r[0] for r in self.opener.requests], ["POST"])

    def test_close_errors_are_logged_not_raised(self):
        live = self.views(self.ok(), delete_error=ConnectionResetError("gone"))
        sid = live.start(FRONT, "v=0 offer")["session"]
        self.assertTrue(live.stop(sid))

    def test_device_id_is_url_quoted(self):
        cams = [{"id": "a/b?c", "name": "Odd"}]
        self.opener = Opener(self.ok())
        LiveViews("t", "https://api.example.com", lambda: cams, self.opener, self.timer).start("a/b?c", "v=0 offer")
        self.assertEqual(self.opener.requests[0][1],
                         "https://api.example.com/v1/devices/a%2Fb%3Fc/media/streaming/whep/sessions")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them and confirm they fail.**

Run: `python3 -m unittest test_live -v`
Expected: `ModuleNotFoundError: No module named 'live'`.

- [ ] **Step 3: Implement `live.py`:**

```python
"""Watch live: forward a browser's WebRTC offer to Ring's WHEP endpoint, so the Ring token never reaches the browser.

The browser posts its SDP offer to PorchLink; PorchLink posts it to Ring with the bearer token and returns Ring's
answer under a random session id. Video then flows from Ring to the browser directly. Each session is closed on
Ring's side when the viewer stops, or after cap_s seconds at most. Standard library only.
"""
import secrets, threading, urllib.error, urllib.request
from urllib.parse import quote, urljoin

MAX_OFFER_BYTES = 65_536  # a browser's SDP offer is a few KB
_CANT_STREAM = "This camera can't stream here (it may use end-to-end encryption)"
_RING_ERRORS = {401: "Ring token expired: get a new Playground token and restart PorchLink",
                403: _CANT_STREAM, 404: _CANT_STREAM}


class LiveError(Exception):
    """A live view could not start. `status` is the HTTP status for the browser, `message` is readable text."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


class LiveViews:
    def __init__(self, token, base_url, cameras, opener=urllib.request.urlopen, timer=threading.Timer,
                 cap_s=180, max_sessions=3, timeout=15):
        self.token, self.base, self.cameras = token, base_url.rstrip("/"), cameras  # cameras() -> [{"id", "name"}]
        self.opener, self.timer = opener, timer
        self.cap_s, self.max_sessions, self.timeout = cap_s, max_sessions, timeout
        self.lock, self.sessions = threading.Lock(), {}  # session id -> {"location", "timer"}, oldest first

    def start(self, device_id, offer_sdp):
        """Open a live view. Returns {"session", "answer", "expires_in", "camera"}; raises LiveError."""
        names = {c["id"]: c["name"] for c in self.cameras()}
        if device_id not in names:
            raise LiveError(404, "No such camera")
        if not offer_sdp or len(offer_sdp.encode()) > MAX_OFFER_BYTES:
            raise LiveError(400, "Bad video offer")
        with self.lock:
            oldest = list(self.sessions)[:max(0, len(self.sessions) - self.max_sessions + 1)]
        for session in oldest:
            self.stop(session)
        url = f"{self.base}/v1/devices/{quote(device_id, safe='')}/media/streaming/whep/sessions"
        req = urllib.request.Request(url, offer_sdp.encode(), method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/sdp"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                status, answer, location = resp.status, resp.read().decode(), resp.headers.get("Location")
        except urllib.error.HTTPError as e:
            status, answer, location = e.code, e.read().decode(errors="replace"), None
        except Exception as e:  # network error or timeout
            print(f"(live view failed: {type(e).__name__}: {e})")
            raise LiveError(502, "Ring live view failed (no answer)")
        if status != 201:
            print(f"(live view refused by Ring: {status}: {answer[:300]})")
            raise LiveError(502, _RING_ERRORS.get(status, f"Ring live view failed ({status})"))
        session = secrets.token_urlsafe(16)
        timer = self.timer(self.cap_s, self.stop, (session,))
        timer.daemon = True
        with self.lock:
            self.sessions[session] = {"location": urljoin(url, location) if location else None, "timer": timer}
        timer.start()
        return {"session": session, "answer": answer, "expires_in": self.cap_s, "camera": names[device_id]}

    def stop(self, session):
        """Close a live view on Ring's side. True if it was open. Never raises."""
        with self.lock:
            s = self.sessions.pop(session, None)
        if s is None:
            return False
        s["timer"].cancel()
        if s["location"]:
            req = urllib.request.Request(s["location"], method="DELETE", headers={"Authorization": f"Bearer {self.token}"})
            try:
                self.opener(req, timeout=self.timeout).close()
            except Exception as e:  # the session ends on Ring's side by itself eventually
                print(f"(live view close failed: {type(e).__name__}: {e})")
        return True
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `python3 -m unittest test_live -v`. Expected: all 10 pass, with no printed noise.
Then run the base suite from Global Constraints. Expected: OK.

- [ ] **Step 5: Commit.**

```bash
git add live.py test_live.py
git commit -m "feat(live): WHEP session proxy — token and Ring session URL stay server-side, 3-minute cap"
```

---

### Task 2: Live API on the web server; the page moves to `dashboard.html`; app wiring

**Files:**
- Create: `dashboard.html`. Its content is exactly the current `web.PAGE` string.
- Modify: `web.py` (load the page from the file; add the `/api/live` endpoints and the `live=None` parameter)
- Modify: `app.py` (`DeviceNames._refresh`/`all`; build `LiveViews` in `main()`; pass it to `web.serve`; startup banner)
- Modify: `.env.example` (`RING_WHEP_BASE`)
- Test: `test_live.py` (add `LiveApiTest` and `DeviceNamesTest`)

**Interfaces:**
- Consumes: `live.LiveViews`, `live.LiveError` and `live.MAX_OFFER_BYTES` from Task 1.
- Produces:
  - `web.make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="", log_bodies=False, status=None, min_p=DEFAULT_MIN_P, live=None)`; `web.serve(...)` takes the same parameters.
  - **Endpoints:**
    - `GET /api/live` returns `{"available": bool, "cameras": [{"id", "name"}]}`.
    - `POST /api/live/<device_id>` takes an `application/sdp` body. It returns 201 with the `start()` JSON, or `{"error"}` with status 400, 413, 415, 503 or `LiveError.status`.
    - `DELETE /api/live/session/<id>` returns 204.
    - `POST /api/live/session/<id>/stop` returns 204. It is for `sendBeacon`.
  - `app.DeviceNames.all()` returns `{id: name}` and never raises.

- [ ] **Step 1: Move the page out of `web.py`, with no change in behaviour.**

Run: `python3 -c "import web; open('dashboard.html', 'w', encoding='utf-8').write(web.PAGE)"`

Then in `web.py`:
- Delete the whole `PAGE = """…"""` block, from `PAGE = """<!doctype html>` through `</script></body></html>"""`.
- Put these lines in its place:

```python
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8") as _f:
    PAGE = _f.read()  # the dashboard: plain HTML, CSS and JavaScript, edited as a file
```

- Change the import line to `import json, os, re, time`.

Run the base suite **without** `test_live`. Expected: OK. The existing page test must still pass.

- [ ] **Step 2: Write the failing tests.** Append to `test_live.py`, before `if __name__`. Add `import json, threading, urllib.request` to the import line, plus `from zoneinfo import ZoneInfo`, `import app`, `import web` and `from store import Store`.

```python
class StubLive:
    def __init__(self, error=None):
        self.error, self.started, self.stopped = error, [], []

    def cameras(self):
        return CAMERAS

    def start(self, device_id, offer):
        self.started.append((device_id, offer))
        if self.error:
            raise self.error
        return {"session": "s1", "answer": "v=0 answer", "expires_in": 180, "camera": "Front Door"}

    def stop(self, session):
        self.stopped.append(session)
        return True


class LiveApiTest(unittest.TestCase):
    def start(self, live):
        server = web.make_server(Store(":memory:"), ZoneInfo("UTC"), 0, live=live)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(self, method, path, body=None, ctype="application/sdp", length=None):
        headers = {"Content-Type": ctype} if body is not None else {}
        if length is not None:  # claim a size without sending it, like test_web's webhook 413 test
            headers["Content-Length"] = str(length)
        req = urllib.request.Request(self.base + path, body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw and e.headers.get("Content-Type") == "application/json" else None

    def test_cameras_are_listed_when_live_view_is_on(self):
        self.start(StubLive())
        self.assertEqual(self.call("GET", "/api/live"), (200, {"available": True, "cameras": CAMERAS}))

    def test_live_view_off(self):
        self.start(None)
        self.assertEqual(self.call("GET", "/api/live"), (200, {"available": False, "cameras": []}))
        self.assertEqual(self.call("POST", "/api/live/" + FRONT, b"v=0 offer"), (503, {"error": "Live view needs a Ring token"}))
        self.assertEqual(self.call("DELETE", "/api/live/session/s1")[0], 204)

    def test_offer_goes_through_and_the_answer_comes_back(self):
        live = StubLive()
        self.start(live)
        self.assertEqual(self.call("POST", "/api/live/" + FRONT, b"v=0 offer"),
                         (201, {"session": "s1", "answer": "v=0 answer", "expires_in": 180, "camera": "Front Door"}))
        self.assertEqual(live.started, [(FRONT, "v=0 offer")])

    def test_only_sdp_of_a_sane_size_is_accepted(self):
        live = StubLive()
        self.start(live)
        self.assertEqual(self.call("POST", "/api/live/" + FRONT, b"v=0 offer", "text/plain"),
                         (415, {"error": "expected application/sdp"}))
        self.assertEqual(self.call("POST", "/api/live/" + FRONT, b"v", length=65_537), (413, {"error": "video offer too large"}))
        self.assertEqual(live.started, [])

    def test_live_errors_pass_through(self):
        self.start(StubLive(LiveError(502, "Ring token expired: get a new Playground token and restart PorchLink")))
        self.assertEqual(self.call("POST", "/api/live/" + FRONT, b"v=0 offer"),
                         (502, {"error": "Ring token expired: get a new Playground token and restart PorchLink"}))

    def test_stop_by_delete_or_beacon(self):
        live = StubLive()
        self.start(live)
        self.assertEqual(self.call("DELETE", "/api/live/session/s1")[0], 204)
        self.assertEqual(self.call("POST", "/api/live/session/s2/stop", b"", "text/plain")[0], 204)
        self.assertEqual(live.stopped, ["s1", "s2"])

    def test_unknown_live_paths(self):
        self.start(StubLive())
        self.assertEqual(self.call("POST", "/api/live/a/b", b"v=0 offer")[0], 404)
        self.assertEqual(self.call("DELETE", "/api/live/other")[0], 404)
        self.assertEqual(self.call("POST", "/webhook", b"{}", "application/json")[0], 404)  # webhooks still off without enqueue


class DeviceNamesTest(unittest.TestCase):
    def test_all_lists_cameras_and_never_raises(self):
        class Ring:
            def __init__(self, fail):
                self.fail = fail

            def devices(self):
                if self.fail:
                    raise ConnectionError("down")
                return [{"id": "d1", "attributes": {"name": "Front Door"}}]

        self.assertEqual(app.DeviceNames(Ring(False)).all(), {"d1": "Front Door"})
        with mock.patch("builtins.print"):
            self.assertEqual(app.DeviceNames(Ring(True)).all(), {})
```

Also add this check to `test_live.py` (inside `DeviceNamesTest` is fine):

```python
    def test_page_is_still_served_from_the_file(self):
        with open(web.os.path.join(web.os.path.dirname(web.os.path.abspath(web.__file__)), "dashboard.html"), encoding="utf-8") as f:
            self.assertEqual(web.PAGE, f.read())
```

- [ ] **Step 3: Run them and confirm they fail.**

Run: `python3 -m unittest test_live -v`
Expected: `LiveApiTest` errors (`make_server() got an unexpected keyword argument 'live'`), and `DeviceNamesTest` fails (no `all`).

- [ ] **Step 4: Implement in `web.py`.**
- Add `from live import LiveError, MAX_OFFER_BYTES` after `from decider import DEFAULT_MIN_P`.
- Change both signatures to end with `status=None, min_p=DEFAULT_MIN_P, live=None):`. `serve` passes `live` on to `make_server`.
- Inside `Handler`, add these helpers (next to `_send`):

```python
        def _json(self, code, obj):
            self._send(code, "application/json", json.dumps(obj))

        def _length(self):
            try:
                return int(self.headers.get("Content-Length") or "")
            except ValueError:
                return -1
```

- In `do_POST`, use `length = self._length()` instead of the inline `try/int/except`; the webhook behaviour is unchanged. Make the first lines of `do_POST`:

```python
        def do_POST(self):
            if self.path.startswith("/api/live/"):
                return self._live_post(self.path[len("/api/live/"):].split("/"))
```

- Add these methods:

```python
        def _live_post(self, parts):
            if len(parts) == 3 and parts[0] == "session" and parts[2] == "stop":  # sendBeacon when the page closes
                if live:
                    live.stop(parts[1])
                return self._send(204, "text/plain", "")
            if len(parts) != 1 or not parts[0]:
                return self._send(404, "text/plain", "not found")
            if not live:
                return self._json(503, {"error": "Live view needs a Ring token"})
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/sdp":  # also blocks cross-site form posts: this type needs CORS, never granted
                return self._json(415, {"error": "expected application/sdp"})
            length = self._length()
            if length < 0:
                return self._json(400, {"error": "bad Content-Length"})
            if length > MAX_OFFER_BYTES:
                return self._json(413, {"error": "video offer too large"})
            try:
                out = live.start(unquote(parts[0]), self.rfile.read(length).decode(errors="replace"))
            except LiveError as e:
                return self._json(e.status, {"error": e.message})
            self._json(201, out)

        def do_DELETE(self):
            prefix = "/api/live/session/"
            session = self.path[len(prefix):] if self.path.startswith(prefix) else ""
            if not session or "/" in session:
                return self._send(404, "text/plain", "not found")
            if live:
                live.stop(session)
            self._send(204, "text/plain", "")
```

- Add `from urllib.parse import unquote` to the imports.
- In `do_GET`, add before the `/api/devices` branch:

```python
            if self.path == "/api/live":
                return self._json(200, {"available": bool(live), "cameras": live.cameras() if live else []})
```

**`app.py`:**
- Add `from live import LiveViews` to the imports.
- Replace `DeviceNames` with:

```python
class DeviceNames:
    """Device id -> name. Webhooks only carry the id; refresh from Ring on a miss, never raise."""

    def __init__(self, ring):
        self.ring, self.names = ring, {}

    def _refresh(self):
        try:
            self.names.update({d["id"]: device_name(d) for d in self.ring.devices()})
        except Exception as e:
            print(f"(device names unavailable: {type(e).__name__}: {e})")

    def __call__(self, ev):
        device_id = ev["device_id"]
        if device_id not in self.names:
            self._refresh()
        return self.names.get(device_id, f"Ring device ...{device_id[-6:]}")

    def all(self):
        """Every camera, id -> name (for Watch live)."""
        if not self.names:
            self._refresh()
        return dict(self.names)
```

- In `main()`, replace `name_for=DeviceNames(ring)).start()` with `name_for=names).start()`. Add these lines before the `worker = …` line:

```python
    names = DeviceNames(ring)
    # Watch live: the browser's video offer goes to Ring through PorchLink (see live.py). Off in FAKE mode
    # unless RING_WHEP_BASE points at the simulator's camera (sim/fake_whep.py).
    whep_base = env.get("RING_WHEP_BASE") or ("" if fake else env.get("RING_BASE_URL", "https://api.amazonvision.com"))
    live = LiveViews(env.get("RING_TOKEN") or "sim-token", whep_base,
                     lambda: [{"id": i, "name": n} for i, n in names.all().items()]) if whep_base else None
```

- Add `, live` to the end of the `web.serve` thread's args tuple, after `min_p`.
- In the startup banner, change `f"devices: {…}, {len(config['automations'])} automations")` to end with `f"devices: {…}, {len(config['automations'])} automations | live view {'ON' if live else 'OFF'}")`. Keep the `devices:` expression as it is.

**`.env.example`:** after the `RING_BASE_URL` line (or at the end of the Ring section if that line is absent), add:

```
# Watch live (dashboard video) uses Ring's WHEP endpoint at RING_BASE_URL. For a local test without a camera,
# run .venv/bin/python sim/fake_whep.py and set RING_WHEP_BASE=http://127.0.0.1:9100
RING_WHEP_BASE=
```

- [ ] **Step 5: Run the tests and confirm they pass.**

Run: `python3 -m unittest test_live -v`. Expected: all pass, pristine.
Then run the base suite, which now includes `test_live`. Expected: OK.

- [ ] **Step 6: Commit.**

```bash
git add dashboard.html web.py app.py .env.example test_live.py
git commit -m "feat(web): /api/live endpoints proxy Ring WHEP; dashboard page moves to dashboard.html"
```

---

### Task 3: The Watch live button and player on the dashboard

**Files:**
- Modify: `dashboard.html`
- Test: `test_dashboard.py` (one page test)

**Interfaces:**
- Consumes the endpoints from Task 2:
  - `GET /api/live` returns `{"available", "cameras": [{"id", "name"}]}`.
  - `POST /api/live/<id>` sends an `application/sdp` offer and gets back `{"session", "answer", "expires_in", "camera"}` or `{"error"}`.
  - `DELETE /api/live/session/<id>`.
  - `POST /api/live/session/<id>/stop` (beacon).
- `/api/events` items carry `device_id`, which is what the buttons use.

- [ ] **Step 1: Write the failing test.** Add to `ApiTest` in `test_dashboard.py`:

```python
    def test_page_has_watch_live(self):
        self.start()
        body = self.get("/")[2]
        for marker in ('id="watch-live"', 'id="live"', 'id="live-video"', 'id="live-status"', 'id="live-stop"',
                       'id="live-again"', "Keep watching", "muted", "playsinline", "RTCPeerConnection", "recvonly",
                       "application/sdp", "/api/live/session/", "sendBeacon", "data-watch", "Live view needs a Ring token"):
            self.assertIn(marker, body)
```

- [ ] **Step 2: Run it and confirm it fails.**

Run: `python3 -m unittest test_dashboard.ApiTest.test_page_has_watch_live -v`
Expected: FAIL (`'id="watch-live"' not found`).

- [ ] **Step 3: Implement in `dashboard.html`.**

**CSS:** add these rules before the `@media (max-width:560px)` line:

```css
button{font:inherit;cursor:pointer}
.right{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.watch{background:var(--amber);color:#1b1404;border:0;border-radius:999px;padding:8px 16px;font-weight:800}
.watch:disabled{background:#2a2f39;color:var(--muted);cursor:not-allowed}
.watch.small{padding:4px 11px;font-size:12px}
.acts{margin-left:auto;display:flex;gap:8px;align-items:center}
.live{background:var(--card);border:1px solid #5a4416;border-radius:20px;padding:14px;margin-bottom:22px}
.live[hidden]{display:none}
.live-head,.live-foot{display:flex;justify-content:space-between;align-items:center;gap:12px}
.live-head b{font-size:17px}.live-foot{margin-top:10px;color:var(--muted);font-size:14px}
.live video{display:block;width:100%;aspect-ratio:16/9;background:#000;border-radius:12px;margin-top:10px;object-fit:contain}
.stop{background:#2a2f39;color:var(--text);border:1px solid var(--line);border-radius:999px;padding:6px 14px;font-weight:700}
```

Then:
- In the existing `.chip{…}` rule, remove `margin-left:auto;`, since `.acts` now pushes the chip right.
- In the mobile rule, replace `.chip{margin-left:66px}` with `.acts{margin-left:66px}`.

**HTML:** replace the header `<div class="top">…</div>` (the brand and the status) with:

```html
<div class="top"><div class="brand">Porch<span>Link</span></div>
<div class="right"><button class="watch" id="watch-live" disabled title="Live view needs a Ring token">▶ Watch live</button>
<div class="status"><span class="dot" id="dot"></span><span id="status-text">Connecting…</span></div></div></div>
<section class="live" id="live" hidden aria-label="Live camera">
<div class="live-head"><b id="live-title">Live</b><button class="stop" id="live-stop">Stop</button></div>
<video id="live-video" autoplay muted playsinline></video>
<div class="live-foot"><span id="live-status" role="status">Connecting…</span><button class="watch" id="live-again" hidden>Keep watching</button></div>
</section>
```

**JavaScript:**

(a) In `eventRow`, replace the trailing `'<span class="chip '…'</span></div>'` part so the row ends with an `acts` group:

```js
function eventRow(e){
  const s=e.summary,meta=[e.device_name||"",clock(e.start_ms),s.detail].filter(Boolean).map(esc).join(" · ");
  const watch=cameras[e.device_id]?'<button class="watch small" data-watch="'+esc(e.device_id)+'">▶ Watch</button>':"";
  return '<div class="ev" data-testid="event"><div class="icon" aria-hidden="true">'+(ICON[s.icon]||ICON.motion)+
    '</div><div><b>'+esc(s.headline)+'</b><div class="meta">'+meta+'</div></div><div class="acts">'+watch+
    '<span class="chip '+esc(s.chip.kind)+'">'+esc(s.chip.text)+'</span></div></div>';
}
```

(b) In `tick()`, right after `const key=JSON.stringify(evs);`, add `newestCamera=evs.length?evs[0].device_id:null;`.

(c) Replace the last line `tick();setInterval(tick,1000);` with the block below:

```js
// Watch live: the browser makes the WebRTC offer; PorchLink forwards it to Ring with the token (see live.py).
const $=id=>document.getElementById(id);
let cameras={},newestCamera=null,pc=null,session=null,countdown=null,currentCam=null;
async function loadLive(){
  let r={available:false,cameras:[]};
  try{r=await (await fetch("/api/live")).json();}catch(err){}
  cameras={};for(const c of r.cameras)cameras[c.id]=c.name;
  const b=$("watch-live");b.disabled=!r.available||!r.cameras.length;
  b.title=b.disabled?"Live view needs a Ring token":"";
  lastEvents="";  // re-render rows so their ▶ Watch buttons match the camera list
}
const say=text=>{$("live-status").textContent=text;};
const closeOnRing=s=>fetch("/api/live/session/"+encodeURIComponent(s),{method:"DELETE"}).catch(()=>{});
function stopLive(reason){
  clearInterval(countdown);countdown=null;
  if(pc){pc.close();pc=null;}
  $("live-video").srcObject=null;
  if(session){closeOnRing(session);session=null;}
  if(reason)say(reason);
}
function startCountdown(secs){
  const end=Date.now()+secs*1000;
  const show=()=>{
    const left=Math.max(0,Math.round((end-Date.now())/1000));
    if(!left){stopLive("Stopped after "+Math.round(secs/60)+" minutes");$("live-again").hidden=false;return;}
    say("Live · "+Math.floor(left/60)+":"+String(left%60).padStart(2,"0")+" left");
  };
  show();countdown=setInterval(show,1000);
}
function watchForVideo(mine){
  const v=$("live-video"),t=setTimeout(()=>{if(pc===mine&&!v.videoWidth)stopLive("Couldn't connect to the camera");},15000);
  v.addEventListener("playing",()=>clearTimeout(t),{once:true});
  mine.addEventListener("connectionstatechange",()=>{if(pc===mine&&mine.connectionState==="failed")stopLive("Couldn't connect to the camera");});
}
async function watch(cam){
  if(!cam)return;
  stopLive();
  currentCam=cam;$("live").hidden=false;$("live-again").hidden=true;
  $("live-title").textContent=(cameras[cam]||"Camera")+" · LIVE";say("Connecting…");
  const mine=pc=new RTCPeerConnection({iceServers:[{urls:"stun:stun.l.google.com:19302"}]});
  try{
    mine.addTransceiver("video",{direction:"recvonly"});mine.addTransceiver("audio",{direction:"recvonly"});
    mine.ontrack=e=>{if(e.track.kind==="video")$("live-video").srcObject=e.streams[0]||new MediaStream([e.track]);};
    await mine.setLocalDescription(await mine.createOffer());
    await new Promise(done=>{  // Ring wants every ICE candidate in the offer
      if(mine.iceGatheringState==="complete")return done();
      const t=setTimeout(done,3000);
      mine.addEventListener("icegatheringstatechange",()=>{if(mine.iceGatheringState==="complete"){clearTimeout(t);done();}});
    });
    if(pc!==mine)return;  // stopped while gathering
    const r=await fetch("/api/live/"+encodeURIComponent(cam),{method:"POST",headers:{"Content-Type":"application/sdp"},body:mine.localDescription.sdp});
    const out=await r.json();
    if(!r.ok)throw new Error(out.error||"Live view failed");
    if(pc!==mine){closeOnRing(out.session);return;}
    session=out.session;
    await mine.setRemoteDescription({type:"answer",sdp:out.answer});
    startCountdown(out.expires_in);watchForVideo(mine);
  }catch(err){if(pc===mine)stopLive(err.message||"Couldn't connect to the camera");}
}
$("watch-live").onclick=()=>watch(cameras[newestCamera]?newestCamera:Object.keys(cameras)[0]);
$("live-stop").onclick=()=>{stopLive();$("live").hidden=true;};
$("live-again").onclick=()=>watch(currentCam);
eventsEl.addEventListener("click",e=>{const b=e.target.closest("[data-watch]");if(b)watch(b.dataset.watch);});
addEventListener("keydown",e=>{if(e.key==="Escape"&&!$("live").hidden){stopLive();$("live").hidden=true;}});
addEventListener("pagehide",()=>{if(session)navigator.sendBeacon("/api/live/session/"+encodeURIComponent(session)+"/stop");});
loadLive().then(()=>{tick();setInterval(tick,1000);setInterval(loadLive,30000);});
```

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `python3 -m unittest test_dashboard -v`. Expected: all pass, including the existing page test (it checks markers such as `id="status-text"` and `prefers-reduced-motion`).
Then run the base suite. Expected: OK.

Also check the JavaScript syntax:

```bash
python3 -c "import re,web; open('/tmp/dash.js','w').write(re.search(r'<script>(.*)</script>', web.PAGE, re.S).group(1))" && node --check /tmp/dash.js && echo JS OK
```

If `node` is not installed, say so in the report.

- [ ] **Step 5: Commit.**

```bash
git add dashboard.html test_dashboard.py
git commit -m "feat(dashboard): Watch live button, per-event ▶ Watch, muted player with 3-minute countdown and Keep watching"
```

---

### Task 4: Fake Ring camera, preview switch, and a video end-to-end test

**Files:**
- Create: `sim/fake_whep.py`
- Create: `test_live_e2e.py`
- Modify: `dashboard_preview.py` (the `--whep` option)

**Interfaces:**
- Consumes:
  - `live.LiveViews` (Task 1);
  - `web.make_server(..., live=)` and `web.serve(..., live=)` (Task 2).
- Produces:
  - `sim.fake_whep.make_app(hold_s=3)`, an aiohttp `web.Application`. `app["sessions"]` is a dict of open sessions.
  - `PorchTrack(device_id, hold_s)`.

- [ ] **Step 1: Write the failing test** `test_live_e2e.py`:

```python
"""End to end for Watch live: an aiortc viewer -> PorchLink's /api/live proxy -> the fake Ring WHEP camera.

Needs .venv (aiortc, aiohttp, numpy). Run: .venv/bin/python -m unittest test_live_e2e -v
Skipped under plain python3.
"""
import asyncio, json, threading, unittest, urllib.request
from zoneinfo import ZoneInfo

try:
    from aiohttp import web as aioweb
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from sim.fake_whep import make_app
except ImportError:
    make_app = None

import web
from live import LiveViews
from store import Store


@unittest.skipIf(make_app is None, "needs .venv with aiortc")
class WatchLiveEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def test_viewer_gets_porch_video_through_porchlink(self):
        fake = make_app(hold_s=1)
        runner = aioweb.AppRunner(fake)
        await runner.setup()
        await aioweb.TCPSite(runner, "127.0.0.1", 0).start()
        self.addAsyncCleanup(runner.cleanup)
        whep = f"http://127.0.0.1:{runner.addresses[0][1]}"

        live = LiveViews("test-token", whep, lambda: [{"id": "front", "name": "Front Door"}])
        server = web.make_server(Store(":memory:"), ZoneInfo("UTC"), 0, live=live)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(method, path, body=None):  # blocking HTTP runs in a thread: the fake camera shares this event loop
            req = urllib.request.Request(base + path, body, method=method, headers={"Content-Type": "application/sdp"})
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return json.loads(raw) if raw else None

        pc = RTCPeerConnection()
        self.addAsyncCleanup(pc.close)
        pc.addTransceiver("video", direction="recvonly")
        got = asyncio.get_running_loop().create_future()

        @pc.on("track")
        def on_track(track):
            async def read():
                for _ in range(5):
                    await track.recv()
                got.set_result(await track.recv())
            asyncio.ensure_future(read())

        await pc.setLocalDescription(await pc.createOffer())
        out = await asyncio.to_thread(call, "POST", "/api/live/front", pc.localDescription.sdp.encode())
        self.assertEqual((out["camera"], out["expires_in"]), ("Front Door", 180))
        await pc.setRemoteDescription(RTCSessionDescription(sdp=out["answer"], type="answer"))
        frame = await asyncio.wait_for(got, 20)
        self.assertEqual((frame.width, frame.height), (640, 360))
        self.assertGreater(frame.to_ndarray(format="rgb24").mean(), 20)  # a porch picture, not a black frame
        self.assertEqual(len(fake["sessions"]), 1)

        await asyncio.to_thread(call, "DELETE", "/api/live/session/" + out["session"])
        self.assertEqual(fake["sessions"], {})  # PorchLink closed the session on the camera's side

    async def test_fake_camera_wants_a_bearer_token(self):
        fake = make_app()
        runner = aioweb.AppRunner(fake)
        await runner.setup()
        await aioweb.TCPSite(runner, "127.0.0.1", 0).start()
        self.addAsyncCleanup(runner.cleanup)
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/v1/devices/front/media/streaming/whep/sessions"

        def post():
            req = urllib.request.Request(url, b"v=0", method="POST", headers={"Content-Type": "application/sdp"})
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                return e.code

        self.assertEqual(await asyncio.to_thread(post), 401)


if __name__ == "__main__":
    unittest.main()
```

(Add `import urllib.error` to the imports.)

- [ ] **Step 2: Run it and confirm it fails.**

Run: `.venv/bin/python -m unittest test_live_e2e -v`
Expected: the tests are **skipped** (`needs .venv with aiortc`) because `sim.fake_whep` doesn't exist yet. That counts as failing for this step. Confirm by running `.venv/bin/python -c "import sim.fake_whep"`, which should give `ModuleNotFoundError`.

- [ ] **Step 3: Implement `sim/fake_whep.py`:**

```python
#!/usr/bin/env python3
"""A pretend Ring WHEP camera for testing Watch live without a Ring device (needs .venv: aiortc, aiohttp, Pillow).

It answers like Ring (201 + SDP answer + Location; DELETE closes the session) and streams the simulator's porch
pictures as live video. Every frame is stamped "SIMULATED CAMERA" so nobody mistakes it for Ring.

Run: .venv/bin/python sim/fake_whep.py [--port 9100]
Then start PorchLink (or dashboard_preview.py --whep) with RING_WHEP_BASE=http://127.0.0.1:9100
"""
import argparse, asyncio, glob, os, secrets, time

import av
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGES = sorted(glob.glob(os.path.join(HERE, "images", "*.jpg")))


class PorchTrack(VideoStreamTrack):
    """The sample pictures in a loop, a new one every hold_s seconds, stamped with the camera and the time."""

    def __init__(self, device_id, hold_s=3):
        super().__init__()
        self.device_id, self.hold_s, self.started = device_id, hold_s, time.time()
        self.pictures = [Image.open(p).convert("RGB").resize((640, 360)) for p in IMAGES]

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        img = self.pictures[int((time.time() - self.started) / self.hold_s) % len(self.pictures)].copy()
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 640, 26), fill=(0, 0, 0))
        draw.text((8, 7), f"SIMULATED CAMERA - {self.device_id} - {time.strftime('%H:%M:%S')}", fill=(255, 194, 77))
        frame = av.VideoFrame.from_image(img)
        frame.pts, frame.time_base = pts, time_base
        return frame


def make_app(hold_s=3):
    sessions = {}

    async def create(request):
        if not request.headers.get("Authorization", "").startswith("Bearer "):
            return web.Response(status=401, text="missing bearer token")
        if request.content_type != "application/sdp":
            return web.Response(status=415, text="expected application/sdp")
        pc = RTCPeerConnection()
        await pc.setRemoteDescription(RTCSessionDescription(sdp=await request.text(), type="offer"))
        pc.addTrack(PorchTrack(request.match_info["device_id"], hold_s))
        await pc.setLocalDescription(await pc.createAnswer())  # aiortc gathers all ICE candidates here
        sid = secrets.token_hex(8)
        sessions[sid] = pc
        return web.Response(status=201, text=pc.localDescription.sdp, content_type="application/sdp",
                            headers={"Location": f"/whep/sessions/{sid}"})

    async def delete(request):
        pc = sessions.pop(request.match_info["sid"], None)
        if pc is None:
            return web.Response(status=404)
        await pc.close()
        return web.Response(status=204)

    async def close_all(app):
        await asyncio.gather(*(pc.close() for pc in sessions.values()))
        sessions.clear()

    app = web.Application()
    app.router.add_post("/v1/devices/{device_id}/media/streaming/whep/sessions", create)
    app.router.add_delete("/whep/sessions/{sid}", delete)
    app.on_shutdown.append(close_all)
    app["sessions"] = sessions
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--hold", type=float, default=3, help="seconds per picture")
    a = ap.parse_args()
    print(f"Fake Ring camera (WHEP): http://127.0.0.1:{a.port}  — set RING_WHEP_BASE to this")
    web.run_app(make_app(a.hold), host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()
```

If aiohttp warns that setting `app["sessions"]` should use an `AppKey`, use `SESSIONS = web.AppKey("sessions", dict)` and `app[SESSIONS]` in both the module and the test, so the output stays clean.

**`dashboard_preview.py`:**
- Add the option `p.add_argument("--whep", help="fake camera URL (sim/fake_whep.py) to turn on Watch live")`.
- In `main()`, before `web.serve(...)`, add:

```python
    live = None
    if a.whep:
        from live import LiveViews
        live = LiveViews("preview-token", a.whep, lambda: [{"id": "front", "name": "Front Door"},
                                                          {"id": "back", "name": "Back Yard"}])
```

- Change the serve call to `web.serve(store, TZ, a.port, None, devices, "", False, status, live=live)`.
- Add to the docstring: `With a fake camera: .venv/bin/python sim/fake_whep.py & python3 dashboard_preview.py --whep http://127.0.0.1:9100`.

- [ ] **Step 4: Run the tests and confirm they pass.**

Run: `.venv/bin/python -m unittest test_live_e2e -v`. Expected: 2 tests pass, not skipped, with no warnings.
Then run the video suite, `.venv/bin/python -m unittest test_grab_frame test_live_e2e`. Expected: OK.
Then run the base suite with `python3`. Expected: OK. `test_live_e2e` is not in it.
Also check by hand that the server starts: run `.venv/bin/python sim/fake_whep.py --port 9101 &`, wait 2 s, then `curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "Content-Type: application/sdp" --data "v=0" http://127.0.0.1:9101/v1/devices/x/media/streaming/whep/sessions`. Expected: `401`. Then kill the process.

- [ ] **Step 5: Commit.**

```bash
git add sim/fake_whep.py test_live_e2e.py dashboard_preview.py
git commit -m "test(live): fake Ring WHEP camera streams the porch pictures; end-to-end video through PorchLink"
```

---

### Task 5: Docs

**Files:**
- Modify: `README.md`, `docs/hackathon-plan.md`

- [ ] **Step 1: README.** Add a section `## Watch live` after the dashboard section (or after the Strands Decider section, if there is no dashboard section):

```
## Watch live

The dashboard's **▶ Watch live** button (and **▶ Watch** on each door event) plays the Ring camera in the page,
muted, for up to 3 minutes; **Keep watching** starts a new session. The browser makes the WebRTC offer and PorchLink
forwards it to Ring's WHEP endpoint with the token, so the token and Ring's session URL never reach the browser.
Video goes straight from Ring to the browser. Ring's watermark is on every frame; cameras with end-to-end encryption
can't stream, and the page says so.

Try it without a camera (fake WHEP camera streams the simulator's pictures, stamped "SIMULATED CAMERA"):

    .venv/bin/python sim/fake_whep.py &                        # port 9100
    python3 dashboard_preview.py --whep http://127.0.0.1:9100  # open http://127.0.0.1:8090

With a real Ring Playground token, Watch live is on automatically (`RING_TOKEN` in `.env`). The token lasts 30 minutes.
```

Then add `test_live` to the README's test command, and add the line `.venv/bin/python -m unittest test_grab_frame test_live_e2e   # video tests (needs requirements-v2.txt)`, replacing the existing `test_grab_frame`-only line.

- [ ] **Step 2: Hackathon plan.** In `docs/hackathon-plan.md`, change the Day 9 heading's start from `**Day 9 — Watch live (agreed 2026-10-08).**` to `**Day 9 — Watch live (agreed 2026-10-08; built 2026-10-09 on feat/watch-live, real-camera check pending).**`

- [ ] **Step 3: Run the base suite.** Expected: OK. Docs don't affect tests.

- [ ] **Step 4: Commit.**

```bash
git add README.md docs/hackathon-plan.md
git commit -m "docs: Watch live — how it works and how to try it with the fake camera"
```

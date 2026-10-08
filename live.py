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

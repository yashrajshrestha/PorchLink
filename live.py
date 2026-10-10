"""Watch live: forward a browser's WebRTC offer to Ring's WHEP endpoint, so the Ring token never reaches the browser.

The browser posts its SDP offer to PorchLink; PorchLink posts it to Ring with the bearer token and returns Ring's
answer under a random session id. Video then flows from Ring to the browser directly. Each session is closed on
Ring's side when the viewer stops, or after cap_s seconds at most. Standard library only.
"""
import ipaddress, secrets, threading, urllib.error, urllib.request
from urllib.parse import quote, urljoin, urlparse

from ring_client import _NO_REDIRECT  # a 3xx is an error here: the token must never follow a redirect

MAX_OFFER_BYTES = 65_536  # a browser's SDP offer is a few KB
_CANT_STREAM = "This camera can't stream here (it may use end-to-end encryption)"
_RING_ERRORS = {401: "Ring token expired: get a new Playground token and restart PorchLink",
                403: _CANT_STREAM, 404: _CANT_STREAM}


class LiveError(Exception):
    """A live view could not start. `status` is the HTTP status for the browser, `message` is readable text."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _start_thread(fn):
    threading.Thread(target=fn, daemon=True).start()


def _is_ring_host(base, location):
    """May the token go to `location`? Same scheme and host as the WHEP URL, or (both https) a sibling under the
    same registered domain, since Ring may answer with its own media host."""
    b, loc = urlparse(base), urlparse(location)
    if (b.scheme, b.netloc) == (loc.scheme, loc.netloc):
        return True
    host, other = b.hostname or "", loc.hostname or ""
    if b.scheme != "https" or loc.scheme != "https" or not host or not other:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # an IP has no domain to share
    except ValueError:
        pass
    labels = host.split(".")
    return len(labels) >= 2 and other.endswith("." + ".".join(labels[-2:]))


class LiveViews:
    def __init__(self, token, base_url, cameras, opener=_NO_REDIRECT.open, timer=threading.Timer,
                 cap_s=180, max_sessions=3, timeout=15, spawn=_start_thread):
        self.token, self.base, self.cameras = token, base_url.rstrip("/"), cameras  # cameras() -> [{"id", "name"}]
        self.opener, self.timer, self.spawn = opener, timer, spawn  # spawn(fn) runs fn off the request path
        self.host_logged = False
        self.cap_s, self.max_sessions, self.timeout = cap_s, max_sessions, timeout
        self.lock, self.sessions = threading.Lock(), {}  # session id -> {"location", "timer"}, oldest first

    def start(self, device_id, offer_sdp):
        """Open a live view. Returns {"session", "answer", "expires_in", "camera"}; raises LiveError."""
        names = {c["id"]: c["name"] for c in self.cameras()}
        if device_id not in names:
            raise LiveError(404, "No such camera")
        try:
            body = offer_sdp.encode()
        except (AttributeError, UnicodeError):  # not text, or not encodable
            raise LiveError(400, "Bad video offer")
        if not body or len(body) > MAX_OFFER_BYTES:
            raise LiveError(400, "Bad video offer")
        url = f"{self.base}/v1/devices/{quote(device_id, safe='')}/media/streaming/whep/sessions"
        req = urllib.request.Request(url, body, method="POST",
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/sdp"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                status, answer, location = resp.status, resp.read().decode(), resp.headers.get("Location")
        except urllib.error.HTTPError as e:
            try:
                status, answer, location = e.code, e.read().decode(errors="replace"), None
            except Exception:  # an unreadable body still maps by status
                status, answer, location = e.code, "", None
            finally:
                e.close()
        except Exception as e:  # network error or timeout
            print(f"(live view failed: {type(e).__name__}: {e})")
            raise LiveError(502, "Ring live view failed (no answer)")
        if status != 201:
            print(f"(live view refused by Ring: {status}: {answer[:300]})")
            raise LiveError(502, _RING_ERRORS.get(status, f"Ring live view failed ({status})"))
        location = urljoin(url, location) if location else None
        if not self.host_logged:  # once: shows which host real Ring uses for sessions
            self.host_logged = True
            print(f"(live view session host: {urlparse(location or url).hostname})")
        if location and not _is_ring_host(url, location):  # the token goes only to Ring's own hosts
            print("(live view: ignoring Location on another host)")
            location = None
        session = secrets.token_urlsafe(16)
        timer = self.timer(self.cap_s, self.stop, (session,))
        timer.daemon = True
        with self.lock:
            self.sessions[session] = {"location": location, "timer": timer}
            evicted = []
            while len(self.sessions) > self.max_sessions:  # over the limit: the oldest viewers go
                evicted.append(self.sessions.pop(next(iter(self.sessions))))
        timer.start()
        for old in evicted:  # Ring's DELETE can be slow; the new viewer should not wait for it
            self.spawn(lambda old=old: self._close(old))
        return {"session": session, "answer": answer, "expires_in": self.cap_s, "camera": names[device_id]}

    def stop(self, session):
        """Close a live view on Ring's side. True if it was open. Never raises."""
        with self.lock:
            s = self.sessions.pop(session, None)
        if s is None:
            return False
        self._close(s)
        return True

    def _close(self, s):
        """Cancel a session's cap timer and delete it on Ring's side, if Ring gave us its URL. Never raises."""
        s["timer"].cancel()
        if s["location"]:
            req = urllib.request.Request(s["location"], method="DELETE", headers={"Authorization": f"Bearer {self.token}"})
            try:
                self.opener(req, timeout=self.timeout).close()
            except Exception as e:  # the session ends on Ring's side by itself eventually
                print(f"(live view close failed: {type(e).__name__}: {e})")

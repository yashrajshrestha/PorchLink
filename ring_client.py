"""Minimal Ring Partner API client (stdlib only), plus a fake client for local testing."""
import json, time, urllib.error, urllib.parse, urllib.request


class TokenExpired(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface 3xx as HTTPError so snapshot() can follow it without the auth header


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


class RingClient:
    def __init__(self, token, base_url="https://api.amazonvision.com"):
        self.token, self.base = token, base_url.rstrip("/")

    def _get(self, path_or_url):
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.api+json, application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise TokenExpired("Ring token expired or invalid") from None
            raise RuntimeError(f"Ring API {e.code} on {url}: {e.read()[:200]!r}") from None

    def devices(self):
        return self._get("/v1/devices").get("data", [])

    def snapshot(self, device_id, timestamp_ms):
        """Return JPEG bytes for the moment nearest timestamp_ms, or None if Ring has no media (416).

        Body format found by probing the real API. A 303 redirect is followed by hand without the
        Bearer header, because pre-signed media URLs reject a second auth method."""
        req = urllib.request.Request(
            f"{self.base}/v1/devices/{device_id}/media/image/download", method="POST",
            data=json.dumps({"type": "at_timestamp", "timestamp": int(timestamp_ms)}).encode(),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with _NO_REDIRECT.open(req, timeout=20) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307) and e.headers.get("Location"):
                url = urllib.parse.urljoin(req.full_url, e.headers["Location"])
                with urllib.request.urlopen(url, timeout=20) as r:
                    return r.read()
            if e.code == 416:
                return None
            if e.code == 401:
                raise TokenExpired("Ring token expired or invalid") from None
            raise RuntimeError(f"Ring snapshot {e.code}: {e.read()[:200]!r}") from None

    def events(self, device_id, max_pages=5):
        # Event History is JSON:API; follow links.next for a few pages at most.
        out, url = [], f"/v1/history/devices/{device_id}/events"
        for _ in range(max_pages):
            body = self._get(url)
            out += body.get("data", [])
            url = (body.get("links") or {}).get("next")
            if not url:
                break
        return out


class FakeRing:
    """Local test mode: adds a fake 20-second live-view event every `every` polls."""

    def __init__(self, every=3):
        self.every, self.polls, self._events = every, 0, []

    def devices(self):
        return [{"id": "fake-front-door", "type": "devices", "attributes": {"name": "Front Door (fake)"}}]

    def snapshot(self, device_id, timestamp_ms):
        return None  # like the Playground: no stored media

    def events(self, device_id):
        self.polls += 1
        if self.polls % self.every == 0:
            now = int(time.time() * 1000)
            self._events.insert(0, {"type": "history-events", "id": f"fake-{self.polls}",
                                    "attributes": {"start": now - 20000, "end": now, "event_type": "on_demand"}})
        return list(self._events)


def parse_event(raw, device_id):
    a = raw.get("attributes") or {}
    return {"id": raw["id"], "device_id": device_id, "start_ms": int(a.get("start") or 0),
            "end_ms": int(a.get("end") or a.get("start") or 0), "event_type": a.get("event_type", "unknown"),
            "sub_type": a.get("sub_type"), "source": "poll"}


def device_name(device):
    name = (device.get("attributes") or {}).get("name")
    return name or f"Ring device ...{device['id'][-6:]}"

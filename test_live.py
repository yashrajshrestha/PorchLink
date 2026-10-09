"""Tests for Watch live (no network, no browser). Run: python3 -m unittest test_live -v"""
import http.client, http.server, io, json, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
from zoneinfo import ZoneInfo

from live import LiveError, LiveViews
import app
import web
from store import Store

CAMERAS = [{"id": "ava1.ring.device.front", "name": "Front Door"}]
FRONT = "ava1.ring.device.front"


class Response(io.BytesIO):
    def __init__(self, status, body=b"", headers=None):
        super().__init__(body)
        self.status, self.headers = status, headers or {}


class Opener:
    """Records every request. POSTs get `reply` (a Response, or an exception to raise; a list is used one per POST,
    the last repeating); DELETEs get 204, or raise `delete_error` when set."""

    def __init__(self, reply, delete_error=None):
        self.reply, self.delete_error, self.requests = reply, delete_error, []
        self.posts = 0

    def __call__(self, req, timeout):
        self.requests.append((req.get_method(), req.full_url, dict(req.header_items()), req.data))
        if req.get_method() == "DELETE":
            if self.delete_error:
                raise self.delete_error
            return Response(204)
        reply = self.reply[min(self.posts, len(self.reply) - 1)] if isinstance(self.reply, list) else self.reply
        self.posts += 1
        if isinstance(reply, Exception):
            raise reply
        return Response(reply.status, reply.getvalue(), reply.headers)  # fresh: the caller closes it


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
        kw.setdefault("spawn", lambda fn: fn())  # evictions close inline so tests stay deterministic
        return LiveViews("secret-token", kw.pop("base", "https://api.example.com/"), lambda: CAMERAS, self.opener, self.timer, **kw)

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
        for secret in ("secret-token", "/v1/whep/sessions/abc"):
            self.assertNotIn(secret, repr(out))

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
        live = self.views([self.ok("/s/a"), self.ok("/s/b"), self.ok("/s/c")], max_sessions=2)
        a, b, c = (live.start(FRONT, "v=0 offer")["session"] for _ in range(3))
        self.assertEqual(self.deletes(), [("https://api.example.com/s/a", "Bearer secret-token")])
        self.assertEqual([t.cancelled for t in self.timers], [True, False, False])
        self.assertFalse(live.stop(a))
        self.assertTrue(live.stop(b))
        self.assertTrue(live.stop(c))

    def test_failed_start_at_the_limit_does_not_evict(self):
        error = urllib.error.HTTPError("https://x", 500, "no", {}, io.BytesIO(b"oops"))
        live = self.views([self.ok(), error], max_sessions=1)
        sid = live.start(FRONT, "v=0 offer")["session"]
        with self.assertRaises(LiveError):
            live.start(FRONT, "v=0 offer")
        self.assertEqual(self.deletes(), [])
        self.assertFalse(self.timers[0].cancelled)
        self.assertTrue(live.stop(sid))

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

    def test_location_on_another_host_is_ignored(self):
        live = self.views(self.ok("https://evil.example/x"))
        sid = live.start(FRONT, "v=0 offer")["session"]
        self.assertTrue(live.stop(sid))
        self.assertEqual([r[0] for r in self.opener.requests], ["POST"])  # the token never leaves Ring's host

    def test_location_on_rings_own_domain_is_kept(self):
        live = self.views(self.ok("https://media.example.com/x"))
        sid = live.start(FRONT, "v=0 offer")["session"]
        self.assertTrue(live.stop(sid))
        self.assertEqual(self.deletes(), [("https://media.example.com/x", "Bearer secret-token")])

    def test_location_on_another_domain_or_plain_http_is_still_dropped(self):
        for location in ("https://evil.example/x", "https://media.example.com.evil.example/x", "https://notexample.com/x",
                         "http://media.example.com/x"):
            live = self.views(self.ok(location), base="https://api.example.com")
            live.stop(live.start(FRONT, "v=0 offer")["session"])
            self.assertEqual(self.deletes(), [], location)

    def test_sibling_hosts_need_a_dns_name_and_https(self):
        for base, location in (("https://127.0.0.1:9100", "https://0.0.0.1/x"), ("http://api.example.com", "http://media.example.com/x")):
            live = self.views(self.ok(location), base=base)
            live.stop(live.start(FRONT, "v=0 offer")["session"])
            self.assertEqual(self.deletes(), [], (base, location))

    def test_session_host_is_printed_once(self):
        live = self.views([self.ok("https://media.example.com/a"), self.ok("/b")])
        live.start(FRONT, "v=0 offer")
        live.start(FRONT, "v=0 offer")
        lines = [c.args[0] for c in print.call_args_list if str(c.args[0]).startswith("(live view session host")]
        self.assertEqual(lines, ["(live view session host: media.example.com)"])

    def test_evicted_sessions_close_off_the_request_path(self):
        pending = []
        live = self.views([self.ok("/s/a"), self.ok("/s/b")], max_sessions=1, spawn=pending.append)
        live.start(FRONT, "v=0 offer")
        live.start(FRONT, "v=0 offer")  # returns without having sent the DELETE
        self.assertEqual(self.deletes(), [])
        self.assertEqual(len(pending), 1)
        pending[0]()
        self.assertEqual(self.deletes(), [("https://api.example.com/s/a", "Bearer secret-token")])

    def test_default_spawn_closes_on_a_daemon_thread(self):
        deleted = threading.Event()

        class Signalling(Opener):
            def __call__(self, req, timeout):
                if req.get_method() == "DELETE":
                    deleted.set()
                return super().__call__(req, timeout)

        self.opener = Signalling(self.ok("/s/a"))
        live = LiveViews("t", "https://api.example.com", lambda: CAMERAS, self.opener, self.timer, max_sessions=1)
        live.start(FRONT, "v=0 offer")
        live.start(FRONT, "v=0 offer")
        self.assertTrue(deleted.wait(5))

    def test_unencodable_offers_are_bad_requests(self):
        live = self.views(self.ok())
        for offer in ("\ud800", None, b"v=0 offer", 5):
            with self.assertRaises(LiveError) as cm:
                live.start(FRONT, offer)
            self.assertEqual((cm.exception.status, cm.exception.message), (400, "Bad video offer"))
        self.assertEqual(self.opener.requests, [])

    def test_unreadable_error_body_still_maps_by_status(self):
        class Broken(io.BytesIO):
            def read(self, *args):
                raise OSError("reset")

        error = urllib.error.HTTPError("https://x", 401, "no", {}, Broken())
        with self.assertRaises(LiveError) as cm:
            self.views(error).start(FRONT, "v=0 offer")
        self.assertEqual(cm.exception.message, "Ring token expired: get a new Playground token and restart PorchLink")

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


class RedirectTest(unittest.TestCase):
    """The real default opener: Ring's answer may be a redirect, and the token must not follow it."""

    def serve(self, handler):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_a_redirect_is_a_failure_and_the_token_is_never_sent_on(self):
        stolen = []

        class Thief(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                stolen.append(self.headers.get("Authorization"))
                self.send_response(201)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_DELETE = do_POST

            def log_message(self, *args):
                pass

        thief = self.serve(Thief)

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{thief}/steal")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        ring = self.serve(Redirector)
        live = LiveViews("secret-token", f"http://127.0.0.1:{ring}", lambda: CAMERAS)
        with mock.patch("builtins.print"), self.assertRaises(LiveError) as cm:
            live.start(FRONT, "v=0 offer")
        self.assertEqual((cm.exception.status, cm.exception.message), (502, "Ring live view failed (302)"))
        time.sleep(0.2)
        self.assertEqual(stolen, [])


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
        self.assertEqual(self.call("POST", "/api/live/" + FRONT, b"", length=65_537), (413, {"error": "video offer too large"}))
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
        self.assertEqual(self.call("POST", "/webhook", b"", "application/json")[0], 404)  # webhooks still off without enqueue; empty body, as that path does not drain


class HostCheckTest(unittest.TestCase):
    """DNS rebinding: a page on another domain that resolves to 127.0.0.1 must not reach the API."""

    def start(self, live=None, **kw):
        self.queued = []
        server = web.make_server(Store(":memory:"), ZoneInfo("UTC"), 0, live=live, **kw)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def call(self, method, path, host, body=None, ctype="application/sdp"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host}
        if body is not None:
            headers["Content-Type"] = ctype
        conn.request(method, path, body, headers)
        r = conn.getresponse()
        out = r.status, r.read().decode()
        conn.close()
        return out

    def test_foreign_hosts_are_refused_on_every_method(self):
        live = StubLive()
        self.start(live)
        self.assertEqual(self.call("GET", "/api/live", "evil.example"), (403, "bad host"))
        self.assertEqual(self.call("GET", "/", "evil.example:8080"), (403, "bad host"))
        self.assertEqual(self.call("POST", "/api/live/" + FRONT, "evil.example:8080", b"v=0 offer"), (403, "bad host"))
        self.assertEqual(self.call("DELETE", "/api/live/session/s1", "evil.example"), (403, "bad host"))
        self.assertEqual(self.call("GET", "/api/live", f"127.0.0.1.evil.example:{self.port}")[0], 403)
        self.assertEqual(self.call("GET", "/api/live", "127.0.0.1:1")[0], 403)  # not this server's port
        self.assertEqual((live.started, live.stopped), ([], []))

    def test_local_names_are_accepted_in_any_case(self):
        self.start(StubLive())
        for host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}", f"[::1]:{self.port}", f"LocalHost:{self.port}"):
            self.assertEqual(self.call("GET", "/", host)[0], 200, host)

    def test_webhooks_keep_working_through_a_public_tunnel(self):
        self.start(enqueue=lambda ev, name: self.queued.append(ev), webhook_secret="k")
        with mock.patch("builtins.print"):
            status, body = self.call("POST", "/webhook", "abc123.tunnel.example", b"{}", "application/json")
        self.assertEqual((status, body), (401, "bad signature"))  # reached the signature check, not the host check


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

    def test_a_failing_ring_is_asked_at_most_every_30_seconds(self):
        class Down:
            calls = 0

            def devices(self):
                self.calls += 1
                raise ConnectionError("down")

        now = [100.0]
        ring = Down()
        names = app.DeviceNames(ring, clock=lambda: now[0])
        with mock.patch("builtins.print"):
            names.all()
            now[0] += 29
            names.all()
            self.assertEqual(ring.calls, 1)
            now[0] += 2
            names.all()
            self.assertEqual(ring.calls, 2)
            names({"device_id": "ava1.ring.device.X"})  # the worker's lookup still asks on a miss
            self.assertEqual(ring.calls, 3)

    def test_page_is_still_served_from_the_file(self):
        with open(web.os.path.join(web.os.path.dirname(web.os.path.abspath(web.__file__)), "dashboard.html"), encoding="utf-8") as f:
            self.assertEqual(web.PAGE, f.read())


if __name__ == "__main__":
    unittest.main()

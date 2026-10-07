"""Tests for the /webhook endpoint: every rejection, and that only a valid signed door event is queued.
Run: python3 -m unittest test_web -v"""
import http.client, json, socket, threading, unittest
from unittest import mock
from zoneinfo import ZoneInfo

import web
from store import Store
from webhooks import sign

SECRET = "test-signing-key"
TZ = ZoneInfo("Asia/Kathmandu")


def motion(request_id="req-1", kind="motion_detected"):
    return {"data": {"type": kind, "attributes": {"source": "ava1.ring.device.ABC123", "source_type": "devices",
                                                  "timestamp": 1786715596787, "sub_type": "human"}},
            "meta": {"version": "1.0", "time": "2026-10-07T10:00:00Z", "request_id": request_id, "account_id": "a"}}


class WebhookEndpointTest(unittest.TestCase):
    def start(self, secret=SECRET, enqueue=True):
        self.queued = []
        server = web.make_server(Store(":memory:"), TZ, 0,
                                 (lambda ev, name: self.queued.append((ev, name))) if enqueue else None, None, secret)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def post(self, body, headers=None, path="/webhook", sign_with=SECRET):
        hdrs = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if sign_with is not None:
            hdrs["X-Signature"] = sign(sign_with, body)
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", path)
        for k, v in hdrs.items():
            conn.putheader(k, v)
        conn.endheaders()
        if hdrs.get("Content-Length", "").isdigit() and int(hdrs["Content-Length"]) <= web.MAX_WEBHOOK_BYTES:
            conn.send(body)
        status = conn.getresponse().status
        conn.close()
        return status

    def test_valid_signed_motion_is_queued_and_acked(self):
        self.start()
        self.assertEqual(self.post(json.dumps(motion()).encode()), 200)
        self.assertEqual(len(self.queued), 1)
        ev, name = self.queued[0]
        self.assertEqual((ev["id"], ev["source"], name), ("webhook:req-1", "webhook", None))

    def test_raw_body_is_what_gets_verified(self):
        """Signed exactly as sent: non-canonical JSON (spaces, key order) must still verify against the raw bytes."""
        self.start()
        p = motion()
        body = ('{ "meta" : ' + json.dumps(p["meta"]) + ' ,\n  "data":  ' + json.dumps(p["data"], indent=3) + '  }').encode()
        self.assertNotEqual(body, json.dumps(json.loads(body)).encode())
        self.assertEqual(self.post(body), 200)
        self.assertEqual(len(self.queued), 1)

    def test_stalled_client_is_dropped_and_server_keeps_serving(self):
        with mock.patch.object(web, "REQUEST_TIMEOUT", 0.5):
            self.start()
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.addCleanup(sock.close)
        sock.sendall(b"POST /webhook HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                     b"Content-Length: 100\r\n\r\n")  # promises a body, never sends it
        try:
            dropped = sock.recv(1024) == b""
        except ConnectionResetError:
            dropped = True
        self.assertTrue(dropped)
        self.assertEqual(self.post(json.dumps(motion()).encode()), 200)

    def test_refusals_are_logged_without_the_body(self):
        self.start()
        with mock.patch("builtins.print") as out:
            self.assertEqual(self.post(b'{"secret": "payload"}', sign_with="wrong-key"), 401)
        lines = [str(c.args[0]) for c in out.call_args_list]
        self.assertEqual(lines, ["webhook refused 401: bad signature"])

    def test_verified_body_is_logged_only_when_asked(self):
        body = json.dumps(motion()).encode()
        for log_bodies, expected in ((False, 0), (True, 1)):
            self.queued = []
            server = web.make_server(Store(":memory:"), TZ, 0, lambda ev, name: None, None, SECRET, log_bodies)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.port = server.server_address[1]
            with mock.patch("builtins.print") as out:
                self.assertEqual(self.post(body), 200)
            self.assertEqual(len([c for c in out.call_args_list if str(c.args[0]).startswith("webhook body: ")]), expected)

    def test_charset_parameter_is_fine(self):
        self.start()
        self.assertEqual(self.post(json.dumps(motion()).encode(), {"Content-Type": "application/json; charset=utf-8"}), 200)

    def test_non_door_event_is_acked_but_not_queued(self):
        self.start()
        self.assertEqual(self.post(json.dumps(motion(kind="device_online")).encode()), 200)
        self.assertEqual(self.queued, [])

    def test_rejections(self):
        self.start()
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        good = json.dumps(motion()).encode()
        self.assertEqual(self.post(good, path="/other"), 404)
        self.assertEqual(self.post(good, {"Content-Type": "text/plain"}), 415)
        self.assertEqual(self.post(good, {"Content-Length": "abc"}), 400)
        self.assertEqual(self.post(good, {"Content-Length": "-1"}), 400)
        self.assertEqual(self.post(b"x", {"Content-Length": str(web.MAX_WEBHOOK_BYTES + 1)}), 413)
        self.assertEqual(self.post(good, sign_with="wrong-key"), 401)
        self.assertEqual(self.post(good, sign_with=None), 401)
        self.assertEqual(self.post(b"not json"), 400)
        self.assertEqual(self.post(b"[1, 2]"), 400)
        self.assertEqual(self.post(json.dumps({"data": {"type": "motion_detected"}}).encode()), 400)
        self.assertEqual(self.queued, [])

    def test_no_secret_refuses_every_webhook(self):
        self.start(secret="")
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.assertEqual(self.post(json.dumps(motion()).encode(), sign_with=""), 503)
        self.assertEqual(self.queued, [])

    def test_no_handler_is_404(self):
        self.start(enqueue=False)
        self.assertEqual(self.post(json.dumps(motion()).encode()), 404)


if __name__ == "__main__":
    unittest.main()

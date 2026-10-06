"""Tests for rules and the poll -> store -> alert flow. Run: python3 -m unittest -v"""
import ssl, unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import app
from alerts import EmailAlerter
from ring_client import FakeRing, TokenExpired, parse_event
from rules import Settings, evaluate, format_alert, is_night
from store import Store
from worker import EventWorker

TZ = ZoneInfo("Asia/Kathmandu")


def ms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp() * 1000)


def event(eid, start, length_s=20, device="dev1"):
    return {"id": eid, "device_id": device, "start_ms": start, "end_ms": start + length_s * 1000, "event_type": "on_demand"}


class CaptureAlerter:
    def __init__(self):
        self.sent = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return True


class RulesTest(unittest.TestCase):
    def test_night_window_wraps_midnight(self):
        s = Settings(night_start=23, night_end=6)
        self.assertTrue(is_night(23, s) and is_night(2, s))
        self.assertFalse(is_night(6, s) or is_night(14, s))

    def test_night_window_same_day(self):
        s = Settings(night_start=1, night_end=5)
        self.assertTrue(is_night(3, s))
        self.assertFalse(is_night(0, s) or is_night(5, s))

    def test_daytime_single_event_is_plain_activity(self):
        ev = event("a", ms(2026, 10, 5, 14, 0))
        self.assertEqual([k for k, _ in evaluate(ev, [ev["start_ms"]], None, Settings(), TZ)], ["activity"])

    def test_no_alert_when_every_event_off_and_nothing_unusual(self):
        ev = event("a", ms(2026, 10, 5, 14, 0))
        self.assertEqual(evaluate(ev, [ev["start_ms"]], None, Settings(alert_every_event=False), TZ), [])

    def test_night_event(self):
        ev = event("a", ms(2026, 10, 5, 2, 14))
        self.assertIn("night", [k for k, _ in evaluate(ev, [ev["start_ms"]], None, Settings(), TZ)])

    def test_burst_fires_once_per_window(self):
        s = Settings(burst_count=3, burst_minutes=30)
        starts = [ms(2026, 10, 5, 14, m) for m in (0, 10, 20)]
        third = event("c", starts[2])
        self.assertIn("burst", [k for k, _ in evaluate(third, starts, None, s, TZ)])
        fourth = event("d", ms(2026, 10, 5, 14, 25))
        kinds = [k for k, _ in evaluate(fourth, starts + [fourth["start_ms"]], starts[2], s, TZ)]
        self.assertNotIn("burst", kinds)  # already alerted inside this window

    def test_events_outside_window_do_not_count(self):
        s = Settings(burst_count=3, burst_minutes=30)
        starts = [ms(2026, 10, 5, 13, 0), ms(2026, 10, 5, 14, 0), ms(2026, 10, 5, 14, 10)]
        ev = event("c", starts[2])
        self.assertNotIn("burst", [k for k, _ in evaluate(ev, starts, None, s, TZ)])

    def test_format_alert(self):
        ev = event("a", ms(2026, 10, 5, 2, 14), length_s=20)
        subject, body = format_alert(ev, "Front Door", [("night", "Unusual night-time activity at 02:14.")], TZ)
        self.assertEqual(subject, "PorchLink: Unusual activity at Front Door, 02:14 AM")
        self.assertIn("How long: 20 seconds", body)


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.store, self.alerter = Store(":memory:"), CaptureAlerter()
        self.worker = EventWorker(self.store, app.make_process(self.store, self.alerter, Settings(), TZ))

    def poll(self, ring, **kw):
        return app.poll_once(ring, self.store, self.worker.handle, **kw)  # handle = synchronous submit

    def test_startup_poll_records_without_alerting_then_new_event_alerts(self):
        ring = FakeRing(every=1)
        self.assertEqual(self.poll(ring, alert_new=False), 1)
        self.assertEqual(self.alerter.sent, [])
        self.assertEqual(self.poll(ring), 1)
        self.assertEqual(len(self.alerter.sent), 1)

    def test_same_event_is_not_alerted_twice(self):
        ring = FakeRing(every=2)  # second poll adds one event, third poll returns it again
        for _ in range(3):
            self.poll(ring)
        self.assertEqual(len(self.alerter.sent), 1)

    def test_burst_through_store(self):
        s = Settings(burst_count=3, burst_minutes=30)
        worker = EventWorker(self.store, app.make_process(self.store, self.alerter, s, TZ))
        kinds = []
        for i, m in enumerate((0, 5, 10, 15)):
            worker.handle({**event(f"e{i}", ms(2026, 10, 5, 14, m)), "source": "poll"}, "Front Door")
            kinds.append(self.alerter.sent[-1][0])
        self.assertTrue(kinds[2].startswith("PorchLink: Unusual activity"))   # 3rd event: burst
        self.assertTrue(kinds[3].startswith("PorchLink: Activity"))           # 4th: no repeat burst

    def test_parse_real_event_shape(self):
        raw = {"type": "history-events", "id": "ava1.x", "attributes": {
            "start": 1791193259621, "end": 1791193279829, "event_type": "on_demand"}}
        self.assertEqual(parse_event(raw, "dev1")["end_ms"] - parse_event(raw, "dev1")["start_ms"], 20208)


class PollIsolationTest(unittest.TestCase):
    class TwoDoors:
        def __init__(self, failure):
            self.failure = failure

        def devices(self):
            return [{"id": "a", "attributes": {"name": "A"}}, {"id": "b", "attributes": {"name": "B"}}]

        def events(self, device_id):
            if device_id == "a":
                raise self.failure
            return [{"type": "history-events", "id": "b-1",
                     "attributes": {"start": 1000, "end": 2000, "event_type": "motion_detected"}}]

    def test_one_failing_device_does_not_stop_the_others(self):
        submitted = []
        with mock.patch("builtins.print"):
            n = app.poll_once(self.TwoDoors(RuntimeError("Ring API 500")), Store(":memory:"),
                              lambda ev, name: submitted.append((ev["id"], name)))
        self.assertEqual((n, submitted), (1, [("b-1", "B")]))

    def test_expired_token_still_stops_polling(self):
        with self.assertRaises(TokenExpired):
            app.poll_once(self.TwoDoors(TokenExpired("expired")), Store(":memory:"), lambda ev, name: None)

    def test_device_names_cache_and_fallback(self):
        ring = self.TwoDoors(None)
        names = app.DeviceNames(ring)
        self.assertEqual(names({"device_id": "b"}), "B")

        class Down:
            def devices(self):
                raise RuntimeError("Ring API 500")

        with mock.patch("builtins.print"):
            self.assertEqual(app.DeviceNames(Down())({"device_id": "ava1.ring.device.ABC123"}),
                             "Ring device ...ABC123")


class EmailTest(unittest.TestCase):
    ENV = {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "me@example.com", "SMTP_PASSWORD": "x", "ALERT_TO": "care@example.com"}

    def test_sends_over_starttls(self):
        with mock.patch("alerts.smtplib.SMTP") as smtp, mock.patch("builtins.print"):
            self.assertTrue(EmailAlerter(self.ENV).send("subj", "body"))
        server = smtp.return_value.__enter__.return_value
        server.starttls.assert_called_once()
        context = server.starttls.call_args.kwargs["context"]  # certificate checked: no MITM on the SMTP password
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        server.login.assert_called_once_with("me@example.com", "x")
        msg = server.send_message.call_args[0][0]
        self.assertEqual((msg["To"], msg["Subject"]), ("care@example.com", "subj"))

    def test_smtp_failure_returns_false(self):
        with mock.patch("alerts.smtplib.SMTP", side_effect=OSError("down")), mock.patch("builtins.print"):
            self.assertFalse(EmailAlerter(self.ENV).send("subj", "body"))

    def test_not_configured_prints_only(self):
        with mock.patch("alerts.smtplib.SMTP") as smtp, mock.patch("builtins.print"):
            self.assertFalse(EmailAlerter({}).send("subj", "body"))
        smtp.assert_not_called()


if __name__ == "__main__":
    unittest.main()

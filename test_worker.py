"""Tests for the event worker and per-event status in the store. Run: python3 -m unittest test_worker -v"""
import os, sqlite3, tempfile, threading, unittest
from unittest import mock

from store import Store
from worker import DUPLICATE_WINDOW_MS, EventWorker


def ev(eid, start=1_000_000, source="poll", device="dev1", kind="motion_detected"):
    return {"id": eid, "device_id": device, "start_ms": start, "end_ms": start, "event_type": kind,
            "sub_type": None, "source": source}


class Recorder:
    """A process() stand-in that records calls and fails the first `fail_times` of them."""

    def __init__(self, fail_times=0):
        self.calls, self.fail_times = [], fail_times

    def __call__(self, e, name):
        self.calls.append((e["id"], name))
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("boom")


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def duplicate_of(self, event_id):
        return self.store.db.execute("SELECT duplicate_of FROM events WHERE id=?", (event_id,)).fetchone()[0]

    def test_doorbell_lane_does_not_wait_behind_slow_motion(self):
        gate, seen = threading.Event(), []

        def process(e, name):
            if e["event_type"] == "motion_detected":
                gate.wait(5)  # the AI is still describing this motion event
            seen.append(e["id"])

        w = EventWorker(self.store, process, lanes=("doorbell", "main"),
                        lane_for=lambda e: "doorbell" if e["event_type"] == "button_press" else "main").start()
        w.submit(ev("m1"), "Front")
        w.submit(ev("d1", start=1_005_000, kind="button_press"), "Front")
        for _ in range(200):
            if seen:
                break
            threading.Event().wait(0.01)
        self.assertEqual(seen, ["d1"])  # handled while the motion event was still blocked
        gate.set()
        self.assertTrue(w.drain(5))
        self.assertEqual(seen, ["d1", "m1"])
        self.assertEqual({self.store.status("m1"), self.store.status("d1")}, {"done"})

    def test_one_lane_by_default(self):
        order = []
        w = EventWorker(self.store, lambda e, name: order.append(e["id"])).start()
        for i in range(5):
            w.submit(ev(f"e{i}", start=1_000_000 + i * 60_000, kind="button_press" if i % 2 else "motion_detected"), "Front")
        self.assertTrue(w.drain(5))
        self.assertEqual(order, ["e0", "e1", "e2", "e3", "e4"])  # strictly in order, one thread

    def test_window_constant(self):
        self.assertEqual(DUPLICATE_WINDOW_MS, 15_000)

    def test_new_event_is_processed_once(self):
        rec = Recorder()
        w = EventWorker(self.store, rec)
        self.assertEqual(w.handle(ev("e1"), "Front"), "done")
        self.assertIsNone(w.handle(ev("e1"), "Front"))
        self.assertEqual(rec.calls, [("e1", "Front")])
        self.assertEqual(self.store.status("e1"), "done")

    def test_failure_is_retried_then_succeeds(self):
        rec = Recorder(fail_times=1)
        w = EventWorker(self.store, rec)
        self.assertEqual(w.handle(ev("e1"), "Front"), "failed")
        self.assertEqual(self.store.status("e1"), "failed")
        self.assertEqual(w.handle(ev("e1"), "Front"), "done")
        self.assertEqual(len(rec.calls), 2)

    def test_gives_up_after_max_attempts(self):
        rec = Recorder(fail_times=99)
        w = EventWorker(self.store, rec, max_attempts=3)
        self.assertEqual([w.handle(ev("e1"), "Front") for _ in range(5)], ["failed", "failed", "failed", None, None])
        self.assertEqual(len(rec.calls), 3)

    def test_crash_leftover_pending_is_retried(self):
        self.store.begin(ev("e1"), "Front", 3)  # the process died mid-event: the row is stuck in pending
        rec = Recorder()
        self.assertEqual(EventWorker(self.store, rec).handle(ev("e1"), "Front"), "done")

    def test_history_rows_never_alert_later(self):
        self.assertTrue(self.store.record_history(ev("e1"), "Front"))
        self.assertFalse(self.store.record_history(ev("e1"), "Front"))
        rec = Recorder()
        self.assertIsNone(EventWorker(self.store, rec).handle(ev("e1"), "Front"))
        self.assertEqual((rec.calls, self.store.status("e1")), ([], "history"))

    def test_same_press_webhook_then_poll_is_handled_once(self):
        rec = Recorder()
        w = EventWorker(self.store, rec)
        self.assertEqual(w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front"), "done")
        self.assertEqual(w.handle(ev("hist-1", 1_005_000, "poll"), "Front"), "duplicate")
        self.assertEqual(rec.calls, [("webhook:req-1", "Front")])
        self.assertEqual(self.store.status("hist-1"), "duplicate")
        self.assertEqual(self.duplicate_of("hist-1"), "webhook:req-1")

    def test_same_press_poll_then_webhook_is_handled_once(self):
        rec = Recorder()
        w = EventWorker(self.store, rec)
        self.assertEqual(w.handle(ev("hist-1", 1_005_000, "poll"), "Front"), "done")
        self.assertEqual(w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front"), "duplicate")

    def test_three_events_through_both_routes_are_handled_once_each(self):
        """p = poll copy, w = webhook copy of motion events at 0, 10 and 20 s: no copy may absorb two others."""
        rec = Recorder()
        w = EventWorker(self.store, rec)
        for e in (ev("p0", 0, "poll"), ev("w0", 0, "webhook"), ev("w1", 10_000, "webhook"),
                  ev("w2", 20_000, "webhook"), ev("p1", 10_000, "poll"), ev("p2", 20_000, "poll")):
            w.handle(e, "Front")
        rows = self.store.db.execute("SELECT id, status, duplicate_of FROM events").fetchall()
        done = [r[0] for r in rows if r[1] == "done"]
        dups = [r for r in rows if r[1] == "duplicate"]
        self.assertEqual(len(done), 3)
        self.assertEqual(len(dups), 3)
        self.assertEqual(sorted(c[0] for c in rec.calls), sorted(done))  # each physical event handled once
        self.assertEqual(len({r[2] for r in dups}), 3)  # each duplicate points at a distinct handled event
        self.assertTrue(all(r[2] in done for r in dups))

    def test_nearest_candidate_is_chosen(self):
        w = EventWorker(self.store, Recorder())
        self.assertEqual(w.handle(ev("hist-1", 1_000_000, "poll"), "Front"), "done")
        self.assertEqual(w.handle(ev("hist-2", 1_006_000, "poll"), "Front"), "done")
        self.assertEqual(w.handle(ev("webhook:req-1", 1_005_000, "webhook"), "Front"), "duplicate")
        self.assertEqual(self.duplicate_of("webhook:req-1"), "hist-2")

    def test_window_boundary_is_inclusive(self):
        w = EventWorker(self.store, Recorder())
        w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front")
        self.assertEqual(w.handle(ev("hist-1", 1_000_000 + DUPLICATE_WINDOW_MS, "poll"), "Front"), "duplicate")
        w.handle(ev("webhook:req-2", 2_000_000, "webhook"), "Front")
        self.assertEqual(w.handle(ev("hist-2", 2_000_000 + DUPLICATE_WINDOW_MS + 1, "poll"), "Front"), "done")

    def test_failed_copy_does_not_hide_the_other_copy(self):
        rec = Recorder(fail_times=1)
        w = EventWorker(self.store, rec)
        self.assertEqual(w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front"), "failed")
        self.assertEqual(w.handle(ev("hist-1", 1_000_000, "poll"), "Front"), "done")  # only a done copy dedupes

    def test_events_20_seconds_apart_are_both_handled(self):
        w = EventWorker(self.store, Recorder())
        self.assertEqual(w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front"), "done")
        self.assertEqual(w.handle(ev("hist-1", 1_020_000, "poll"), "Front"), "done")

    def test_same_source_never_dedupes(self):
        w = EventWorker(self.store, Recorder())
        self.assertEqual(w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front"), "done")
        self.assertEqual(w.handle(ev("webhook:req-2", 1_003_000, "webhook"), "Front"), "done")

    def test_other_type_or_device_is_not_a_duplicate(self):
        w = EventWorker(self.store, Recorder())
        w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front")
        self.assertEqual(w.handle(ev("hist-1", 1_000_000, "poll", kind="button_press"), "Front"), "done")
        self.assertEqual(w.handle(ev("hist-2", 1_000_000, "poll", device="dev2"), "Back"), "done")

    def test_duplicates_do_not_count_toward_bursts_or_the_timeline(self):
        w = EventWorker(self.store, Recorder())
        w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front")
        w.handle(ev("hist-1", 1_000_000, "poll"), "Front")
        self.assertEqual(self.store.starts_since("dev1", 0), [1_000_000])
        self.assertEqual([e["id"] for e in self.store.timeline(0)], ["webhook:req-1"])

    def test_name_for_fills_missing_names(self):
        rec = Recorder()
        w = EventWorker(self.store, rec, name_for=lambda e: "Front Door")
        w.handle(ev("e1"), None)
        self.assertEqual(rec.calls, [("e1", "Front Door")])
        row = self.store.db.execute("SELECT device_name FROM events WHERE id='e1'").fetchone()
        self.assertEqual(row[0], "Front Door")

    def test_background_thread_drains_the_queue(self):
        rec = Recorder()
        w = EventWorker(self.store, rec).start()
        for i in range(5):
            w.submit(ev(f"e{i}", 1_000_000 + i * 60_000))
        self.assertTrue(w.drain(5))
        self.assertEqual(len(rec.calls), 5)

    def test_thread_survives_a_failing_event(self):
        w = EventWorker(self.store, Recorder(fail_times=1)).start()
        w.submit(ev("e1"))
        w.submit(ev("e2", 2_000_000))
        self.assertTrue(w.drain(5))
        self.assertEqual((self.store.status("e1"), self.store.status("e2")), ("failed", "done"))

    def test_unknown_event_has_no_status(self):
        self.assertIsNone(self.store.status("nope"))

    def test_finish_done_raises_returns_failed(self):
        """If store.finish("done") raises, handle returns "failed" without re-raising."""
        class FailingStore(Store):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.finish_done_count = 0
            def finish(self, event_id, status):
                if status == "done":
                    self.finish_done_count += 1
                    if self.finish_done_count == 1:
                        raise sqlite3.OperationalError("database is locked")
                return super().finish(event_id, status)

        store = FailingStore(":memory:")
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

        rec = Recorder()
        w = EventWorker(store, rec)
        result = w.handle(ev("e1"), "Front")
        self.assertEqual(result, "failed")
        self.assertEqual(rec.calls, [("e1", "Front")])
        self.assertEqual(store.status("e1"), "pending")  # stays pending, will retry

    def test_begin_error_never_overwrites_a_done_event(self):
        class BeginRaises(Store):
            def begin(self, *args, **kwargs):
                raise sqlite3.OperationalError("database is locked")

        store = BeginRaises(":memory:")
        store.db.execute("INSERT INTO events (id, device_id, start_ms, end_ms, event_type, status)"
                         " VALUES ('e1', 'dev1', 1, 1, 'motion_detected', 'done')")
        rec = Recorder()
        self.assertEqual(EventWorker(store, rec).handle(ev("e1"), "Front"), "failed")
        self.assertEqual((store.status("e1"), rec.calls), ("done", []))

    def test_thread_survives_failing_finish(self):
        """Worker thread survives if store.finish fails and processes later events."""
        class FailOnFirstStore(Store):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.finish_count = 0
            def finish(self, event_id, status):
                self.finish_count += 1
                if self.finish_count == 1:
                    raise sqlite3.OperationalError("database is locked")
                return super().finish(event_id, status)

        store = FailOnFirstStore(":memory:")
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

        rec = Recorder()
        w = EventWorker(store, rec).start()
        w.submit(ev("e1", 1_000_000))
        w.submit(ev("e2", 2_000_000))
        self.assertTrue(w.drain(5))
        # First event failed to finish, second event should have been processed
        self.assertEqual(len(rec.calls), 2)
        self.assertEqual([call[0] for call in rec.calls], ["e1", "e2"])


class SubmitDedupeTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        quiet = mock.patch("builtins.print")
        quiet.start()
        self.addCleanup(quiet.stop)

    def blocker(self, calls, fail_ids=()):
        """A process() that waits on self.gate for the event named "block", and fails for fail_ids."""
        self.gate, self.entered = threading.Event(), threading.Event()

        def process(e, name):
            calls.append(e["id"])
            if e["id"] == "block":
                self.entered.set()
                self.assertTrue(self.gate.wait(5))
            if e["id"] in fail_ids:
                raise RuntimeError("boom")
        return process

    def test_resubmitting_an_inflight_event_is_ignored(self):
        calls = []
        w = EventWorker(self.store, self.blocker(calls)).start()
        results = [w.submit(ev("block"))]
        self.assertTrue(self.entered.wait(5))
        results += [w.submit(ev("block")), w.submit(ev("block"))]  # in flight: the worker is still inside process
        self.gate.set()
        self.assertTrue(w.drain(5))
        self.assertEqual((results, calls), ([True, False, False], ["block"]))
        self.assertTrue(w.submit(ev("block")))  # handled and released: submit works again (store says done)
        self.assertTrue(w.drain(5))

    def test_queued_copies_do_not_burn_all_retries_at_once(self):
        calls = []
        w = EventWorker(self.store, self.blocker(calls, fail_ids={"e1"})).start()
        w.submit(ev("block"))
        self.assertTrue(self.entered.wait(5))
        self.assertEqual([w.submit(ev("e1")) for _ in range(3)], [True, False, False])
        self.gate.set()
        self.assertTrue(w.drain(5))
        attempts = self.store.db.execute("SELECT attempts FROM events WHERE id='e1'").fetchone()[0]
        self.assertEqual((calls.count("e1"), attempts, self.store.status("e1")), (1, 1, "failed"))
        self.assertTrue(w.submit(ev("e1")))  # the next poll's resubmit is accepted: attempt 2
        self.assertTrue(w.drain(5))
        attempts = self.store.db.execute("SELECT attempts FROM events WHERE id='e1'").fetchone()[0]
        self.assertEqual((calls.count("e1"), attempts), (2, 2))


class MigrationTest(unittest.TestCase):
    def test_rows_from_an_old_database_count_as_done(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        db = sqlite3.connect(path)
        db.executescript("CREATE TABLE events (id TEXT PRIMARY KEY, device_id TEXT, device_name TEXT,"
                         " start_ms INTEGER, end_ms INTEGER, event_type TEXT);"
                         "INSERT INTO events VALUES ('old-1', 'dev1', 'Front', 1, 2, 'on_demand');")
        db.commit()
        db.close()
        store = Store(path)
        self.assertEqual(store.status("old-1"), "done")
        self.assertFalse(store.begin(ev("old-1"), "Front", 3))


if __name__ == "__main__":
    unittest.main()

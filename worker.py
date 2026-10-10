"""Worker threads handle every Ring event, from the poller and from webhooks, one at a time per lane.

Within a lane, events are handled in order. An event is marked done only after it was actually handled: a failure
is retried when the poll sees the event again, up to max_attempts. Both copies of one press (poll and webhook) go
to the same lane, so dedupe stays serial.
By default there is one lane. The app gives doorbell presses their own lane, so a press never waits behind
a motion event whose picture the AI is still describing. Work shared across lanes is locked: burst alerts are
claimed under a lock in app.handle_event, and Bridge.plan holds a lock for cooldowns.
"""
import queue, threading, time

DUPLICATE_WINDOW_MS = 15_000  # same press seen by webhook and by history: start times this close


class EventWorker:
    def __init__(self, store, process, name_for=None, max_attempts=3, lanes=("main",), lane_for=None):
        """lane_for(ev) picks one of `lanes`; each lane has its own queue and thread."""
        self.store, self.process, self.name_for, self.max_attempts = store, process, name_for, max_attempts
        self.queues = {lane: queue.Queue() for lane in lanes}
        self.lane_for = lane_for or (lambda ev: lanes[0])
        self.inflight, self.inflight_lock = set(), threading.Lock()  # ids queued or being handled

    def submit(self, ev, name=None):
        """Queue an event; name None means look it up (webhooks only carry the device id). An event that is
        already queued or in flight is not queued again (so retries stay spaced by the poll interval);
        returns whether it was queued."""
        with self.inflight_lock:
            if ev["id"] in self.inflight:
                return False
            self.inflight.add(ev["id"])
        self.queues[self.lane_for(ev)].put((ev, name))
        return True

    def start(self):
        for lane, q in self.queues.items():
            threading.Thread(target=self._run, args=(q,), name=f"porchlink-worker-{lane}", daemon=True).start()
        return self

    def _run(self, q):
        while True:
            ev, name = q.get()
            try:
                self.handle(ev, name)
            except Exception as e:
                print(f"worker error on {ev.get('id')}: {type(e).__name__}: {e}")
            finally:
                with self.inflight_lock:
                    self.inflight.discard(ev.get("id"))
                q.task_done()

    def _unfinished(self):
        return sum(q.unfinished_tasks for q in self.queues.values())

    def drain(self, timeout=5):
        """Wait until every queued event is handled (tests). True if the queues emptied in time."""
        deadline = time.monotonic() + timeout
        while self._unfinished() and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._unfinished()

    def handle(self, ev, name=None):
        """Handle one event at most once. Returns "done", "failed", "duplicate", or None when skipped."""
        claimed = False  # only a claimed event may be marked failed: never overwrite a done one
        try:
            if name is None and self.name_for:
                name = self.name_for(ev)
            if not self.store.begin(ev, name, self.max_attempts):
                return None
            claimed = True
            original = self.store.find_duplicate(ev, DUPLICATE_WINDOW_MS)
            if original:
                self.store.finish(ev["id"], "duplicate", original)
                return "duplicate"
            self.process(ev, name)
        except Exception as e:
            print(f"event {ev.get('id')} failed: {type(e).__name__}: {e}")
            if claimed:
                try:
                    self.store.finish(ev["id"], "failed")
                except Exception:
                    pass
            return "failed"
        try:
            self.store.finish(ev["id"], "done")
        except Exception as e:
            print(f"event {ev.get('id')} failed to finish: {type(e).__name__}: {e}")
            return "failed"
        return "done"

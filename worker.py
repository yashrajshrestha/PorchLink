"""One thread handles every Ring event, from the poller and from webhooks, one at a time.

Serial handling removes races (cooldowns, burst counting), and an event is marked done only after it was
actually handled: a failure is retried when the poll sees the event again, up to max_attempts.
"""
import queue, threading, time

DUPLICATE_WINDOW_MS = 15_000  # same press seen by webhook and by history: start times this close


class EventWorker:
    def __init__(self, store, process, name_for=None, max_attempts=3):
        self.store, self.process, self.name_for, self.max_attempts = store, process, name_for, max_attempts
        self.queue = queue.Queue()
        self.inflight, self.inflight_lock = set(), threading.Lock()  # ids queued or being handled

    def submit(self, ev, name=None):
        """Queue an event; name None means look it up (webhooks only carry the device id). An event that is
        already queued or in flight is not queued again (so retries stay spaced by the poll interval);
        returns whether it was queued."""
        with self.inflight_lock:
            if ev["id"] in self.inflight:
                return False
            self.inflight.add(ev["id"])
        self.queue.put((ev, name))
        return True

    def start(self):
        threading.Thread(target=self._run, name="porchlink-worker", daemon=True).start()
        return self

    def _run(self):
        while True:
            ev, name = self.queue.get()
            try:
                self.handle(ev, name)
            except Exception as e:
                print(f"worker error on {ev.get('id')}: {type(e).__name__}: {e}")
            finally:
                with self.inflight_lock:
                    self.inflight.discard(ev.get("id"))
                self.queue.task_done()

    def drain(self, timeout=5):
        """Wait until every queued event is handled (tests). True if the queue emptied in time."""
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.queue.unfinished_tasks

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

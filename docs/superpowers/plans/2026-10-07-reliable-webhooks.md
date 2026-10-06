# Reliable, Signed Ring Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every real Ring event is handled exactly once, forged webhooks are rejected, and no event is lost when something fails.

**Architecture:**
- **`webhooks.py`** is new and pure. It handles signature checks and parses Ring's real webhook shape.
- **`worker.py`** is new. One queue and one thread process every event, whether it came from the poll or a webhook. It keeps a per-event status in SQLite and retries failures. It also dedupes the same press arriving by both routes.
- **The web endpoint** only validates and enqueues, then answers 200 at once.
- **The simulator** sends signed, real-shape webhooks, including chaos duplicates and forgeries.

**Tech Stack:** Python 3.9+ standard library only (`hmac`, `hashlib`, `queue`, `threading`, `sqlite3`, `http.server`, `ssl`, `unittest`).

**Spec:** `docs/superpowers/specs/2026-10-07-reliable-webhooks-design.md`

## Global Constraints

- **Workspace:** repo `/Users/yashshrestha/PorchLink`, branch `feat/reliable-webhooks`. Python on this machine is 3.10.
- **Dependencies:** standard library only for the core. Add no dependencies.
- **Full test suite:** run from the repo root with `python3 -m unittest test_porchlink test_sim test_vision test_bridge test_decider test_webhooks test_worker test_web`. Run only the files that exist at the time.
  - The baseline is 48 tests, all passing.
  - `test_grab_frame` needs optional dependencies and is not part of the suite.
- **Exact values:**
  - Header `X-Signature`, value `sha256=<64 hex>`, which is the HMAC-SHA256 of the raw body keyed with `RING_WEBHOOK_SECRET`.
  - `MAX_WEBHOOK_BYTES = 65_536`.
  - `DUPLICATE_WINDOW_MS = 15_000`.
  - `max_attempts` default `3`.
  - Webhook event id is `webhook:<meta.request_id>`.
  - Event `source` is `"poll"` or `"webhook"`.
  - Statuses are `pending`, `done`, `failed`, `duplicate` and `history`.
- **Webhook status codes, in check order:** 404 (wrong path or no handler), 503 (no secret), 415 (not `application/json`), 400 (bad `Content-Length`), 413 (too large), 401 (bad signature), 400 (bad JSON or payload), 200 (accepted, or a non-door event that is ignored).
- **Simulator:** the default webhook secret is `sim-secret`, and `--webhook-secret` overrides it, as does the `RING_WEBHOOK_SECRET` env var.
- **Test output:** keep it clean. Patch `builtins.print` in tests that trigger expected log lines.
- **Commits:** messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

---

### Task 1: `webhooks.py` and verified SMTP TLS

**Files:**
- Create: `webhooks.py`
- Create: `test_webhooks.py`
- Modify: `ring_client.py`, so that `parse_event` adds `"source": "poll"`
- Modify: `alerts.py`, to use a verified TLS context
- Modify: `test_porchlink.py`, in `EmailTest.test_sends_over_starttls`

**Interfaces:**
- Produces:
  - `webhooks.sign(secret: str, body: bytes) -> str` (`"sha256=<hex>"`)
  - `webhooks.verify_signature(secret: str, body: bytes, header: str | None) -> bool`
  - `webhooks.DOOR_EVENT_TYPES`
  - `webhooks.parse_webhook(payload: dict) -> dict | None`. It returns an event dict `{"id","device_id","start_ms","end_ms","event_type","sub_type","source"}`, or `None` for an ignored type, and raises `ValueError` on a malformed door event.
  - History events from `ring_client.parse_event` now carry `"source": "poll"`.
  - `ring_client.parse_webhook` stays untouched in this task, because Task 3 removes it.

- [ ] **Step 1: Write the failing tests**

Create `test_webhooks.py`:

```python
"""Tests for Ring webhook signatures and parsing. Run: python3 -m unittest test_webhooks -v"""
import hashlib, hmac, json, unittest

from webhooks import DOOR_EVENT_TYPES, parse_webhook, sign, verify_signature

SECRET = "test-signing-key"


def real_payload(kind="motion_detected", **attrs):
    a = {"source": "ava1.ring.device.ABC123", "source_type": "devices", "timestamp": 1786715596787,
         "component_ids": ["0"], **attrs}
    return {"data": {"type": kind, "attributes": a},
            "meta": {"version": "1.0", "time": "2026-10-07T10:00:00Z", "request_id": "req-123", "account_id": "acc-1"}}


class SignatureTest(unittest.TestCase):
    body = json.dumps(real_payload()).encode()

    def test_sign_matches_hmac_sha256_of_raw_body(self):
        expected = hmac.new(SECRET.encode(), self.body, hashlib.sha256).hexdigest()
        self.assertEqual(sign(SECRET, self.body), "sha256=" + expected)

    def test_correct_signature_verifies(self):
        self.assertTrue(verify_signature(SECRET, self.body, sign(SECRET, self.body)))

    def test_uppercase_hex_is_accepted(self):
        sig = sign(SECRET, self.body)
        self.assertTrue(verify_signature(SECRET, self.body, "sha256=" + sig[7:].upper()))

    def test_rejections(self):
        good = sign(SECRET, self.body)
        cases = {
            "wrong key": (SECRET, self.body, sign("other-key", self.body)),
            "tampered body": (SECRET, self.body + b" ", good),
            "missing header": (SECRET, self.body, None),
            "empty header": (SECRET, self.body, ""),
            "no prefix": (SECRET, self.body, good[7:]),
            "non-hex": (SECRET, self.body, "sha256=" + "z" * 64),
            "wrong length": (SECRET, self.body, good[:-2]),
            "empty secret": ("", self.body, sign("", self.body)),
        }
        for name, args in cases.items():
            self.assertFalse(verify_signature(*args), name)


class ParseTest(unittest.TestCase):
    def test_motion_real_shape(self):
        self.assertEqual(parse_webhook(real_payload(sub_type="human")), {
            "id": "webhook:req-123", "device_id": "ava1.ring.device.ABC123", "start_ms": 1786715596787,
            "end_ms": 1786715596787, "event_type": "motion_detected", "sub_type": "human", "source": "webhook"})

    def test_button_press_has_no_sub_type(self):
        ev = parse_webhook(real_payload("button_press"))
        self.assertEqual((ev["event_type"], ev["sub_type"]), ("button_press", None))

    def test_door_event_types(self):
        self.assertEqual(DOOR_EVENT_TYPES, {"motion_detected", "button_press"})

    def test_non_door_events_are_ignored(self):
        for kind in ("device_online", "device_offline", "device_added", "device_removed",
                     "app_integration_added", "app_integration_removed",
                     "subscription_activated", "subscription_deactivated"):
            self.assertIsNone(parse_webhook(real_payload(kind)), kind)

    def test_malformed_door_events_raise(self):
        no_meta = real_payload()
        del no_meta["meta"]["request_id"]
        no_source = real_payload()
        del no_source["data"]["attributes"]["source"]
        no_time = real_payload()
        del no_time["data"]["attributes"]["timestamp"]
        bad_time = real_payload(timestamp="soon")
        for name, payload in {"no type": {"data": {"attributes": {}}}, "data not an object": {"data": []},
                              "no request_id": no_meta, "no source": no_source, "no timestamp": no_time,
                              "timestamp not a number": bad_time}.items():
            with self.assertRaises(ValueError, msg=name):
                parse_webhook(payload)


if __name__ == "__main__":
    unittest.main()
```

In `test_porchlink.py`, add `import ssl` to the imports. Then replace the line `        server.starttls.assert_called_once()` in `EmailTest.test_sends_over_starttls` with:

```python
        server.starttls.assert_called_once()
        context = server.starttls.call_args.kwargs["context"]  # certificate checked: no MITM on the SMTP password
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
```

- [ ] **Step 2: Run the tests to make sure they fail**

Run: `python3 -m unittest test_webhooks test_porchlink`
Expected: `ModuleNotFoundError: No module named 'webhooks'`, and the email test fails with `KeyError: 'context'`.

- [ ] **Step 3: Create `webhooks.py`**

```python
"""Ring webhook security and parsing. Standard library only, no I/O.

Ring signs every webhook: X-Signature: sha256=<hex HMAC-SHA256 of the raw request body, keyed with the
app's HMAC signing key>. Delivery is at-least-once, so the delivery's meta.request_id is the event id:
a retried delivery has the same id and is stored once.
"""
import hashlib, hmac, re

DOOR_EVENT_TYPES = {"motion_detected", "button_press"}
_SIGNATURE = re.compile(r"sha256=([0-9a-fA-F]{64})")


def sign(secret, body):
    """The X-Signature value Ring would send for this body."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret, body, header):
    """True only for a well-formed header whose HMAC matches; constant-time compare."""
    if not secret or not header:
        return False
    match = _SIGNATURE.fullmatch(header.strip())
    if not match:
        return False
    return hmac.compare_digest(sign(secret, body)[len("sha256="):], match.group(1).lower())


def parse_webhook(payload):
    """Turn a Ring webhook body into an event, or None for non-door events (device online, etc.).
    Raises ValueError when a door event is missing what we need."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("webhook has no data object")
    kind = data.get("type")
    if not kind:
        raise ValueError("webhook has no data.type")
    if kind not in DOOR_EVENT_TYPES:
        return None
    attrs = data.get("attributes") or {}
    request_id = (payload.get("meta") or {}).get("request_id")
    source, timestamp = attrs.get("source"), attrs.get("timestamp")
    missing = [name for name, value in (("meta.request_id", request_id), ("attributes.source", source),
                                        ("attributes.timestamp", timestamp)) if value in (None, "")]
    if missing:
        raise ValueError("webhook is missing " + ", ".join(missing))
    try:
        start_ms = int(timestamp)
    except (TypeError, ValueError):
        raise ValueError("attributes.timestamp is not a number") from None
    return {"id": f"webhook:{request_id}", "device_id": str(source), "start_ms": start_ms, "end_ms": start_ms,
            "event_type": kind, "sub_type": attrs.get("sub_type") or None, "source": "webhook"}
```

- [ ] **Step 4: Mark history events as coming from the poll**

In `ring_client.py`, in `parse_event`, change the returned dict so it ends with `"sub_type": a.get("sub_type"), "source": "poll"}`. The full function becomes:

```python
def parse_event(raw, device_id):
    a = raw.get("attributes") or {}
    return {"id": raw["id"], "device_id": device_id, "start_ms": int(a.get("start") or 0),
            "end_ms": int(a.get("end") or a.get("start") or 0), "event_type": a.get("event_type", "unknown"),
            "sub_type": a.get("sub_type"), "source": "poll"}
```

- [ ] **Step 5: Verify the SMTP server's certificate**

In `alerts.py`, change the import line to `import smtplib, ssl`, and replace `s.starttls()` with:

```python
                s.starttls(context=ssl.create_default_context())  # verify the server's certificate
```

- [ ] **Step 6: Run the tests to make sure they pass**

Run: `python3 -m unittest test_webhooks test_porchlink`
Expected: `OK`.
Then run the full suite from Global Constraints. Expected: `OK`, 48 + 9 new = 57 tests.

- [ ] **Step 7: Commit**

```bash
git add webhooks.py test_webhooks.py ring_client.py alerts.py test_porchlink.py
git commit -m "feat: verify Ring webhook signatures and parse the real payload; verified SMTP TLS

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: event status in the store, and the event worker

**Files:**
- Modify: `store.py`
- Create: `worker.py`
- Create: `test_worker.py`

**Interfaces:**
- Consumes: event dicts with a `"source"` key (Task 1).
- Produces:
  - `Store.begin(ev, name, max_attempts=3) -> bool`
  - `Store.finish(event_id, status)`
  - `Store.record_history(ev, name) -> bool`
  - `Store.find_duplicate(ev, window_ms) -> str | None`
  - `Store.status(event_id) -> str | None`
  - `worker.EventWorker(store, process, name_for=None, max_attempts=3)`, where `process(ev, name)` raises on failure and `name_for(ev) -> str` must not raise. It has:
    - `.submit(ev, name=None)`
    - `.start() -> self`
    - `.handle(ev, name=None) -> "done" | "failed" | "duplicate" | None`
    - `.drain(timeout=5) -> bool`
  - `worker.DUPLICATE_WINDOW_MS = 15_000`.
  - `Store.add_event` stays in this task, because Task 3 removes it.

- [ ] **Step 1: Write the failing tests**

Create `test_worker.py`:

```python
"""Tests for the event worker and per-event status in the store. Run: python3 -m unittest test_worker -v"""
import os, sqlite3, tempfile, unittest
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

    def test_same_press_poll_then_webhook_is_handled_once(self):
        rec = Recorder()
        w = EventWorker(self.store, rec)
        self.assertEqual(w.handle(ev("hist-1", 1_005_000, "poll"), "Front"), "done")
        self.assertEqual(w.handle(ev("webhook:req-1", 1_000_000, "webhook"), "Front"), "duplicate")

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
```

- [ ] **Step 2: Run the tests to make sure they fail**

Run: `python3 -m unittest test_worker`
Expected: `ModuleNotFoundError: No module named 'worker'`.

- [ ] **Step 3: Add event status to `store.py`**

1. In `Store.__init__`, right after the existing `for column in ("sub_type", "description"):` migration loop, add:

```python
            # Event status (added for the worker). Rows from older databases count as done.
            for column, kind in (("source", "TEXT"), ("status", "TEXT DEFAULT 'done'"), ("attempts", "INTEGER DEFAULT 0")):
                try:
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError:
                    pass
```

2. Add these methods to `Store`, after `add_event`:

```python
    _INSERT_EVENT = ("INSERT OR IGNORE INTO events (id, device_id, device_name, start_ms, end_ms, event_type, sub_type,"
                     " source, status, attempts) VALUES (?,?,?,?,?,?,?,?,?,?)")

    def _event_row(self, ev, name, status, attempts):
        return (ev["id"], ev["device_id"], name, ev["start_ms"], ev["end_ms"], ev["event_type"], ev.get("sub_type"),
                ev.get("source"), status, attempts)

    def begin(self, ev, name, max_attempts=3):
        """Claim an event for processing. True when it is new, or an earlier attempt failed (or was cut off
        mid-way by a crash: only the single worker calls this, so a 'pending' row here is a leftover) and
        attempts remain. False for done, duplicate and history events."""
        with self.lock, self.db:
            if self.db.execute(self._INSERT_EVENT, self._event_row(ev, name, "pending", 1)).rowcount == 1:
                return True
            cur = self.db.execute(
                "UPDATE events SET status='pending', attempts=attempts+1"
                " WHERE id=? AND status IN ('failed', 'pending') AND attempts<?", (ev["id"], max_attempts))
            return cur.rowcount == 1

    def finish(self, event_id, status):
        with self.lock, self.db:
            self.db.execute("UPDATE events SET status=? WHERE id=?", (status, event_id))

    def record_history(self, ev, name):
        """Store an event seen on the first poll without alerting. True if it was new."""
        with self.lock, self.db:
            return self.db.execute(self._INSERT_EVENT, self._event_row(ev, name, "history", 0)).rowcount == 1

    def find_duplicate(self, ev, window_ms):
        """Id of an already-handled copy of the same press from the other route (poll vs webhook): same
        device and type, within window_ms. Two events from the same route are never duplicates."""
        with self.lock:
            row = self.db.execute(
                "SELECT id FROM events WHERE device_id=? AND event_type=? AND status='done' AND id<>?"
                " AND COALESCE(source, '') <> ? AND ABS(start_ms - ?) <= ? LIMIT 1",
                (ev["device_id"], ev["event_type"], ev["id"], ev.get("source") or "", ev["start_ms"], window_ms)).fetchone()
        return row[0] if row else None

    def status(self, event_id):
        with self.lock:
            row = self.db.execute("SELECT status FROM events WHERE id=?", (event_id,)).fetchone()
        return row[0] if row else None
```

3. In `starts_since`, change the query to:

```python
            rows = self.db.execute("SELECT start_ms FROM events WHERE device_id=? AND start_ms>=?"
                                   " AND COALESCE(status, 'done') <> 'duplicate'", (device_id, since_ms)).fetchall()
```

4. In `timeline`, change `WHERE e.start_ms >= ? GROUP BY e.id` to `WHERE e.start_ms >= ? AND COALESCE(e.status, 'done') <> 'duplicate' GROUP BY e.id`.

- [ ] **Step 4: Create `worker.py`**

```python
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

    def submit(self, ev, name=None):
        """Queue an event; name None means look it up (webhooks only carry the device id)."""
        self.queue.put((ev, name))

    def start(self):
        threading.Thread(target=self._run, name="porchlink-worker", daemon=True).start()
        return self

    def _run(self):
        while True:
            ev, name = self.queue.get()
            try:
                self.handle(ev, name)
            finally:
                self.queue.task_done()

    def drain(self, timeout=5):
        """Wait until every queued event is handled (tests). True if the queue emptied in time."""
        deadline = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.queue.unfinished_tasks

    def handle(self, ev, name=None):
        """Handle one event at most once. Returns "done", "failed", "duplicate", or None when skipped."""
        try:
            if name is None and self.name_for:
                name = self.name_for(ev)
            if not self.store.begin(ev, name, self.max_attempts):
                return None
            if self.store.find_duplicate(ev, DUPLICATE_WINDOW_MS):
                self.store.finish(ev["id"], "duplicate")
                return "duplicate"
            self.process(ev, name)
        except Exception as e:
            print(f"event {ev.get('id')} failed: {type(e).__name__}: {e}")
            try:
                self.store.finish(ev["id"], "failed")
            except Exception:
                pass
            return "failed"
        self.store.finish(ev["id"], "done")
        return "done"
```

- [ ] **Step 5: Run the tests to make sure they pass**

Run: `python3 -m unittest test_worker`. Expected: `OK` (18 tests).
Then run the full suite. Expected: `OK`, 75 tests.

- [ ] **Step 6: Commit**

```bash
git add store.py worker.py test_worker.py
git commit -m "feat: event worker with per-event status, retries and poll/webhook dedupe

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: wire it end to end (endpoint, app, simulator)

**Files:**
- Modify: `web.py` (secure `/webhook`; add `make_server`)
- Modify: `app.py` (worker, `DeviceNames`, `make_process`, per-device poll isolation; remove `make_webhook_handler`)
- Modify: `ring_client.py` (remove `parse_webhook`)
- Modify: `store.py` (remove `add_event`)
- Modify: `sim/fake_ring_server.py` (real-shape signed webhooks, chaos duplicates and forgeries)
- Modify: `.env.example`, `README.md`
- Create: `test_web.py`
- Modify: `test_porchlink.py`, `test_bridge.py`, `test_vision.py`, `test_sim.py`

**Interfaces:**
- Consumes:
  - From Task 1: `webhooks.verify_signature`, `parse_webhook`, `sign`.
  - From Task 2: `worker.EventWorker`, plus `Store.begin`, `record_history`, `status`, `finish` and `find_duplicate`.
- Produces:
  - `web.make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret="") -> ThreadingHTTPServer`
  - `web.serve(...)`, with the same arguments, which also prints and calls `serve_forever`
  - `web.MAX_WEBHOOK_BYTES`
  - `app.DeviceNames(ring)`, callable as `(ev) -> name`
  - `app.make_process(store, alerter, settings, tz, describe=None, bridge=None) -> process(ev, name)`
  - `app.poll_once(ring, store, submit, alert_new=True) -> int`
  - `app.EventWorker`, re-exported via `from worker import EventWorker`
  - On the simulator `FakeRing`:
    - `.webhook_payload(e) -> dict` (real shape)
    - `.deliveries(e, secret) -> list[(body: bytes, signature: str, forged: bool)]`
    - `.injected_failures: int`
    - `.push_webhooks(url, stop, secret)`

- [ ] **Step 1: Write the failing endpoint tests**

Create `test_web.py`:

```python
"""Tests for the /webhook endpoint: every rejection, and that only a valid signed door event is queued.
Run: python3 -m unittest test_web -v"""
import http.client, json, threading, unittest
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

    def test_charset_parameter_is_fine(self):
        self.start()
        self.assertEqual(self.post(json.dumps(motion()).encode(), {"Content-Type": "application/json; charset=utf-8"}), 200)

    def test_non_door_event_is_acked_but_not_queued(self):
        self.start()
        self.assertEqual(self.post(json.dumps(motion(kind="device_online")).encode()), 200)
        self.assertEqual(self.queued, [])

    def test_rejections(self):
        self.start()
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
        self.assertEqual(self.post(json.dumps(motion()).encode(), sign_with=""), 503)
        self.assertEqual(self.queued, [])

    def test_no_handler_is_404(self):
        self.start(enqueue=False)
        self.assertEqual(self.post(json.dumps(motion()).encode()), 404)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run them to make sure they fail**

Run: `python3 -m unittest test_web`
Expected: `AttributeError: module 'web' has no attribute 'make_server'`.

- [ ] **Step 3: Secure the endpoint in `web.py`**

1. Add to the imports: `from webhooks import parse_webhook, verify_signature`. Below the imports, add `MAX_WEBHOOK_BYTES = 65_536  # Ring webhooks are small; refuse anything bigger before reading it`.
2. Rename `def serve(store, tz, port=8080, on_webhook=None, devices=None):` to `def make_server(store, tz, port=8080, enqueue=None, devices=None, webhook_secret=""):`.
3. Replace the whole `do_POST` method with:

```python
        def do_POST(self):
            if self.path != "/webhook" or not enqueue:
                return self._send(404, "text/plain", "not found")
            if not webhook_secret:
                return self._send(503, "text/plain", "webhook secret not configured")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._send(415, "text/plain", "expected application/json")
            try:
                length = int(self.headers.get("Content-Length") or "")
            except ValueError:
                length = -1
            if length < 0:
                return self._send(400, "text/plain", "bad Content-Length")
            if length > MAX_WEBHOOK_BYTES:
                return self._send(413, "text/plain", "webhook too large")
            body = self.rfile.read(length)
            if not verify_signature(webhook_secret, body, self.headers.get("X-Signature")):
                return self._send(401, "text/plain", "bad signature")
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError("payload is not a JSON object")
                ev = parse_webhook(payload)
            except ValueError as e:
                return self._send(400, "text/plain", f"bad webhook: {e}")
            if ev is not None:  # door event: the worker does the rest; Ring needs a 200 within 5 s
                enqueue(ev, None)
            self._send(200, "text/plain", "ok")
```

4. At the end of the function, replace the last three lines (`server = ThreadingHTTPServer(...)`, `print(...)`, `server.serve_forever()`) with:

```python
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(store, tz, port=8080, enqueue=None, devices=None, webhook_secret=""):
    server = make_server(store, tz, port, enqueue, devices, webhook_secret)
    print(f"Dashboard: http://127.0.0.1:{port}")
    server.serve_forever()
```

- [ ] **Step 4: Wire the worker into `app.py`**

1. Change the imports:
   - `from ring_client import FakeRing, RingClient, TokenExpired, device_name, parse_event` (drop `parse_webhook`)
   - add `from worker import EventWorker`
2. Delete `make_webhook_handler` entirely. Replace `poll_once` with the following, which also adds `DeviceNames` and `make_process`:

```python
class DeviceNames:
    """Device id -> name. Webhooks only carry the id; refresh from Ring on a miss, never raise."""

    def __init__(self, ring):
        self.ring, self.names = ring, {}

    def __call__(self, ev):
        device_id = ev["device_id"]
        if device_id not in self.names:
            try:
                self.names.update({d["id"]: device_name(d) for d in self.ring.devices()})
            except Exception as e:
                print(f"(device names unavailable: {type(e).__name__}: {e})")
        return self.names.get(device_id, f"Ring device ...{device_id[-6:]}")


def make_process(store, alerter, settings, tz, describe=None, bridge=None):
    """The worker's per-event step: rules, home actions, email, records."""
    def process(ev, name):
        handle_event(ev, name, store, alerter, settings, tz, describe, bridge)
    return process


def poll_once(ring, store, submit, alert_new=True):
    """Fetch every device's events (oldest first). Events not yet handled go to submit(ev, name), the
    worker; on the first poll (alert_new=False) new ones are recorded as history without alerting.
    One device failing is logged and skipped; an expired token stops everything.
    Return how many events were seen for the first time."""
    new = 0
    for device in ring.devices():
        name = device_name(device)
        try:
            raws = ring.events(device["id"])
        except TokenExpired:
            raise
        except Exception as e:
            print(f"poll error on {name}: {e}")
            continue
        for ev in sorted((parse_event(raw, device["id"]) for raw in raws), key=lambda e: e["start_ms"]):
            if not alert_new:
                new += store.record_history(ev, name)
                continue
            status = store.status(ev["id"])
            if status is None:
                new += 1
            if status in (None, "failed", "pending"):  # the worker decides; it never handles an event twice
                submit(ev, name)
    return new
```

3. In `main()`, replace the two lines from `on_webhook = make_webhook_handler(...)` through the `threading.Thread(target=web.serve, ...).start()` call with:

```python
    worker = EventWorker(store, make_process(store, alerter, settings, tz, describe, bridge),
                         name_for=DeviceNames(ring)).start()
    webhook_secret = env.get("RING_WEBHOOK_SECRET", "")
    threading.Thread(target=web.serve, args=(store, tz, int(env.get("WEB_PORT", 8080)), worker.submit, devices,
                                             webhook_secret), daemon=True).start()
```

4. In the startup `print(...)`, add `f"webhooks {'signed' if webhook_secret else 'OFF (set RING_WEBHOOK_SECRET)'} | "` right after the email part.
5. In the poll loop, change the call to `poll_once(ring, store, worker.submit, alert_new=not first or env.get("ALERT_ON_STARTUP") == "1")`.

- [ ] **Step 5: Remove the old guessed webhook parser and `add_event`**

1. In `ring_client.py`, delete the whole `parse_webhook` function.
2. In `store.py`, delete the whole `add_event` method.
3. Check: `grep -n "add_event\|ring_client import.*parse_webhook\|make_webhook_handler" *.py sim/*.py` must print nothing.

- [ ] **Step 6: Real-shape, signed webhooks in the simulator**

In `sim/fake_ring_server.py`:

1. Replace the module docstring's second paragraph (the lines saying motion_detected / button_press webhook payloads were not observed and their shape is an assumption) with:

```
Webhooks use Ring's documented shape (data.type + attributes.source/timestamp/sub_type + meta.request_id)
and are signed like Ring's: X-Signature: sha256=<HMAC-SHA256 of the raw body>. Chaos also re-delivers the
same request and sends forged (wrongly signed) webhooks, which the app must refuse.
```

   Change the `Then:` run line in the docstring to `Then: RING_WEBHOOK_SECRET=sim-secret RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token python3 app.py`.

2. Change the imports so the project root is importable when the file is run as a script:

```python
import argparse, json, os, random, sys, threading, time, urllib.error, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # run as a script from anywhere
from webhooks import sign  # noqa: E402
```

3. In `FakeRing.__init__`, inside the events loop, add `"request_id": f"sim-req-{i}"` to the event dict, so it reads `{**e, "id": f"sim.event.{i}", "request_id": f"sim-req-{i}", "start": start, ...}`. After `self.webhooks_sent = set()`, add `self.injected_failures = 0`.

4. In `handle`, inside `if roll < 0.4:`, add `self.injected_failures += 1` before the `return`.

5. Replace `webhook_payload` with:

```python
    @staticmethod
    def webhook_payload(e):
        """Ring's documented webhook shape. request_id is stable per event, so a re-delivery is a true duplicate."""
        attrs = {"source": e["device"], "source_type": "devices", "timestamp": e["start"], "component_ids": ["0"]}
        if e.get("sub_type"):
            attrs["sub_type"] = e["sub_type"]
        sent = datetime.fromtimestamp(e["start"] / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"data": {"type": e["type"], "attributes": attrs},
                "meta": {"version": "1.0", "time": sent, "request_id": e["request_id"], "account_id": "sim.account"}}

    def deliveries(self, e, secret):
        """What gets POSTed for one event: (body, X-Signature, forged). Chaos adds Ring-style re-deliveries
        of the same body and forged webhooks signed with the wrong key."""
        body = json.dumps(self.webhook_payload(e)).encode()
        out = [(body, sign(secret, body), False)]
        if self.chaos and self.rand.random() < self.chaos:
            out.append((body, sign(secret, body), False))
        if self.chaos and self.rand.random() < self.chaos:
            forged = self.webhook_payload(e)
            forged["meta"] = {**forged["meta"], "request_id": f"forged-{self.rand.randrange(10**9)}"}
            forged_body = json.dumps(forged).encode()
            out.append((forged_body, sign("not-the-ring-key", forged_body), True))
        return out
```

6. Replace `push_webhooks` with:

```python
    def push_webhooks(self, url, stop, secret):
        while not stop.is_set():
            for e in self.due_webhooks():
                for body, signature, forged in self.deliveries(e, secret):
                    req = urllib.request.Request(url, data=body, method="POST", headers={
                        "Content-Type": "application/json", "X-Signature": signature})
                    try:
                        urllib.request.urlopen(req, timeout=5).read()
                    except urllib.error.HTTPError as ex:
                        print(f"{'forged webhook refused' if forged else 'webhook rejected'}: HTTP {ex.code}")
                    except Exception as ex:
                        print(f"webhook to {url} failed: {ex}")
            stop.wait(0.5)
```

7. In `main()`, add `p.add_argument("--webhook-secret", default=os.environ.get("RING_WEBHOOK_SECRET") or "sim-secret", help="HMAC key used to sign webhooks (the app's RING_WEBHOOK_SECRET)")`. Change the thread start to `args=(a.webhook, stop, a.webhook_secret)`.

- [ ] **Step 7: Adapt the existing tests to the worker**

1. **`test_porchlink.py`.**
   - Change the imports:
     - `from ring_client import FakeRing, TokenExpired, parse_event`
     - add `from worker import EventWorker`
   - Replace the first three tests of `FlowTest` (through `test_burst_through_store`) with:

```python
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
```

   - Add this class before `EmailTest`:

```python
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
```

2. **`test_bridge.py`, in `EndToEndTest.test_scenario_drives_the_lights`.**
   - Add `from worker import EventWorker` to the imports.
   - Replace `app.poll_once(ring, store, Alerter(), Settings(), TZ, describe=describe, bridge=bridge)` with:

```python
        worker = EventWorker(store, app.make_process(store, Alerter(), Settings(), TZ, describe=describe, bridge=bridge))
        app.poll_once(ring, store, worker.handle)
```

3. **`test_vision.py`, in `DescribeTest.run_scenario`.**
   - Add `from worker import EventWorker` to the imports.
   - Replace `app.poll_once(ring, store, alerter, Settings(), TZ, describe=describe)` with:

```python
        worker = EventWorker(store, app.make_process(store, alerter, Settings(), TZ, describe=describe))
        app.poll_once(ring, store, worker.handle)
```

4. **`test_sim.py`.**
   - Change the imports:
     - add `from unittest import mock`
     - add `import web`
     - add `from webhooks import parse_webhook, verify_signature`
     - add `from worker import EventWorker`
   - Add a module constant `SECRET = "sim-secret"`.
   - Replace `test_rules_fire_for_scenario`'s poll line with:

```python
        worker = EventWorker(self.store, app.make_process(self.store, self.alerter, Settings(), TZ))
        app.poll_once(ring, self.store, worker.handle)
```

   - Add to `FakeServerTest`:

```python
    def test_webhooks_are_real_shape_and_signed(self):
        self.start()
        e = next(x for x in self.fake.events if x["type"] == "button_press")
        [(body, signature, forged)] = self.fake.deliveries(e, SECRET)  # no chaos: exactly one delivery
        self.assertFalse(forged)
        self.assertTrue(verify_signature(SECRET, body, signature))
        ev = parse_webhook(json.loads(body))
        self.assertEqual((ev["id"], ev["device_id"], ev["start_ms"], ev["event_type"]),
                         (f"webhook:{e['request_id']}", e["device"], e["start"], "button_press"))
```

   - Replace `test_monkey_chaos_every_event_alerts_once` with:

```python
    def test_monkey_chaos_every_event_handled_once(self):
        """Injected 500s, duplicated and shuffled history, Ring-style webhook re-deliveries and forged webhooks.
        Poll and signed webhooks race through the real endpoint into one worker: every door event is handled
        exactly once, by exactly one route, and every forgery is refused."""
        ring = self.start(chaos=0.5)
        worker = EventWorker(self.store, app.make_process(self.store, self.alerter, Settings(), TZ),
                             name_for=app.DeviceNames(ring)).start()
        server = web.make_server(self.store, TZ, 0, worker.submit, None, SECRET)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/webhook"

        def post(body, signature):
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-Signature": signature})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code

        forged = 0
        with mock.patch("builtins.print"):
            for _ in range(40):
                try:
                    app.poll_once(ring, self.store, worker.submit)
                except RuntimeError:
                    pass  # devices() hit an injected 500: the real loop logs it and polls again
                for e in self.fake.due_webhooks():
                    for body, signature, is_forged in self.fake.deliveries(e, SECRET):
                        self.assertEqual(post(body, signature), 401 if is_forged else 200)
                        forged += is_forged
            self.assertTrue(worker.drain(10))
            self.fake.chaos = 0
            app.poll_once(ring, self.store, worker.submit)  # quiet poll picks up anything missed
            self.assertTrue(worker.drain(10))

        self.assertGreater(self.fake.injected_failures, 0, "chaos should have injected some failures")
        self.assertGreater(forged, 0, "chaos should have sent some forged webhooks")
        status = dict(self.store.db.execute("SELECT id, status FROM events").fetchall())
        self.assertFalse({"pending", "failed"} & set(status.values()), status)
        for e in self.fake.events:  # each physical event: handled once, by the poll or by its webhook
            routes = [status.get(e["id"]), status.get(f"webhook:{e['request_id']}")]
            self.assertEqual(routes.count("done"), 1, (e["id"], routes))
        self.assertNotIn("forged", " ".join(status))  # forgeries never reach the store
        self.assertEqual(len(self.alerter.sent), len(self.fake.events))  # one email per event, no repeats
        for kinds, rows in self.alerts_per_event().values():
            self.assertEqual(kinds, rows)  # no alert kind recorded twice for the same event
```

- [ ] **Step 8: Document the secret**

1. In `.env.example`, after the `RING_FAKE=0` line, add:

```
# Ring webhooks: the HMAC signing key from your Ring app credentials. Webhooks are refused (503) until it is set.
# The local simulator signs with "sim-secret" unless you pass --webhook-secret.
RING_WEBHOOK_SECRET=
```

2. In `README.md`:
   - Replace `RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token python3 app.py` with `RING_WEBHOOK_SECRET=sim-secret RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token python3 app.py`.
   - Replace `rm -f porchlink.db && RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token \` with `rm -f porchlink.db && RING_WEBHOOK_SECRET=sim-secret RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token \`.
   - Replace the bullet starting `- Assumption: real webhook payloads were not observed` with:

```
- Webhooks follow Ring's documented shape and signature: `X-Signature: sha256=<HMAC-SHA256 of the raw body>` with
  `RING_WEBHOOK_SECRET` (the app refuses unsigned or wrongly signed webhooks, and all webhooks while the secret is unset).
  The id is `meta.request_id`, so Ring's re-deliveries are stored once, and a press seen by both the webhook and the
  history poll is handled once (`worker.py`). Chaos mode also sends forged webhooks; the app answers them 401.
```

   - In the `## Test` section, change the test command to `python3 -m unittest test_porchlink test_sim test_vision test_bridge test_decider test_webhooks test_worker test_web -v`.

- [ ] **Step 9: Run everything**

Run: `python3 -m unittest test_web`. Expected: `OK` (6 tests).
Run the full suite. Expected: `OK`. The count is about 75 + 6 (`test_web`) + 3 (`PollIsolationTest`) + 1 (sim shape) = 85, and the chaos test is renamed, not added. The output must have no stray prints.

Then a manual smoke check, from the repo root:
1. Start the simulator and app in two terminals:
   - `python3 sim/fake_ring_server.py sim/scenarios/night_visit.json --speed 600 --chaos 0.3 --webhook http://127.0.0.1:8080/webhook`
   - `RING_WEBHOOK_SECRET=sim-secret RING_BASE_URL=http://127.0.0.1:9000 RING_TOKEN=sim-token POLL_SECONDS=2 ALERT_ON_STARTUP=1 DB_PATH=/tmp/porchlink-smoke.db python3 app.py` (start this one first; `ALERT_ON_STARTUP=1` so the first poll alerts too, and a throwaway database)
2. Let them run about 15 s.
3. Expected:
   - the simulator prints `forged webhook refused: HTTP 401` at least once;
   - the app prints one `[ALERT]` per scenario event, with no repeats.
4. Stop both with Ctrl+C. Record the counts in your report.

- [ ] **Step 10: Commit**

```bash
git add web.py app.py ring_client.py store.py sim/fake_ring_server.py .env.example README.md test_web.py test_porchlink.py test_bridge.py test_vision.py test_sim.py
git commit -m "feat: signed webhook endpoint feeding one worker; real-shape signed simulator with forged-webhook chaos

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

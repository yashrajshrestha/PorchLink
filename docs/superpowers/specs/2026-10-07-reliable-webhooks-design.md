# Reliable, signed Ring events — design

**Date:** 2026-10-07 · **Status:** approved by the user in chat ("ok") · **Hackathon plan:** days 1–3 of `docs/hackathon-plan.md`

## Goal

Every real Ring doorbell press or motion event is handled exactly once, forged webhooks are rejected, and no event is lost when something fails. This is the base the light patterns (days 4–6) build on.

Out of scope: light patterns and the vision gate, the `ALERT_EVERY_EVENT=0` description bug, OAuth refresh, a public URL (a tunnel for the Playground test; API Gateway on days 7–8), Lambda/DynamoDB.

## Ring facts this relies on

- Webhooks are HTTPS POST with a JSON body. Ring requires an HTTP 200 within 5 seconds and delivers at least once, so the same delivery can repeat with the same `meta.request_id`.
- Each request carries `X-Signature: sha256=<hex>`: lowercase hex HMAC-SHA256 of the **raw request body bytes**, keyed with the app's HMAC signing key.
- Likely payload shape (not yet observed live; the parser must be tolerant):

```json
{"data": {"type": "motion_detected",
          "attributes": {"source": "ava1.ring.device.XXXYYY", "source_type": "devices",
                         "timestamp": 1786715596787, "sub_type": "human", "component_ids": ["0"]}},
 "meta": {"version": "1.0", "time": "2026-10-07T10:00:00Z", "request_id": "req-123", "account_id": "acc-1"}}
```

- Event types: `motion_detected`, `button_press` (no `sub_type`), plus non-door events (`device_added`, `device_removed`, `device_online`, `device_offline`, `app_integration_added`, `app_integration_removed`, `subscription_activated`, `subscription_deactivated`).
- With Smart Alerts off, every motion `sub_type` is `motion`.

## Design

### `webhooks.py` (new): signature + parsing, no I/O

- `verify_signature(secret: str, body: bytes, header: str | None) -> bool`
  - False when the secret is empty, the header is missing, or the header isn't `sha256=<64 hex chars>`.
  - Otherwise compares `hmac.new(secret.encode(), body, sha256).hexdigest()` with the header value via `hmac.compare_digest`. The hex compare is case-insensitive.
- `sign(secret: str, body: bytes) -> str` returns `"sha256=<hex>"`. The simulator and tests use it.
- `DOOR_EVENT_TYPES = {"motion_detected", "button_press"}`.
- `parse_webhook(payload: dict) -> dict | None`:
  - Returns `None` for a valid non-door event type (acknowledged and ignored).
  - Raises `ValueError` when `data.type`, `meta.request_id`, `attributes.source` or `attributes.timestamp` is missing on a door event.
  - Otherwise returns the event dict used everywhere else: `{"id": "webhook:<request_id>", "device_id": source, "start_ms": timestamp, "end_ms": timestamp, "event_type": data.type, "sub_type": attributes.sub_type or None, "source": "webhook"}`.
- `ring_client.parse_webhook` is removed, and so is the old guessed JSON:API/flat webhook shape. `ring_client.parse_event` (history) stays and also gets `"source": "poll"`.

### `web.py`: the `/webhook` endpoint

The checks run in this order, and the first failure answers:

1. Path is not `/webhook`, or no handler is configured → 404.
2. `RING_WEBHOOK_SECRET` is not configured → **503** `webhook secret not configured`, meaning fail closed.
3. `Content-Type` media type is not `application/json` (parameters like `; charset=utf-8` are allowed) → **415**.
4. `Content-Length` is missing, not an integer, or negative → **400**. Over **65,536** bytes → **413**.
5. Read exactly that many bytes. `verify_signature` fails → **401**.
6. Body isn't a JSON object, or `parse_webhook` raises → **400**.
7. `parse_webhook` returns `None` → **200** and nothing else happens.
8. Otherwise enqueue the event on the worker and answer **200** immediately. No Ring API call, vision or bridge work happens in the request thread.

`serve(...)` takes `webhook_secret` and an `enqueue` callable in place of `on_webhook`. Responses are plain text.

### `worker.py` (new): one thread handles every event

- `EventWorker(store, process, max_attempts=3)` owns a `queue.Queue`.
  - `submit(ev, name)` enqueues an event.
  - `start()` runs a daemon thread that loops forever: `ev, name = queue.get()` → `self.handle(ev, name)`.
  - `handle(ev, name)` is also callable directly, for tests and the drain helper.
  - `drain(timeout=5)` waits until the queue is empty, for tests.
- `handle(ev, name)`:
  1. `store.begin(ev, name, max_attempts)` returns True when this event should be processed now: new, or previously `failed` with attempts < max. Otherwise return.
  2. If `store.find_duplicate(ev, window_ms=15_000)` finds a `done` event with the same `device_id` and `event_type`, a different `source`, and `|start_ms − ev.start_ms| ≤ 15 s`, then `store.finish(ev.id, "duplicate")` and return.
  3. Call `process(ev, name)`, which is `app.handle_event` with its dependencies bound. On success, `store.finish(ev.id, "done")`. On any exception, log it with the event id and `store.finish(ev.id, "failed")`. The thread never dies.
- Because only this thread runs `handle_event`, the bridge cooldowns and burst counting no longer race.

### `store.py`: event status

- New columns on `events`: `status TEXT DEFAULT 'done'` (rows from older databases count as done) and `attempts INTEGER DEFAULT 0`, plus `source TEXT`, all added with the existing `ALTER TABLE … ADD COLUMN` migration pattern.
- `begin(ev, name, max_attempts) -> bool`:
  - Inserts a new row with `status='pending'`, `attempts=1`, `source=ev["source"]` and returns True.
  - If the row exists with `status='failed'` and `attempts < max_attempts`, it sets `pending` and `attempts+1` and returns True.
  - Otherwise it returns False.
- `finish(event_id, status)` sets the status.
- `record_history(ev, name) -> bool` inserts with `status='history'` (first-poll backfill, no alert) and returns True if the row was new.
- `find_duplicate(ev, window_ms) -> str | None` returns the matching event id, or None.
- `add_event` is removed; every caller moves to `begin`/`record_history`.
- `starts_since` and the timeline count only rows whose status is not `duplicate`, so a duplicate never inflates a burst.

### `app.py`

- `poll_once(ring, store, worker, alert_new)`:
  - For each device, `ring.events(device_id)` runs inside `try/except Exception`. On error it logs `poll error on <name>: <e>` and continues with the next device.
  - `TokenExpired` is re-raised and still stops the app.
  - New events go to `worker.submit` when `alert_new` is true, otherwise to `store.record_history`.
  - It returns the number of new events seen.
- The webhook path has no Ring API call in the request. Device names are resolved inside the worker's `process` from a cache that is refreshed from `ring.devices()` on a miss. If that call fails, it falls back to `Ring device ...<last 6>`.
- `main()` builds the worker, starts it, and passes `worker.submit` plus `RING_WEBHOOK_SECRET` to `web.serve`.
- `.env.example` documents `RING_WEBHOOK_SECRET`.

### `alerts.py`

`s.starttls(context=ssl.create_default_context())`.

### Simulator (`sim/fake_ring_server.py`)

- Webhooks use the real payload shape above. `source` is the scenario device id, `timestamp` is the event start, `request_id` is `sim-req-<n>` (stable per event), and `sub_type` is included only when the scenario has one.
- Every webhook carries `X-Signature` from `webhooks.sign(secret, body)`. The secret is `--webhook-secret` (default: the `RING_WEBHOOK_SECRET` env var, else `sim-secret`).
- Chaos:
  - A duplicate delivery resends the **same body and request_id**.
  - With probability `chaos`, it also sends one **forged** webhook: a fresh request_id with a wrong signature, which the app must answer with 401.
- History stays as it is. History events keep their own ids, and the same physical event also arrives by webhook, which is exactly the duplicate case the worker handles.

## Testing (stdlib `unittest`, offline)

- **webhooks:**
  - `verify_signature` true for a correct signature, false for: wrong key, tampered body, missing header, `sha256=` with a non-hex or wrong-length value, no `sha256=` prefix, empty secret.
  - Uppercase hex is accepted.
  - `parse_webhook` real shape → expected dict; `button_press` without `sub_type`; `device_online` → None; each missing required field → ValueError.
- **web endpoint** (real `ThreadingHTTPServer` on port 0): 503 without a secret, 415, 400 for a bad length, 413, 401, 400 for bad JSON, 200 + nothing enqueued for `device_online`, and 200 + exactly one enqueued event for a valid signed motion webhook.
- **worker:**
  - A `process` that raises once and then succeeds: the first `handle` → `failed`, a second `handle` of the same event → `done`, with one success.
  - After `max_attempts` failures it is never processed again.
  - A `done` event is never processed twice.
  - Webhook event done, then the poll event for the same press 5 s apart → `duplicate`, so process is called once.
  - 20 s apart → both processed.
- **poll isolation:** a fake Ring whose `events()` raises for device A still delivers device B's events. `TokenExpired` from A propagates.
- **alerts:** a fake `smtplib.SMTP` captures the `starttls` context, which is an `ssl.SSLContext` with `verify_mode == CERT_REQUIRED`.
- **simulator end-to-end:** the existing chaos test is updated to post signed real-shape webhooks to the real web endpoint, with duplicates and forged deliveries. Expected:
  - every scenario door event is processed exactly once;
  - forged deliveries get 401;
  - no event stays `pending` once the worker drains.
- The existing 48 tests keep passing, adapted where they called `add_event`, `parse_webhook` or `poll_once` with the old signature.

"""SQLite storage for seen events and sent alerts."""
import json, sqlite3, threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, device_id TEXT, device_name TEXT,
  start_ms INTEGER, end_ms INTEGER, event_type TEXT);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, automation TEXT, device_id TEXT,
  topic TEXT, command TEXT, ok INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, device_id TEXT,
  event_start_ms INTEGER, kind TEXT, message TEXT, delivered INTEGER);
"""


class Store:
    def __init__(self, path):
        # The web thread reads while the poller writes, so share one connection behind a lock.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            for column in ("sub_type", "description"):  # databases made before these columns existed
                try:
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")
                except sqlite3.OperationalError:
                    pass
            for column, kind in (("skipped", "INTEGER DEFAULT 0"), ("decision_p", "REAL")):  # added for Decider
                try:
                    self.db.execute(f"ALTER TABLE actions ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError:
                    pass
            # Event status (added for the worker). Rows from older databases count as done.
            for column, kind in (("source", "TEXT"), ("status", "TEXT DEFAULT 'done'"), ("attempts", "INTEGER DEFAULT 0")):
                try:
                    self.db.execute(f"ALTER TABLE events ADD COLUMN {column} {kind}")
                except sqlite3.OperationalError:
                    pass

    def add_actions(self, event_id, results):
        with self.lock, self.db:
            self.db.executemany(
                "INSERT INTO actions (event_id, automation, device_id, topic, command, ok, error, skipped, decision_p)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [(event_id, r["automation"], r["device"], r["topic"], json.dumps(r["command"]), int(r["ok"]), r["error"],
                  int(r.get("skipped", False)), r.get("decision_p")) for r in results])

    def actions_for(self, event_ids):
        if not event_ids:
            return {}
        with self.lock:
            rows = self.db.execute(
                f"SELECT event_id, automation, device_id, command, ok, skipped, decision_p FROM actions WHERE event_id IN "
                f"({','.join('?' * len(event_ids))}) ORDER BY id", list(event_ids)).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r[0], []).append({"automation": r[1], "device": r[2], "command": json.loads(r[3]), "ok": bool(r[4]),
                                        "skipped": bool(r[5]), "decision_p": r[6]})
        return out

    def set_description(self, event_id, text):
        with self.lock, self.db:
            self.db.execute("UPDATE events SET description=? WHERE id=?", (text, event_id))

    def add_event(self, ev, name):
        """Insert an event; return True only if it was new. Poller and webhooks both call this,
        so the primary key is what stops a double alert."""
        with self.lock, self.db:
            cur = self.db.execute(
                "INSERT OR IGNORE INTO events (id, device_id, device_name, start_ms, end_ms, event_type, sub_type)"
                " VALUES (?,?,?,?,?,?,?)",
                (ev["id"], ev["device_id"], name, ev["start_ms"], ev["end_ms"], ev["event_type"], ev.get("sub_type")))
            return cur.rowcount == 1

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

    def starts_since(self, device_id, since_ms):
        with self.lock:
            rows = self.db.execute("SELECT start_ms FROM events WHERE device_id=? AND start_ms>=?"
                                   " AND COALESCE(status, 'done') <> 'duplicate'", (device_id, since_ms)).fetchall()
        return [r[0] for r in rows]

    def last_alert_start(self, device_id, kind):
        with self.lock:
            row = self.db.execute("SELECT MAX(event_start_ms) FROM alerts WHERE device_id=? AND kind=?",
                                  (device_id, kind)).fetchone()
        return row[0]

    def add_alert(self, ev, kind, message, delivered):
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO alerts (event_id, device_id, event_start_ms, kind, message, delivered) VALUES (?,?,?,?,?,?)",
                (ev["id"], ev["device_id"], ev["start_ms"], kind, message, int(delivered)))

    def timeline(self, since_ms):
        """Events since a time, newest first, each with its alert kinds."""
        with self.lock:
            rows = self.db.execute("""
                SELECT e.*, GROUP_CONCAT(a.kind) AS kinds, MAX(a.delivered) AS delivered
                FROM events e LEFT JOIN alerts a ON a.event_id = e.id
                WHERE e.start_ms >= ? AND COALESCE(e.status, 'done') <> 'duplicate' GROUP BY e.id ORDER BY e.start_ms DESC""", (since_ms,)).fetchall()
        return [dict(r) for r in rows]

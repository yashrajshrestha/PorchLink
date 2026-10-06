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
    if not isinstance(payload, dict):
        raise ValueError("webhook payload is not a dict")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("webhook has no data object")
    kind = data.get("type")
    if not isinstance(kind, str):
        raise ValueError("webhook has no data.type")
    if kind not in DOOR_EVENT_TYPES:
        return None
    attrs = data.get("attributes")
    if attrs is not None and not isinstance(attrs, dict):
        raise ValueError("webhook attributes is not a dict")
    attrs = attrs or {}
    meta = payload.get("meta")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("webhook meta is not a dict")
    meta = meta or {}
    request_id = meta.get("request_id")
    source, timestamp = attrs.get("source"), attrs.get("timestamp")
    missing = [name for name, value in (("meta.request_id", request_id), ("attributes.source", source),
                                        ("attributes.timestamp", timestamp)) if value in (None, "")]
    if missing:
        raise ValueError("webhook is missing " + ", ".join(missing))
    if isinstance(timestamp, bool):
        raise ValueError("attributes.timestamp is not a number")
    try:
        start_ms = int(timestamp)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("attributes.timestamp is not a number") from None
    return {"id": f"webhook:{request_id}", "device_id": str(source), "start_ms": start_ms, "end_ms": start_ms,
            "event_type": kind, "sub_type": attrs.get("sub_type") or None, "source": "webhook"}

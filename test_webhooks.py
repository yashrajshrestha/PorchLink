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
        inf_time = real_payload(timestamp=float("inf"))
        bool_time = real_payload(timestamp=True)
        attr_string = {"data": {"type": "motion_detected", "attributes": "x"}, "meta": {"request_id": "req-123"}}
        meta_string = {"data": {"type": "motion_detected", "attributes": {}}, "meta": "x"}
        type_list = {"data": {"type": ["motion_detected"], "attributes": {}}, "meta": {"request_id": "req-123"}}
        for name, payload in {"no type": {"data": {"attributes": {}}}, "data not an object": {"data": []},
                              "no request_id": no_meta, "no source": no_source, "no timestamp": no_time,
                              "timestamp not a number": bad_time, "payload not a dict": [1],
                              "data.type is a list": type_list, "attributes is a string": attr_string,
                              "meta is a string": meta_string, "timestamp is inf": inf_time,
                              "timestamp is bool": bool_time}.items():
            with self.assertRaises(ValueError, msg=name):
                parse_webhook(payload)


if __name__ == "__main__":
    unittest.main()

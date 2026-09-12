import os
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path

os.environ.setdefault("IOT_DB_ENABLED", "0")  # keep unit tests off the real MySQL
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ containing server.py
import server
from iot_middleware.normalization import normalize


def install_rule(**overrides):
    """Inject an in-memory rule so tests do not depend on the database."""
    rule = {"id": 1, "device_code": "*", "point_code": "temperature", "operator": ">",
            "threshold": 90.0, "duration_sec": 0, "hysteresis": 5.0,
            "level": "SERIOUS", "enabled": True}
    rule.update(overrides)
    server.RULE_ENGINE["rules"] = [rule]
    server.RULE_ENGINE["loadedAt"] = time.time()  # prevent a reload from the (disabled) DB
    server.RULE_ENGINE["state"] = {}
    return rule


class NormalizeTests(unittest.TestCase):
    def test_rejects_missing_identity(self):
        with self.assertRaises(ValueError):
            normalize({"pointCode": "temperature", "rawValue": 1})

    def test_rejects_non_numeric_value(self):
        with self.assertRaises(ValueError):
            normalize({"deviceCode": "D", "pointCode": "t", "rawValue": "hot"})

    def test_rejects_nan_and_infinity(self):
        # float("nan") parses fine, so an explicit finite check is required;
        # letting it through would serialize as invalid JSON and break every
        # browser parsing the API responses.
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                normalize({"deviceCode": "D", "pointCode": "t", "rawValue": bad})

    def test_accepts_bool_and_finite_numbers(self):
        self.assertTrue(normalize({"deviceCode": "D", "pointCode": "t", "rawValue": True})["value"])
        self.assertEqual(normalize({"deviceCode": "D", "pointCode": "t", "rawValue": "3.5"})["value"], 3.5)


class ProcessMessageTests(unittest.TestCase):
    def setUp(self):
        server.DATA["devices"].clear()
        server.DATA["telemetry"].clear()
        server.DATA["alarms"].clear()
        server.DATA["stats"]["received"] = 0
        server.ACTIVE_DEVICE_CODES_BY_SOURCE = {}
        server.RULE_ENGINE["rules"] = []
        server.RULE_ENGINE["state"] = {}
        server.RULE_ENGINE["loadedAt"] = time.time()

    def telemetry(self, code="PLC-001", point="temperature", value=70.0, event="e"):
        server.process_message("factory/F1/telemetry/normalized",
                               {"deviceCode": code, "pointCode": point, "value": value,
                                "timestamp": "2026-09-12T10:00:00Z", "eventId": event})

    def test_malformed_payload_raises_for_caller_to_catch(self):
        with self.assertRaises(ValueError):
            server.process_message("factory/F1/telemetry/normalized", {"value": 1})
        with self.assertRaises(ValueError):
            server.process_message("factory/F1/telemetry/normalized", "not-a-dict")

    def test_telemetry_updates_device_state(self):
        self.telemetry(code="PLC-001", point="pressure", value=0.7, event="e1")
        self.assertEqual(server.DATA["stats"]["received"], 1)
        device = server.DATA["devices"]["PLC-001"]
        self.assertEqual(device["status"], "ONLINE")
        self.assertEqual(device["points"]["pressure"]["value"], 0.7)
        self.assertEqual(len(server.DATA["telemetry"]), 1)

    def test_alarm_opens_and_resolves(self):
        install_rule()
        self.telemetry(value=95, event="hot")
        alarm = server.DATA["alarms"][0]
        self.assertEqual(alarm["status"], "ACTIVE")
        self.assertEqual(alarm["threshold"], 90.0)
        self.assertFalse(alarm["ack"])

        self.telemetry(value=86, event="still")  # inside the 5-unit hysteresis band
        self.assertEqual(alarm["status"], "ACTIVE")
        self.telemetry(value=84, event="cool")   # below threshold - hysteresis
        self.assertEqual(alarm["status"], "RESOLVED")
        self.assertIn("recoveredAt", alarm)

    def test_active_alarm_not_duplicated(self):
        install_rule()
        self.telemetry(value=95, event="hot1")
        self.telemetry(value=97, event="hot2")
        self.telemetry(value=99, event="hot3")
        active = [a for a in server.DATA["alarms"] if a["status"] == "ACTIVE"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["value"], 95)  # first trigger value is kept

    def test_duration_requires_sustained_breach(self):
        install_rule(duration_sec=10)
        base = time.time()
        server.evaluate_rules("D1", "temperature", 95, base)        # breach starts
        server.evaluate_rules("D1", "temperature", 95, base + 5)    # still within duration
        self.assertEqual(list(server.DATA["alarms"]), [])
        server.evaluate_rules("D1", "temperature", 95, base + 10)   # sustained long enough
        self.assertEqual(len(server.DATA["alarms"]), 1)
        server.evaluate_rules("D1", "temperature", 84, base + 11)   # recovers
        self.assertEqual(server.DATA["alarms"][0]["status"], "RESOLVED")

    def test_transient_breach_never_fires(self):
        install_rule(duration_sec=10)
        base = time.time()
        server.evaluate_rules("D1", "temperature", 95, base)
        server.evaluate_rules("D1", "temperature", 80, base + 5)    # back to normal early
        server.evaluate_rules("D1", "temperature", 95, base + 20)   # a fresh breach restarts
        self.assertEqual(list(server.DATA["alarms"]), [])

    def test_sustained_breach_fires_from_other_devices_frames(self):
        # A dead-zone-suppressed device stops sending frames while still in
        # breach; other devices' frames must keep the engine ticking so the
        # pending alarm still fires after duration_sec.
        install_rule(duration_sec=10)
        base = time.time()
        server.evaluate_rules("MODBUS-1", "temperature", 95, base)  # single breach frame
        for offset in (2, 4, 6, 8, 10, 12):
            server.evaluate_rules("OTHER", "temperature", 60, base + offset)
        active = [a for a in server.DATA["alarms"]
                  if a["deviceCode"] == "MODBUS-1" and a["status"] == "ACTIVE"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["value"], 95)

    def test_registry_filters_unknown_devices(self):
        server.process_message("factory/F1/device/registry",
                               {"source": "mqtt-simulator", "deviceCodes": ["PLC-001"]})
        self.telemetry(code="INTRUDER-9", event="x1")
        self.assertEqual(server.DATA["stats"]["received"], 0)
        self.assertNotIn("INTRUDER-9", server.DATA["devices"])
        # The registered device still flows through.
        self.telemetry(code="PLC-001", event="x2")
        self.assertEqual(server.DATA["stats"]["received"], 1)

    def test_multi_source_registry_union(self):
        # A first-arriving source must not prune devices owned by sources
        # whose registry has not landed yet; once both sources are known the
        # union governs acceptance and pruning.
        server.process_message("factory/F1/device/registry",
                               {"source": "mqtt-simulator", "deviceCodes": ["SIM-1"]})
        server.process_message("factory/F1/device/registry",
                               {"source": "modbus-gateway", "deviceCodes": ["MODBUS-1"]})
        self.telemetry(code="MODBUS-1", event="m1")
        self.assertEqual(server.DATA["stats"]["received"], 1)
        self.assertIn("SIM-1", server.DATA["devices"])

        # The gateway re-announces with its device gone: MODBUS-1 is pruned
        # against the union, SIM-1 survives.
        server.process_message("factory/F1/device/registry",
                               {"source": "modbus-gateway", "deviceCodes": []})
        self.assertNotIn("MODBUS-1", server.DATA["devices"])
        self.assertIn("SIM-1", server.DATA["devices"])
        self.telemetry(code="MODBUS-1", event="m2")
        self.assertEqual(server.DATA["stats"]["received"], 1)

    def test_registry_prunes_removed_devices(self):
        server.process_message("factory/F1/device/registry",
                               {"source": "modbus-gateway", "deviceCodes": ["MODBUS-1"]})
        server.process_message("factory/F1/device/registry",
                               {"source": "modbus-gateway", "deviceCodes": []})
        self.assertNotIn("MODBUS-1", server.DATA["devices"])

    def test_removed_status_deletes_device(self):
        server.process_message("factory/F1/device/registry",
                               {"source": "mqtt-simulator", "deviceCodes": ["PLC-001"]})
        self.telemetry(code="PLC-001", event="seed")
        server.process_message("factory/F1/device/PLC-001/status",
                               {"deviceCode": "PLC-001", "status": "OFFLINE", "reason": "REMOVED"})
        self.assertNotIn("PLC-001", server.DATA["devices"])
        self.assertNotIn("PLC-001", server.ACTIVE_DEVICE_CODES_BY_SOURCE["mqtt-simulator"])


class RestoreAlarmsTests(unittest.TestCase):
    def setUp(self):
        server.DATA["alarms"].clear()
        server.RULE_ENGINE["state"] = {}
        install_rule()

    def test_restore_rebuilds_alarms_and_rule_state(self):
        fake_rows = [("T-9", "temperature", "SERIOUS", 95.0, 90.0,
                      datetime(2026, 9, 12, 10, 0, 0), None)]
        original_query = server.DB.query
        server.DB.query = lambda sql, params=(): (
            fake_rows if "iot_alarm_record" in sql else None)
        try:
            server.restore_active_alarms()
        finally:
            server.DB.query = original_query

        self.assertEqual(len(server.DATA["alarms"]), 1)
        alarm = server.DATA["alarms"][0]
        self.assertEqual(alarm["deviceCode"], "T-9")
        self.assertEqual(alarm["status"], "ACTIVE")
        self.assertFalse(alarm["ack"])
        self.assertTrue(server.RULE_ENGINE["state"][(1, "T-9")]["active"],
                        "restored alarms must re-arm their rule state")

    def test_restore_no_rows_is_noop(self):
        original_query = server.DB.query
        server.DB.query = lambda sql, params=(): None
        try:
            server.restore_active_alarms()
        finally:
            server.DB.query = original_query
        self.assertEqual(len(server.DATA["alarms"]), 0)


if __name__ == "__main__":
    unittest.main()

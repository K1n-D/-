import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("IOT_DB_ENABLED", "0")  # keep unit tests off the real MySQL
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ containing server.py
import server
from iot_middleware.normalization import normalize


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
        self.telemetry(value=95, event="hot")
        alarm = server.DATA["alarms"][0]
        self.assertEqual(alarm["status"], "ACTIVE")
        self.assertEqual(alarm["threshold"], server.ALARM_TEMPERATURE_THRESHOLD)

        self.telemetry(value=85, event="cool")
        self.assertEqual(alarm["status"], "RESOLVED")
        self.assertIn("recoveredAt", alarm)

    def test_active_alarm_not_duplicated_or_reopened(self):
        self.telemetry(value=95, event="hot1")
        self.telemetry(value=97, event="hot2")  # worse reading while active
        self.telemetry(value=99, event="hot3")
        active = [a for a in server.DATA["alarms"] if a["status"] == "ACTIVE"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["value"], 99)

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


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from iot_middleware.main import Middleware


class MiddlewareReliabilityTests(unittest.TestCase):
    def test_duplicate_event_is_ignored(self):
        middleware = Middleware()
        payload = {"deviceCode": "PLC-001", "pointCode": "temperature", "rawValue": 75, "eventId": "evt-1"}
        middleware.on_message("factory/F/device/PLC-001/raw", payload)
        middleware.on_message("factory/F/device/PLC-001/raw", payload)
        self.assertEqual(middleware.stats["normalized"], 2)
        self.assertEqual(middleware.stats["duplicates"], 1)

    def test_heartbeat_sequence_must_increase(self):
        middleware = Middleware()
        base = {"deviceCode": "PLC-001", "clientId": "sim-plc-001", "sentEpoch": 1}
        middleware.on_message("factory/F/device/PLC-001/heartbeat", {**base, "sequenceNo": 2})
        middleware.on_message("factory/F/device/PLC-001/heartbeat", {**base, "sequenceNo": 1})
        self.assertEqual(middleware.devices["PLC-001"]["sequenceNo"], 2)
        self.assertEqual(middleware.stats["invalid"], 1)

    def test_invalid_heartbeat_identity_is_rejected(self):
        middleware = Middleware()
        middleware.on_message("factory/F/device/PLC-001/heartbeat", {"deviceCode": "PLC-001"})
        self.assertEqual(middleware.stats["invalid"], 1)
        self.assertNotIn("PLC-001", middleware.devices)


if __name__ == "__main__":
    unittest.main()

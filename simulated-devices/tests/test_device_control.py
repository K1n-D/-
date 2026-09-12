import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sim_devices.main import DeviceManager


class DeviceControlTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = DeviceManager(Path(self.temp_dir.name) / "devices.json")

    def tearDown(self):
        for device in list(self.manager.devices.values()):
            device.stop()
        self.temp_dir.cleanup()

    def test_add_update_and_remove_without_starting_worker(self):
        created = self.manager.add({
            "deviceCode": "TEST-001",
            "deviceType": "TEMPERATURE",
            "name": "测试温度设备",
            "enabled": False,
            "points": {"temperature": {"base": 30, "variation": 2, "unit": "C"}},
        })
        self.assertEqual(created["status"], "OFFLINE")
        self.assertFalse(created["enabled"])
        updated = self.manager.update("TEST-001", {"name": "修改后的设备", "scenario": "high-temperature", "intervalSec": .5})
        self.assertEqual(updated["name"], "修改后的设备")
        self.assertEqual(updated["scenario"], "high-temperature")
        self.assertEqual(updated["intervalSec"], .5)
        self.assertEqual(self.manager.remove("TEST-001")["removed"], True)
        self.assertEqual(self.manager.list(), [])

    def test_device_code_and_type_are_validated(self):
        with self.assertRaises(ValueError):
            self.manager.add({"deviceCode": "", "enabled": False})
        with self.assertRaises(ValueError):
            self.manager.add({"deviceCode": "TEST-002", "deviceType": "UNKNOWN", "enabled": False})
        self.manager.add({"deviceCode": "TEST-002", "enabled": False})
        with self.assertRaises(ValueError):
            self.manager.add({"deviceCode": "TEST-002", "enabled": False})

    def test_configuration_survives_manager_reload(self):
        self.manager.add({
            "deviceCode": "PERSIST-001",
            "deviceType": "PRESSURE",
            "name": "持久化压力设备",
            "scenario": "low-pressure",
            "enabled": False,
            "intervalSec": 3,
            "heartbeatIntervalSec": 20,
            "points": {"pressure": {"base": .4, "variation": .01, "unit": "MPa"}},
        })
        reloaded = DeviceManager(self.manager.state_path)
        reloaded.load_or_seed()
        restored = reloaded.list()[0]
        self.assertEqual(restored["deviceCode"], "PERSIST-001")
        self.assertFalse(restored["enabled"])
        self.assertEqual(restored["scenario"], "low-pressure")
        self.assertEqual(restored["points"]["pressure"]["base"], .4)
        for device in reloaded.devices.values():
            device.stop()


if __name__ == "__main__":
    unittest.main()

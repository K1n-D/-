import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(ROOT / "middleware" / "src"))

from edge_collector.collector import CollectorDevice, engineering_value, read_point, should_report
from edge_collector.point_table import GatewayConfig, PointConfig, load_point_table
from edge_collector.registers import RegisterImage


def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class FakeMqtt:
    """Collects everything the collector would publish to the broker."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload, retain))


class LogicTests(unittest.TestCase):
    def test_engineering_value_applies_scale(self):
        self.assertAlmostEqual(engineering_value(650, 0.1), 65.0)
        self.assertAlmostEqual(engineering_value(650, 0.001), 0.65)

    def test_dead_zone_suppresses_small_changes(self):
        self.assertTrue(should_report(65.0, None, 0.1))       # first reading always reports
        self.assertFalse(should_report(65.05, 65.0, 0.1))     # within dead zone
        self.assertTrue(should_report(65.2, 65.0, 0.1))       # beyond dead zone

    def test_point_table_loads_and_validates(self):
        table = load_point_table(Path(__file__).resolve().parents[1] / "configs" / "point-table.yml")
        self.assertEqual(len(table.points), 3)
        self.assertEqual(table.points[0].device_code, "MODBUS-PLC-001")
        self.assertEqual(table.points[1].scale_factor, 0.001)

    def test_register_image_scenarios(self):
        normal = RegisterImage(scenario="normal")
        registers = normal.update(now=normal._started + 4)
        self.assertEqual(registers[3], 1)  # running status word
        hot = RegisterImage(scenario="high-temperature")
        self.assertEqual(hot.update(now=hot._started + 1)[0], 950)  # temperature x 10 pinned at 95 C


class CollectorEndToEnd(unittest.TestCase):
    """Spawns the slave exactly like production does: its own process."""

    def setUp(self):
        self.port = free_port()
        self.image = RegisterImage(scenario="normal")
        env = dict(os.environ, PYTHONPATH=f"{ROOT / 'edge-collector' / 'src'}{os.pathsep}{ROOT / 'middleware' / 'src'}")
        # --freeze keeps the slave's registers at their initial values, so the
        # expected engineering value (65.0) is deterministic regardless of
        # runner speed; the register-writer cadence is exercised by the slave
        # running in the full stack, not here.
        self.slave = subprocess.Popen(
            [sys.executable, "-u", "-X", "utf8", "-m", "edge_collector.slave",
             "--port", str(self.port), "--freeze"],
            cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self._reachable():
                return
            time.sleep(0.2)
        self.fail(f"modbus slave did not start on {self.port}")

    def _reachable(self):
        probe = socket.socket()
        probe.settimeout(0.5)
        try:
            probe.connect(("127.0.0.1", self.port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def tearDown(self):
        self.slave.terminate()
        self.slave.wait(timeout=5)

    def _collector(self, **point_overrides):
        point = PointConfig(device_code="M-TEST", point_code="temperature", register=0,
                            scale_factor=0.1, dead_zone=0.0, collect_interval_ms=0, unit="C")
        for key, value in point_overrides.items():
            setattr(point, key, value)
        gateway = GatewayConfig(id="test-gw", host="127.0.0.1", port=self.port,
                                heartbeat_interval_sec=60, points=[point])
        device = CollectorDevice(gateway)
        device.client = FakeMqtt()
        return device

    def _connect_modbus(self, device):
        from pymodbus.client import ModbusTcpClient
        device.modbus = ModbusTcpClient("127.0.0.1", port=self.port, timeout=3)
        self.assertTrue(device.modbus.connect(), "collector failed to connect to the slave")

    def test_collector_reads_registers_and_publishes_telemetry(self):
        device = self._collector()
        self._connect_modbus(device)
        try:
            ok, failed = device.poll_once(time.monotonic())
            self.assertEqual((ok, failed), (1, 0))
        finally:
            device.modbus.close()

        raw = self.image.encode()[0]
        expected = round(engineering_value(raw, 0.1), 4)
        telemetry = [p for topic, p, _ in device.client.published if topic.endswith("/raw")]
        self.assertEqual(len(telemetry), 1, device.client.published)
        self.assertEqual(telemetry[0]["deviceCode"], "M-TEST")
        self.assertEqual(telemetry[0]["pointCode"], "temperature")
        self.assertAlmostEqual(float(telemetry[0]["rawValue"]), expected, delta=0.11)

    def test_dead_zone_suppresses_unchanged_registers(self):
        device = self._collector(dead_zone=999.0)  # nothing can cross this zone
        self._connect_modbus(device)
        try:
            device.poll_once(time.monotonic())
            device.poll_once(time.monotonic() + 10)
        finally:
            device.modbus.close()
        telemetry = [p for topic, p, _ in device.client.published if topic.endswith("/raw")]
        self.assertEqual(len(telemetry), 1, "dead zone should hold back the second round")

    def test_registry_and_heartbeat_published_on_timer(self):
        device = self._collector()
        self._connect_modbus(device)
        try:
            device.publish_registry()
            device.publish_heartbeat("M-TEST")
        finally:
            device.modbus.close()
        registry = [p for topic, p, retain in device.client.published
                    if topic.endswith("/device/registry") and retain]
        self.assertEqual(registry and registry[0]["source"], "modbus-gateway")
        self.assertIn("M-TEST", registry[0]["deviceCodes"])
        heartbeat = [p for topic, p, _ in device.client.published if topic.endswith("/heartbeat")]
        self.assertEqual(heartbeat[0]["deviceCode"], "M-TEST")

    def test_read_point_returns_clean_error_when_slave_is_down(self):
        dead_port = free_port()
        from pymodbus.client import ModbusTcpClient
        client = ModbusTcpClient("127.0.0.1", port=dead_port, timeout=0.5)
        client.connect()
        try:
            raw, error = read_point(client, PointConfig(device_code="M-X", point_code="p", register=0))
        finally:
            client.close()
        self.assertIsNone(raw)
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main()

"""Modbus TCP edge collector: polls a PLC per the point table and publishes
the readings as normalized-ready MQTT telemetry.

The wire payload deliberately matches the one the MQTT-direct simulator emits
(``deviceCode/pointCode/rawValue/...`` on ``factory/.../device/<code>/raw``)
so the middleware, backend and dashboards treat both access paths identically.

The collector also owns the device's heartbeat and status lifecycle: while
register reads succeed it publishes heartbeats (so the middleware watchdog
keeps the device ONLINE); when reads fail repeatedly it stops heartbeats and
publishes a retained OFFLINE status, and publishes ONLINE again on recovery.
"""
from __future__ import annotations

import logging
import math
import struct
import threading
import time

from edge_collector.point_table import PointConfig
from iot_middleware.local_mqtt import Client
from iot_middleware.normalization import utc_now

FACTORY = "FACTORY-001"
REGISTRY_TOPIC = f"factory/{FACTORY}/device/registry"
REGISTRY_INTERVAL_SEC = 5.0
FAILS_BEFORE_OFFLINE = 3
# Dead-zone suppression must never fully silence a point: even a constant
# value is re-reported periodically so downstream consumers (and the alarm
# engine's duration gate) keep seeing the current state.
FORCED_REPORT_SEC = 30.0


def engineering_value(raw, scale_factor):
    """Convert a fixed-point register value into engineering units."""
    return raw * scale_factor


def should_report(value, last_value, dead_zone):
    """True when the value moved beyond the dead zone (or was never reported)."""
    if last_value is None:
        return True
    return abs(value - last_value) >= dead_zone


def decode_register_value(registers, data_type, byte_order):
    """Decode one or two raw registers into a signed/float value.

    Byte orders follow the de-facto industry naming for 32-bit values, where
    ABCD is the big-endian byte sequence of the value spread over two words:
    ABCD (big-endian), CDAB (word swap), BADC (byte swap per word) and DCBA
    (little-endian). Single-register types honour signedness only.
    """
    if data_type in ("int16", "uint16"):
        packed = struct.pack(">H", registers[0] & 0xFFFF)
        return struct.unpack(">h" if data_type == "int16" else ">H", packed)[0]
    words = list(registers[:2])
    if byte_order == "CDAB":
        words = [words[1], words[0]]
    elif byte_order == "BADC":
        words = [((w & 0xFF) << 8) | ((w >> 8) & 0xFF) for w in words]
    elif byte_order == "DCBA":
        return struct.unpack("<f" if data_type == "float32" else "<i", struct.pack(">HH", *words))[0]
    packed = struct.pack(">HH", *words)
    return struct.unpack(">f" if data_type == "float32" else ">i", packed)[0]


def merge_blocks(points):
    """Group sorted points into contiguous Modbus read blocks (gap <= 2)."""
    blocks = []
    current = None
    for point in sorted(points, key=lambda p: p.register):
        end = point.register + point.register_count - 1
        if current and point.register - current["end"] <= 2:
            current["end"] = max(current["end"], end)
            current["points"].append(point)
        else:
            current = {"start": point.register, "end": end, "points": [point]}
            blocks.append(current)
    return blocks


def read_block(client, slave_id, register_type, start, count):
    """Read one contiguous register block. Returns (list[int] | None, error | None)."""
    try:
        if register_type == "input":
            result = client.read_input_registers(start, count=count, slave=slave_id)
        else:
            result = client.read_holding_registers(start, count=count, slave=slave_id)
        if result.isError():
            return None, str(result)
        return list(result.registers), None
    except Exception as exc:  # pymodbus raises broad exceptions on IO errors
        return None, str(exc)


def read_point(client, point: PointConfig):
    """Read and decode a single point. Returns (value | None, error | None)."""
    registers, error = read_block(client, point.slave_id, point.register_type,
                                  point.register, point.register_count)
    if error is not None:
        return None, error
    if len(registers) < point.register_count:
        return None, f"short read: {len(registers)} of {point.register_count} registers"
    return decode_register_value(registers, point.data_type, point.byte_order), None


class CollectorDevice:
    """Collects every point of one gateway; runs in its own thread."""

    def __init__(self, gateway, mqtt_host="127.0.0.1", mqtt_port=1883):
        self.gateway = gateway
        self.points = gateway.points
        self.device_codes = sorted({point.device_code for point in self.points})
        self.mqtt_host, self.mqtt_port = mqtt_host, mqtt_port
        self.client = None
        self.modbus = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._seq = int(time.time() * 1000) % 100000
        self._last_values = {}      # (device, point) -> engineering value
        self._last_report = {}      # (device, point) -> monotonic time
        self._last_heartbeat = 0.0
        self._last_registry = 0.0
        self._consecutive_fails = 0
        self._offline_published = False
        self.tick = max(0.2, min(point.collect_interval_ms for point in self.points) / 1000)

    # ---- publishing -------------------------------------------------
    def publish_telemetry(self, point: PointConfig, value):
        payload = {
            "deviceCode": point.device_code,
            "pointCode": point.point_code,
            "rawValue": round(value, 4),
            "unit": point.unit,
            "sentTime": utc_now(),
            "eventId": f"{point.device_code}-{point.point_code}-{time.time_ns()}",
            "source": "modbus-gateway",
        }
        self.client.publish(f"factory/{FACTORY}/device/{point.device_code}/raw", payload, qos=1)

    def publish_heartbeat(self, device_code):
        self._seq += 1
        self.client.publish(
            f"factory/{FACTORY}/device/{device_code}/heartbeat",
            {"deviceCode": device_code, "clientId": f"edge-{self.gateway.id}",
             "sequenceNo": self._seq, "sentEpoch": time.time(), "sentTime": utc_now(),
             "deviceStatus": "RUNNING"},
            qos=1,
        )

    def publish_status(self, device_code, status, reason):
        self.client.publish(
            f"factory/{FACTORY}/device/{device_code}/status",
            {"deviceCode": device_code, "status": status, "reason": reason, "timestamp": utc_now()},
            qos=1, retain=True,
        )

    def publish_registry(self):
        self.client.publish(
            REGISTRY_TOPIC,
            {"source": "modbus-gateway", "gatewayId": self.gateway.id,
             "deviceCodes": self.device_codes, "updatedAt": utc_now()},
            qos=1, retain=True,
        )

    # ---- Modbus -----------------------------------------------------
    def _ensure_modbus(self):
        if self.modbus is not None:
            return True
        try:
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(self.gateway.host, port=self.gateway.port, timeout=3)
            if client.connect():
                self.modbus = client
                logging.info("modbus connected %s:%s", self.gateway.host, self.gateway.port)
                return True
            client.close()
        except Exception as exc:
            logging.warning("modbus connect failed: %s", exc)
        return False

    def _close_modbus(self):
        if self.modbus is not None:
            try:
                self.modbus.close()
            except Exception:
                pass
            self.modbus = None

    # ---- lifecycle ---------------------------------------------------
    def _publish_online(self):
        for code in self.device_codes:
            self.publish_status(code, "ONLINE", "COLLECT_RECOVERED")

    def _publish_offline(self):
        for code in self.device_codes:
            self.publish_status(code, "OFFLINE", "COLLECT_ERROR")

    def poll_once(self, now=None):
        """One collection round. Modbus transactions are merged per contiguous
        register block (borrowed from thingsboard-gateway's batching) so
        neighbouring points cost a single request instead of one each.
        Returns the number of points read OK and the number failed."""
        now = time.monotonic() if now is None else now
        ok_reads = 0
        failed = 0
        groups: dict = {}
        for point in self.points:
            groups.setdefault((point.slave_id, point.register_type), []).append(point)
        for (slave_id, register_type), points in groups.items():
            for block in merge_blocks(points):
                count = block["end"] - block["start"] + 1
                registers, error = read_block(self.modbus, slave_id, register_type,
                                              block["start"], count)
                if error is not None:
                    failed += len(block["points"])
                    continue
                for point in block["points"]:
                    offset = point.register - block["start"]
                    raw = registers[offset:offset + point.register_count]
                    if len(raw) < point.register_count:
                        failed += 1
                        continue
                    value = engineering_value(
                        decode_register_value(raw, point.data_type, point.byte_order),
                        point.scale_factor)
                    if not math.isfinite(value):
                        failed += 1
                        continue
                    ok_reads += 1
                    key = (point.device_code, point.point_code)
                    last = self._last_values.get(key)
                    now_since_report = now - self._last_report.get(key, 0)
                    if (should_report(value, last, point.dead_zone) or now_since_report >= FORCED_REPORT_SEC) \
                            and now_since_report >= point.collect_interval_ms / 1000:
                        self.publish_telemetry(point, value)
                        self._last_values[key] = value
                        self._last_report[key] = now
        return ok_reads, failed

    def run(self):
        logging.info("collector %s starting, devices=%s", self.gateway.id, self.device_codes)
        while not self._stop.is_set():
            if not self.client or not self.client.running:
                try:
                    self.client = Client(f"edge-{self.gateway.id}", keepalive=30)
                    self.client.connect()
                    self.client.start_keepalive()
                except OSError as exc:
                    logging.warning("MQTT connect failed: %s", exc)
                    self._stop.wait(2)
                    continue
            if not self._ensure_modbus():
                if not self._offline_published:
                    self._publish_offline()
                    self._offline_published = True
                self._stop.wait(min(30, 2 ** min(self._consecutive_fails, 5)))
                self._consecutive_fails += 1
                continue

            now = time.monotonic()
            ok_reads, failed = self.poll_once(now)

            if failed and not ok_reads:
                self._consecutive_fails += 1
                if self._consecutive_fails >= FAILS_BEFORE_OFFLINE and not self._offline_published:
                    self._close_modbus()
                    self._publish_offline()
                    self._offline_published = True
            else:
                self._consecutive_fails = 0
                if self._offline_published:
                    self._publish_online()
                    self._offline_published = False

            if not self._offline_published:
                if now - self._last_heartbeat >= self.gateway.heartbeat_interval_sec:
                    for code in self.device_codes:
                        self.publish_heartbeat(code)
                    self._last_heartbeat = now
                if now - self._last_registry >= REGISTRY_INTERVAL_SEC:
                    self.publish_registry()
                    self._last_registry = now
            self._stop.wait(self.tick)
        self._close_modbus()
        if self.client and self.client.running:
            self.client.disconnect()

    def start(self):
        thread = threading.Thread(target=self.run, name=f"edge-{self.gateway.id}", daemon=True)
        thread.start()
        return thread

    def stop(self):
        self._stop.set()

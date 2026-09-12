from __future__ import annotations

import argparse
import copy
import json
import math
import os
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "middleware" / "src"))
from iot_middleware.local_mqtt import Client
from iot_middleware.normalization import utc_now


DEVICE_PROFILES = {
    "PLC": {
        "temperature": {"base": 65, "variation": 3, "unit": "C"},
        "pressure": {"base": .65, "variation": .05, "unit": "MPa"},
        "liquid_level": {"base": 72, "variation": 5, "unit": "%"},
    },
    "TEMPERATURE": {"temperature": {"base": 60, "variation": 2, "unit": "C"}},
    "PRESSURE": {"pressure": {"base": .55, "variation": .04, "unit": "MPa"}},
}

LEGACY_DEVICE_TYPES = {"PLC-001": "PLC", "TEMP-001": "TEMPERATURE", "PRESS-001": "PRESSURE"}


class SimDevice:
    def __init__(self, code, device_type="PLC", points=None, scenario="normal", name=None,
                 interval=1.0, heartbeat_interval=10.0, enabled=True):
        self.code = code
        self.device_type = device_type.upper()
        self.name = name or code
        self.points = copy.deepcopy(points or DEVICE_PROFILES.get(self.device_type, DEVICE_PROFILES["PLC"]))
        self.scenario = scenario
        self.interval = max(.2, float(interval))
        self.heartbeat_interval = max(2.0, float(heartbeat_interval))
        self.enabled = bool(enabled)
        self.status = "OFFLINE"
        self.seq = time.time_ns()
        self.started_at = time.time()
        self.last_heartbeat = None
        self.last_data = None
        self.last_error = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self.client = None
        self.retry = 0
        self.stop_reason = "STOPPED"

    def snapshot(self):
        with self._lock:
            return {
                "deviceCode": self.code,
                "name": self.name,
                "deviceType": self.device_type,
                "scenario": self.scenario,
                "enabled": self.enabled,
                "status": self.status,
                "intervalSec": self.interval,
                "heartbeatIntervalSec": self.heartbeat_interval,
                "points": copy.deepcopy(self.points),
                "lastHeartbeat": self.last_heartbeat,
                "lastData": self.last_data,
                "retryCount": self.retry,
                "lastError": self.last_error,
            }

    def update(self, payload):
        with self._lock:
            if "name" in payload:
                self.name = str(payload["name"]).strip() or self.code
            if "scenario" in payload:
                self.scenario = str(payload["scenario"])
            if "intervalSec" in payload:
                self.interval = max(.2, min(60.0, float(payload["intervalSec"])))
            if "heartbeatIntervalSec" in payload:
                self.heartbeat_interval = max(2.0, min(300.0, float(payload["heartbeatIntervalSec"])))
            if isinstance(payload.get("points"), dict) and payload["points"]:
                self.points = copy.deepcopy(payload["points"])

    def _publish_status(self, status, reason):
        if self.client and self.client.running:
            self.client.publish(
                f"factory/FACTORY-001/device/{self.code}/status",
                {"deviceCode": self.code, "status": status, "reason": reason, "timestamp": utc_now()},
                qos=1,
                retain=True,
            )

    def _connect(self):
        will = {
            "topic": f"factory/FACTORY-001/device/{self.code}/status",
            "payload": {"deviceCode": self.code, "status": "OFFLINE", "reason": "LAST_WILL", "timestamp": utc_now()},
        }
        client_id = f"sim-{self.code.lower()}"
        self.client = Client(client_id, keepalive=30, will=will, on_state=self._set_status)
        self.client.connect()
        self.client.start_keepalive()
        self._publish_status("ONLINE", "CONNECTED")
        with self._lock:
            self.status = "ONLINE"
            self.last_error = None

    def _set_status(self, state):
        if state == "DISCONNECTED" and not self._stop.is_set():
            with self._lock:
                self.status = "RECONNECTING"

    def run(self):
        last_hb = 0.0
        try:
            while not self._stop.is_set():
                if not self.client or not self.client.running:
                    try:
                        self._connect()
                        self.retry = 0
                    except OSError as exc:
                        with self._lock:
                            self.status = "RECONNECTING"
                            self.last_error = str(exc)
                        self.retry += 1
                        delay = min(60, 2 ** min(self.retry - 1, 6)) * random.uniform(.8, 1.2)
                        self._stop.wait(delay)
                        continue
                now = time.time()
                elapsed = now - self.started_at
                with self._lock:
                    heartbeat_interval = self.heartbeat_interval
                    scenario = self.scenario
                if now - last_hb >= heartbeat_interval and not (scenario == "heartbeat-loss" and elapsed >= 15):
                    self.publish_heartbeat(now)
                    last_hb = now
                with self._lock:
                    point_items = list(self.points.items())
                    interval = self.interval
                for point, config in point_items:
                    self.publish_point(point, config, elapsed)
                self._stop.wait(interval)
        finally:
            if self.client and self.client.running:
                self._publish_status("OFFLINE", self.stop_reason)
                self.client.disconnect()
            with self._lock:
                self.status = "OFFLINE"

    def publish_heartbeat(self, now):
        self.seq += 1
        payload = {
            "deviceCode": self.code,
            "clientId": f"sim-{self.code.lower()}",
            "sequenceNo": self.seq,
            "sentEpoch": now,
            "sentTime": utc_now(),
            "deviceStatus": "RUNNING",
        }
        self.client.publish(f"factory/FACTORY-001/device/{self.code}/heartbeat", payload, qos=1)
        with self._lock:
            self.last_heartbeat = utc_now()

    def publish_point(self, point, config, elapsed):
        base = float(config.get("base", 0))
        variation = float(config.get("variation", 1))
        value = base + math.sin(elapsed / 8) * variation + random.uniform(-variation * .2, variation * .2)
        with self._lock:
            scenario = self.scenario
        if scenario == "high-temperature" and point == "temperature":
            value = 95
        if scenario == "low-pressure" and point == "pressure":
            value = .2
        payload = {
            "deviceCode": self.code,
            "pointCode": point,
            "rawValue": round(value, 3),
            "unit": config.get("unit", ""),
            "sentTime": utc_now(),
            "eventId": f"{self.code}-{point}-{time.time_ns()}",
        }
        self.client.publish(f"factory/FACTORY-001/device/{self.code}/raw", payload, qos=1)
        with self._lock:
            self.last_data = utc_now()

    def stop(self, reason="STOPPED"):
        with self._lock:
            self.enabled = False
            self.status = "OFFLINE"
            self.stop_reason = reason
            self._stop.set()


class DeviceManager:
    def __init__(self, state_path=None):
        self.devices = {}
        self.threads = {}
        self.lock = threading.RLock()
        self._registry_client = None
        default_path = Path(__file__).resolve().parents[2] / "configs" / "runtime-devices.json"
        self.state_path = Path(state_path or default_path)

    def _persist_locked(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        records = []
        for device in self.devices.values():
            snapshot = device.snapshot()
            records.append({
                "deviceCode": snapshot["deviceCode"],
                "name": snapshot["name"],
                "deviceType": snapshot["deviceType"],
                "scenario": snapshot["scenario"],
                "enabled": snapshot["enabled"],
                "intervalSec": snapshot["intervalSec"],
                "heartbeatIntervalSec": snapshot["heartbeatIntervalSec"],
                "points": snapshot["points"],
            })
        temp_path = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.state_path)

    def _publish_registry_locked(self):
        """Publish the authoritative simulator inventory as a retained MQTT message.

        Uses one long-lived control connection (reconnected on demand) instead
        of opening a fresh TCP connection for every inventory change and for
        the periodic refresh loop.
        """
        client = self._registry_client
        if client is None or not client.running:
            try:
                client = Client(f"simulator-registry-{os.getpid()}", keepalive=10)
                client.connect()
                client.start_keepalive()
            except OSError:
                self._registry_client = None
                return
            self._registry_client = client
        try:
            client.publish("factory/FACTORY-001/device/registry",
                           {"source": "mqtt-simulator", "deviceCodes": sorted(self.devices),
                            "updatedAt": utc_now()},
                           qos=1, retain=True)
        except OSError:
            self._registry_client = None

    def load_or_seed(self, scenario="normal"):
        with self.lock:
            if self.state_path.exists():
                try:
                    records = json.loads(self.state_path.read_text(encoding="utf-8"))
                    if not isinstance(records, list):
                        raise ValueError("runtime device state must be a list")
                    for record in records:
                        self._add_locked(record)
                    self._publish_registry_locked()
                    return
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    print(f"[sim-devices] invalid state file, rebuilding defaults: {exc}", flush=True)
            for code, device_type in (("PLC-001", "PLC"), ("TEMP-001", "TEMPERATURE"), ("PRESS-001", "PRESSURE")):
                self._add_locked({"deviceCode": code, "deviceType": device_type, "scenario": scenario, "name": code})
            self._persist_locked()
            self._publish_registry_locked()

    def _add_locked(self, payload):
        code = str(payload.get("deviceCode", "")).strip().upper()
        if not code or code in self.devices:
            raise ValueError("deviceCode is required and must be unique")
        device_type = str(payload.get("deviceType", "PLC")).upper()
        if device_type not in DEVICE_PROFILES:
            raise ValueError("unsupported deviceType")
        device = SimDevice(code, device_type, payload.get("points"), payload.get("scenario", "normal"),
                           payload.get("name"), payload.get("intervalSec", 1), payload.get("heartbeatIntervalSec", 10),
                           payload.get("enabled", True))
        self.devices[code] = device
        if device.enabled:
            self._start_locked(device)
        return device

    def add(self, payload):
        with self.lock:
            device = self._add_locked(payload)
            self._persist_locked()
            self._publish_registry_locked()
        return device.snapshot()

    def seed(self, scenario="normal", codes=None):
        with self.lock:
            for code, device_type in (("PLC-001", "PLC"), ("TEMP-001", "TEMPERATURE"), ("PRESS-001", "PRESSURE")):
                if codes and code not in codes:
                    continue
                self._add_locked({"deviceCode": code, "deviceType": device_type, "scenario": scenario, "name": code})
            self._persist_locked()

    def _start_locked(self, device):
        previous = self.threads.get(device.code)
        if previous and previous.is_alive():
            if not device._stop.is_set():
                return
            # A stop request is asynchronous. Wait briefly before replacing the
            # worker so a rapid stop/start cannot leave an enabled device idle.
            previous.join(timeout=2)
            if previous.is_alive():
                raise RuntimeError("device worker is still stopping")
        device.enabled = True
        device._stop.clear()
        thread = threading.Thread(target=device.run, name=f"sim-{device.code}", daemon=True)
        self.threads[device.code] = thread
        thread.start()

    def update(self, code, payload):
        with self.lock:
            device = self.devices.get(code)
            if not device:
                raise KeyError(code)
            device.update(payload)
            if "enabled" in payload:
                if bool(payload["enabled"]):
                    self._start_locked(device)
                else:
                    device.stop("DISABLED")
                    worker = self.threads.get(code)
                    if worker:
                        worker.join(timeout=2)
            self._persist_locked()
            self._publish_registry_locked()
            return device.snapshot()

    def toggle(self, code):
        with self.lock:
            device = self.devices.get(code)
            if not device:
                raise KeyError(code)
            return self.update(code, {"enabled": not device.enabled})

    def remove(self, code):
        with self.lock:
            device = self.devices.pop(code, None)
            if not device:
                raise KeyError(code)
            device.stop("REMOVED")
            worker = self.threads.get(code)
            if worker:
                worker.join(timeout=2)
            # The worker may already have lost its MQTT socket while stopping.
            # Send a retained removal event on a short-lived control connection
            # so the backend can remove the device from its active inventory.
            try:
                control = Client(f"sim-remove-{code.lower()}", keepalive=10)
                control.connect()
                control.publish(f"factory/FACTORY-001/device/{code}/status",
                                {"deviceCode": code, "status": "OFFLINE", "reason": "REMOVED", "timestamp": utc_now()},
                                qos=1, retain=True)
                time.sleep(.1)
                control.disconnect()
            except OSError:
                pass
            self._persist_locked()
            self._publish_registry_locked()
            return {"deviceCode": code, "removed": True}

    def list(self):
        with self.lock:
            return [device.snapshot() for device in self.devices.values()]


def json_response(handler, payload, status=200):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.end_headers()
    handler.wfile.write(raw)


def make_handler(manager):
    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_OPTIONS(self):
            json_response(self, {}, 204)

        def body(self):
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            if path in ("", "/api/devices"):
                return json_response(self, manager.list())
            parts = path.split("/")
            if len(parts) == 4 and parts[:3] == ["", "api", "devices"]:
                device = next((item for item in manager.list() if item["deviceCode"] == parts[3].upper()), None)
                return json_response(self, device or {"error": "device not found"}, 200 if device else 404)
            if path == "/health":
                return json_response(self, {"status": "UP", "deviceCount": len(manager.devices)})
            return json_response(self, {"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            try:
                if path == "/api/devices":
                    return json_response(self, manager.add(self.body()), 201)
                parts = path.split("/")
                if len(parts) == 5 and parts[:3] == ["", "api", "devices"] and parts[4] in ("toggle", "start", "stop"):
                    code = parts[3]
                    if parts[4] == "toggle":
                        return json_response(self, manager.toggle(code))
                    return json_response(self, manager.update(code, {"enabled": parts[4] != "stop"}))
            except (ValueError, KeyError, TypeError) as exc:
                return json_response(self, {"error": str(exc)}, 400 if isinstance(exc, ValueError) else 404)
            return json_response(self, {"error": "not found"}, 404)

        def do_PUT(self):
            path = urlparse(self.path).path.rstrip("/")
            parts = path.split("/")
            if len(parts) != 4 or parts[:3] != ["", "api", "devices"]:
                return json_response(self, {"error": "not found"}, 404)
            try:
                return json_response(self, manager.update(parts[3], self.body()))
            except (ValueError, TypeError) as exc:
                return json_response(self, {"error": str(exc)}, 400)
            except KeyError:
                return json_response(self, {"error": "device not found"}, 404)

        def do_DELETE(self):
            path = urlparse(self.path).path.rstrip("/")
            parts = path.split("/")
            if len(parts) != 4 or parts[:3] != ["", "api", "devices"]:
                return json_response(self, {"error": "not found"}, 404)
            try:
                return json_response(self, manager.remove(parts[3]))
            except KeyError:
                return json_response(self, {"error": "device not found"}, 404)

    return ControlHandler


def main():
    parser = argparse.ArgumentParser(description="Controllable industrial device simulator")
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--device", default="PLC-001")
    parser.add_argument("--all", action="store_true", help="seed the three default simulators")
    parser.add_argument("--control-port", type=int, default=8091)
    args = parser.parse_args()
    manager = DeviceManager()
    if args.all:
        manager.load_or_seed(args.scenario)
    else:
        manager.add({"deviceCode": args.device, "deviceType": LEGACY_DEVICE_TYPES.get(args.device, "PLC"), "scenario": args.scenario})
    # Retained messages live in memory in the bundled broker.  Republish the
    # authoritative inventory periodically so a backend (or broker) restarted
    # independently converges to the durable runtime-devices.json state.
    def registry_loop():
        while True:
            time.sleep(5)
            with manager.lock:
                manager._publish_registry_locked()
    threading.Thread(target=registry_loop, name="simulator-registry-refresh", daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.control_port), make_handler(manager))
    threading.Thread(target=server.serve_forever, name="device-control-api", daemon=True).start()
    print(f"[sim-devices] control API http://127.0.0.1:{args.control_port}/api/devices", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown()
        for device in manager.list():
            manager.devices[device["deviceCode"]].stop()


if __name__ == "__main__":
    main()

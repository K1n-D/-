import http.client
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("IOT_DB_ENABLED", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from iot_middleware.normalization import normalize


class FakeDB:
    """In-memory stand-in so HTTP tests exercise routing and state, not MySQL."""

    def __init__(self):
        self.points = [
            {"deviceCode": "MODBUS-PLC-001", "pointCode": "temperature", "slaveId": 1,
             "register": 0, "registerType": "holding", "scaleFactor": 0.1, "deadZone": 0.1,
             "collectIntervalMs": 1000, "unit": "C", "enabled": True},
        ]
        self.rules = [
            {"id": 1, "deviceCode": "*", "pointCode": "temperature", "operator": ">",
             "threshold": 90.0, "durationSec": 0, "hysteresis": 5.0,
             "level": "SERIOUS", "enabled": True},
        ]
        self.next_rule_id = 2
        self.fail = False

    def point_configs(self):
        return None if self.fail else [dict(p) for p in self.points]

    def update_point(self, device, point, fields):
        if self.fail:
            return False
        for row in self.points:
            if row["deviceCode"] == device and row["pointCode"] == point:
                row.update(fields)
                return True
        return False

    def alarm_rules(self):
        return None if self.fail else [dict(r) for r in self.rules]

    def alarm_rule_rows(self):
        if self.fail:
            return None
        return [{"id": r["id"], "device_code": r["deviceCode"], "point_code": r["pointCode"],
                 "operator": r["operator"], "threshold": r["threshold"],
                 "duration_sec": r["durationSec"], "hysteresis": r["hysteresis"],
                 "level": r["level"], "enabled": r["enabled"]} for r in self.rules]

    def alarm_rule_insert(self, device, point, operator, threshold, duration, hysteresis, level):
        if self.fail:
            return False
        self.rules.append({"id": self.next_rule_id, "deviceCode": device, "pointCode": point,
                           "operator": operator, "threshold": threshold, "durationSec": duration,
                           "hysteresis": hysteresis, "level": level, "enabled": True})
        self.next_rule_id += 1
        return True

    def alarm_rule_update(self, rule_id, fields):
        if self.fail:
            return False
        for row in self.rules:
            if row["id"] == rule_id:
                row.update(fields)
                return True
        return False

    def alarm_rule_delete(self, rule_id):
        if self.fail:
            return False
        before = len(self.rules)
        self.rules = [r for r in self.rules if r["id"] != rule_id]
        return len(self.rules) < before

    def alarm_triggered(self, *args):
        return True

    def alarm_resolved(self, *args):
        return True

    def alarm_acked(self, *args):
        return True

    def telemetry(self, payload):
        return True

    def heartbeat(self, payload):
        return True

    def status(self, payload, old_status=None):
        return True

    def execute(self, *args, **kwargs):
        return not self.fail

    def query(self, *args, **kwargs):
        return None

    def history(self, *args, **kwargs):
        return None


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_db = server.DB
        cls.http_db = FakeDB()
        server.DB = cls.http_db
        cls._start_server()

    @classmethod
    def _start_server(cls):
        from http.server import ThreadingHTTPServer
        server_instance = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = server_instance.server_address[1]
        threading.Thread(target=server_instance.serve_forever, daemon=True).start()
        cls.http_server = server_instance

    @classmethod
    def tearDownClass(cls):
        cls.http_server.shutdown()
        server.DB = cls.original_db

    def setUp(self):
        server.DATA["devices"].clear()
        server.DATA["telemetry"].clear()
        server.DATA["alarms"].clear()
        server.DATA["stats"]["received"] = 0
        server.ACTIVE_DEVICE_CODES_BY_SOURCE = {}
        server.RULE_ENGINE["rules"] = []
        server.RULE_ENGINE["state"] = {}
        server.RULE_ENGINE["loadedAt"] = time.time()
        self.http_db.fail = False
        self.conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def tearDown(self):
        self.conn.close()

    def request(self, method, path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        self.conn.request(method, path, body=payload, headers=headers)
        response = self.conn.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw) if raw else None)

    def feed(self, code, point, value, event):
        server.process_message("factory/F1/telemetry/normalized",
                               {"deviceCode": code, "pointCode": point, "value": value,
                                "timestamp": "2026-09-12T10:00:00Z", "eventId": event})

    def test_unknown_post_returns_404(self):
        status, _ = self.request("POST", "/api/nothing")
        self.assertEqual(status, 404)

    def test_rule_crud_roundtrip(self):
        status, body = self.request("POST", "/api/alarm-rules",
                                    {"deviceCode": "D-X", "pointCode": "vibration",
                                     "operator": ">", "threshold": 5, "durationSec": 3,
                                     "hysteresis": 1, "level": "WARNING"})
        self.assertEqual((status, body["ok"]), (201, True))
        status, rules = self.request("GET", "/api/alarm-rules")
        self.assertEqual(status, 200)
        rule = next(r for r in rules if r["deviceCode"] == "D-X")
        self.assertEqual(rule["threshold"], 5.0)

        status, body = self.request("PUT", f"/api/alarm-rules/{rule['id']}", {"threshold": 6.5})
        self.assertEqual((status, body["ok"]), (200, True))
        _, rules = self.request("GET", "/api/alarm-rules")
        self.assertEqual(next(r for r in rules if r["id"] == rule["id"])["threshold"], 6.5)

        status, body = self.request("DELETE", f"/api/alarm-rules/{rule['id']}")
        self.assertEqual((status, body["ok"]), (200, True))
        _, rules = self.request("GET", "/api/alarm-rules")
        self.assertFalse(any(r["id"] == rule["id"] for r in rules))

    def test_point_update_roundtrip(self):
        status, body = self.request("PUT", "/api/points/MODBUS-PLC-001/temperature",
                                    {"scaleFactor": 0.2, "enabled": False})
        self.assertEqual((status, body["ok"]), (200, True))
        status, points = self.request("GET", "/api/points")
        row = points[0]
        self.assertEqual(row["scaleFactor"], 0.2)
        self.assertFalse(row["enabled"])

    def test_ack_marks_active_alarm(self):
        server.RULE_ENGINE["rules"] = [{"id": 1, "device_code": "*", "point_code": "temperature",
                                        "operator": ">", "threshold": 90.0, "duration_sec": 0,
                                        "hysteresis": 5.0, "level": "SERIOUS", "enabled": True}]
        server.RULE_ENGINE["loadedAt"] = time.time()
        self.feed("TEMP-9", "temperature", 95, "hot")
        status, body = self.request("POST", "/api/alarms/ack",
                                    {"deviceCode": "TEMP-9", "pointCode": "temperature"})
        self.assertEqual((status, body["acked"]), (200, 1))
        alarm = server.DATA["alarms"][0]
        self.assertTrue(alarm["ack"])

    def test_history_falls_back_to_memory_buffer(self):
        self.feed("H-1", "temperature", 71.5, "h1")
        self.feed("H-2", "temperature", 72.5, "h2")
        self.conn.request("GET", "/api/history?device=H-2")
        response = self.conn.getresponse()
        rows = json.loads(response.read())
        self.assertEqual([row["eventId"] for row in rows], ["h2"])

    def test_sse_stream_delivers_telemetry(self):
        stream = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        stream.request("GET", "/api/stream")
        response = stream.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/event-stream"))
        threading.Thread(target=lambda: (time.sleep(0.4),
                                         self.feed("SSE-D", "temperature", 88.0, "s1")),
                         daemon=True).start()
        response.fp.raw._sock.settimeout(5)
        frame = response.fp.readline()  # "data: {...}\n\n" first line
        self.assertTrue(frame.startswith(b"data: "))
        payload = json.loads(frame[6:])
        self.assertEqual(payload["deviceCode"], "SSE-D")
        stream.close()

    def test_login_rejects_bad_credentials(self):
        status, _ = self.request("POST", "/api/auth/login",
                                 {"username": "admin", "password": "wrong"})
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()

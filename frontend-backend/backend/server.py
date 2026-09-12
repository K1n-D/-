from __future__ import annotations
import json, logging, os, sys, threading, time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "middleware" / "src"))
from iot_middleware.local_mqtt import Client

try:
    import mysql.connector
except ImportError:  # The demo still runs in memory when the optional driver is absent.
    mysql = None
else:
    mysql = mysql.connector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).resolve().parents[2]

# Alarm thresholds are configuration, not code.
ALARM_TEMPERATURE_THRESHOLD = float(os.getenv("IOT_ALARM_TEMPERATURE_THRESHOLD", "90"))
MAX_ALARMS = int(os.getenv("IOT_MAX_ALARMS", "200"))

# Set IOT_AUTH_ENABLED=1 to require a Bearer token on data APIs. Off by default
# so the local demo works out of the box; the login flow is unchanged.
AUTH_ENABLED = os.getenv("IOT_AUTH_ENABLED", "0").lower() in {"1", "true", "yes"}
AUTH_TOKEN = os.getenv("IOT_AUTH_TOKEN", "local-demo-token")
ADMIN_USER = os.getenv("IOT_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("IOT_ADMIN_PASSWORD", "admin123")

# The browser pages live on 5173; anything else is not entitled to read the API
# cross-origin. Override with IOT_CORS_ORIGINS if the demo is served elsewhere.
ALLOWED_ORIGINS = [o.strip() for o in os.getenv(
    "IOT_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(",") if o.strip()]


def utcnow_naive():
    """Naive UTC timestamp for MySQL TIMESTAMP columns (utcnow() is deprecated)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return utcnow_naive()


class Persistence:
    """Small MySQL writer with reconnect and an in-memory fallback."""

    def __init__(self):
        self.enabled = os.getenv("IOT_DB_ENABLED", "1").lower() not in {"0", "false", "no"}
        self.conn = None
        self.lock = threading.RLock()
        self.last_error = None

    def connect(self):
        if not self.enabled or mysql is None:
            if mysql is None:
                self.last_error = "mysql-connector-python is not installed"
            return False
        with self.lock:
            try:
                self.conn = mysql.connect(
                    host=os.getenv("IOT_DB_HOST", "127.0.0.1"),
                    port=int(os.getenv("IOT_DB_PORT", "3306")),
                    user=os.getenv("IOT_DB_USER", "root"),
                    # Keep the local Windows demo usable after the root password
                    # change while still allowing deployments to override it.
                    password=os.getenv("IOT_DB_PASSWORD", "root1234"),
                    database=os.getenv("IOT_DB_NAME", "iot_monitor"),
                    autocommit=True,
                )
                self._ensure_schema()
                self.last_error = None
                logging.info("database connected: %s:%s/%s", os.getenv("IOT_DB_HOST", "127.0.0.1"), os.getenv("IOT_DB_PORT", "3306"), os.getenv("IOT_DB_NAME", "iot_monitor"))
                return True
            except Exception as exc:
                self.conn = None
                self.last_error = str(exc)
                logging.warning("database unavailable, using memory fallback: %s", exc)
                return False

    def _ensure_schema(self):
        schema = ROOT / "backend" / "src" / "main" / "resources" / "db" / "schema.sql"
        if not schema.exists():
            schema = Path(__file__).resolve().parent / "src" / "main" / "resources" / "db" / "schema.sql"
        statements = [part.strip() for part in schema.read_text(encoding="utf-8").split(";") if part.strip()]
        cur = self.conn.cursor()
        try:
            for statement in statements:
                cur.execute(statement)
        finally:
            cur.close()

    def execute(self, sql, params=()):
        with self.lock:
            if self.conn is None:
                if not self.connect():
                    return False
            try:
                if not self.conn.is_connected():
                    self.connect()
                if self.conn is None:
                    return False
                cur = self.conn.cursor()
                try:
                    cur.execute(sql, params)
                finally:
                    cur.close()
                return True
            except Exception as exc:
                self.last_error = str(exc)
                logging.warning("database write failed: %s", exc)
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                return False

    def query(self, sql, params=()):
        """Run a SELECT; returns rows or None when the database is down."""
        with self.lock:
            if self.conn is None and not self.connect():
                return None
            try:
                cur = self.conn.cursor()
                try:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                finally:
                    cur.close()
                return rows
            except Exception as exc:
                self.last_error = str(exc)
                logging.warning("query failed: %s", exc)
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                return None

    def point_configs(self):
        rows = self.query(
            """SELECT device_code, point_code, slave_id, register, register_type,
                      scale_factor, dead_zone, collect_interval_ms, unit, enabled
               FROM iot_point_config ORDER BY device_code, point_code""")
        if rows is None:
            return None
        return [{"deviceCode": d, "pointCode": p, "slaveId": s, "register": r,
                 "registerType": t, "scaleFactor": float(sf), "deadZone": float(dz),
                 "collectIntervalMs": ci, "unit": u, "enabled": bool(en)}
                for d, p, s, r, t, sf, dz, ci, u, en in rows]

    POINT_EDITABLE = {"scale_factor": "scaleFactor", "dead_zone": "deadZone",
                      "collect_interval_ms": "collectIntervalMs", "unit": "unit",
                      "enabled": "enabled"}

    def update_point(self, device_code, point_code, fields):
        """Apply a partial update to one point-table row (whitelisted columns)."""
        sets, params = [], []
        for column, key in self.POINT_EDITABLE.items():
            if key not in fields:
                continue
            value = fields[key]
            if column == "unit":
                value = str(value)[:32]
            elif column == "enabled":
                value = 1 if value else 0
            elif column == "collect_interval_ms":
                value = max(200, int(value))
            else:
                value = float(value)
            sets.append(f"{column}=%s")
            params.append(value)
        if not sets:
            return False
        params += [device_code, point_code]
        return self.execute(
            f"UPDATE iot_point_config SET {', '.join(sets)} WHERE device_code=%s AND point_code=%s",
            tuple(params))

    def alarm_rules(self):
        rows = self.query(
            """SELECT device_code, point_code, operator, threshold, duration_sec,
                      hysteresis, level, enabled FROM iot_alarm_rule ORDER BY id""")
        if rows is None:
            return None
        return [{"deviceCode": d, "pointCode": p, "operator": op, "threshold": float(th),
                 "durationSec": du, "hysteresis": float(hy), "level": lv, "enabled": bool(en)}
                for d, p, op, th, du, hy, lv, en in rows]

    def telemetry(self, payload):
        value = payload.get("value")
        numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
        self.execute(
            """INSERT IGNORE INTO iot_history_data
               (event_id, device_code, point_code, value_decimal, value_text, unit,
                quality, source_protocol, collect_time)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (payload.get("eventId"), payload.get("deviceCode"), payload.get("pointCode"),
             value if numeric else None, None if numeric else str(value), payload.get("unit", ""),
             payload.get("quality", "GOOD"), payload.get("sourceProtocol", "MQTT"),
             as_datetime(payload.get("timestamp"))),
        )
        self.execute(
            """INSERT INTO iot_device (device_code, device_name, protocol, status, created_at, updated_at)
               VALUES (%s,%s,%s,'ONLINE',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
               ON DUPLICATE KEY UPDATE status='ONLINE', updated_at=CURRENT_TIMESTAMP""",
            (payload.get("deviceCode"), payload.get("deviceCode"), payload.get("sourceProtocol", "MQTT")),
        )

    def history(self, limit=500):
        """Latest stored telemetry, or None when the database is unavailable so
        callers can fall back to the in-memory buffer."""
        with self.lock:
            if self.conn is None and not self.connect():
                return None
            try:
                cur = self.conn.cursor()
                try:
                    cur.execute(
                        """SELECT event_id, device_code, point_code, value_decimal, value_text,
                                  unit, quality, source_protocol, collect_time
                           FROM iot_history_data ORDER BY id DESC LIMIT %s""", (limit,))
                    rows = cur.fetchall()
                finally:
                    cur.close()
            except Exception as exc:
                self.last_error = str(exc)
                logging.warning("history query failed: %s", exc)
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
                return None
            result = []
            for event_id, device_code, point_code, value_decimal, value_text, unit, quality, protocol, collect_time in rows:
                result.append({
                    "eventId": event_id,
                    "deviceCode": device_code,
                    "pointCode": point_code,
                    "value": float(value_decimal) if value_decimal is not None else value_text,
                    "unit": unit or "",
                    "quality": quality,
                    "sourceProtocol": protocol,
                    "timestamp": collect_time.strftime("%Y-%m-%dT%H:%M:%SZ") if collect_time else None,
                })
            return result

    def alarm_triggered(self, device_code, value, threshold):
        self.execute(
            """INSERT INTO iot_alarm_record
               (device_code, point_code, alarm_type, alarm_level, trigger_value,
                threshold_value, status, trigger_time)
               VALUES (%s,'temperature','THRESHOLD','SERIOUS',%s,%s,'ACTIVE',%s)""",
            (device_code, value, threshold, utcnow_naive()),
        )

    def alarm_resolved(self, device_code):
        self.execute(
            """UPDATE iot_alarm_record SET status='RESOLVED', recover_time=%s
               WHERE device_code=%s AND point_code='temperature' AND status='ACTIVE'""",
            (utcnow_naive(), device_code),
        )

    def heartbeat(self, payload):
        self.execute(
            """INSERT INTO iot_heartbeat_log
               (device_code, client_id, sequence_no, sent_time, received_time, latency_ms, status, payload)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (payload.get("deviceCode"), payload.get("clientId"), payload.get("sequenceNo"),
             as_datetime(payload.get("sentTime")), as_datetime(payload.get("receivedTime")) or utcnow_naive(),
             payload.get("latencyMs"), payload.get("status", "ONLINE"), json.dumps(payload, ensure_ascii=False)),
        )
        self.execute(
            """INSERT INTO iot_device (device_code, device_name, protocol, status, last_heartbeat, created_at, updated_at)
               VALUES (%s,%s,'MQTT','ONLINE',%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
               ON DUPLICATE KEY UPDATE status='ONLINE', last_heartbeat=VALUES(last_heartbeat), updated_at=CURRENT_TIMESTAMP""",
            (payload.get("deviceCode"), payload.get("deviceCode"), as_datetime(payload.get("receivedTime")) or utcnow_naive()),
        )

    def status(self, payload, old_status=None):
        self.execute(
            """INSERT INTO iot_device_status_log
               (device_code, old_status, new_status, reason, event_time)
               VALUES (%s,%s,%s,%s,%s)""",
            (payload.get("deviceCode"), old_status, payload.get("status", "UNKNOWN"), payload.get("reason"),
             as_datetime(payload.get("timestamp")) or utcnow_naive()),
        )
        # A removal is a terminal inventory operation.  Do not follow the
        # audit-log insert with an UPSERT, otherwise a deleted device would be
        # recreated in iot_device as OFFLINE.
        if payload.get("reason") == "REMOVED":
            return
        self.execute(
            """INSERT INTO iot_device (device_code, device_name, protocol, status, created_at, updated_at)
               VALUES (%s,%s,'MQTT',%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
               ON DUPLICATE KEY UPDATE status=VALUES(status), updated_at=CURRENT_TIMESTAMP""",
            (payload.get("deviceCode"), payload.get("deviceCode"), payload.get("status", "UNKNOWN")),
        )


DB = Persistence()
DB.connect()

DATA = {"devices": {}, "telemetry": deque(maxlen=500), "alarms": deque(maxlen=MAX_ALARMS),
        "stats": {"received": 0}, "startedAt": time.time()}
LOCK = threading.RLock()

# Devices register themselves through retained registry messages published on
# factory/FACTORY-001/device/registry.  Every access path (the MQTT-direct
# simulator, a Modbus edge collector, later a real gateway) announces its
# devices with a distinct "source", so the backend accepts telemetry exactly
# for the union of registered devices and rejects stale retained statuses of
# devices that were deleted meanwhile.
# ``{}`` means no registry has been received yet; in that window messages from
# unknown devices are still accepted so a restarted backend is not blocked.
ACTIVE_DEVICE_CODES_BY_SOURCE: dict = {}


def all_active_device_codes():
    codes = set()
    for source_codes in ACTIVE_DEVICE_CODES_BY_SOURCE.values():
        codes |= source_codes
    return codes


def load_persisted_registry():
    """Seed the simulator's durable inventory before MQTT retained delivery.

    The lightweight local broker keeps retained messages in memory only.  If
    the broker is restarted while the simulator is still running, its
    retained registry is temporarily unavailable.  Loading the same durable
    runtime-devices.json file prevents stale retained status messages from
    repopulating deleted devices during that interval.
    """
    state_path = ROOT / "simulated-devices" / "configs" / "runtime-devices.json"
    try:
        records = json.loads(state_path.read_text(encoding="utf-8"))
        codes = {str(item.get("deviceCode", "")).strip().upper() for item in records
                 if isinstance(item, dict) and item.get("deviceCode")}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    if codes:
        ACTIVE_DEVICE_CODES_BY_SOURCE["mqtt-simulator"] = codes
        for code in codes:
            DATA["devices"].setdefault(code, {"deviceCode": code, "status": "OFFLINE", "points": {}})


def evaluate_temperature_alarm(code, value):
    """Open a SERIOUS alarm above the threshold and resolve it once the value
    falls back; alarms were previously write-only and never recovered."""
    active = [a for a in DATA["alarms"]
              if a["deviceCode"] == code and a["pointCode"] == "temperature" and a["status"] == "ACTIVE"]
    if value > ALARM_TEMPERATURE_THRESHOLD:
        if not active:
            DATA["alarms"].append({"deviceCode": code, "pointCode": "temperature", "level": "SERIOUS",
                                   "value": value, "threshold": ALARM_TEMPERATURE_THRESHOLD,
                                   "status": "ACTIVE", "time": time.time()})
            DB.alarm_triggered(code, value, ALARM_TEMPERATURE_THRESHOLD)
            logging.warning("alarm OPEN device=%s value=%s threshold=%s", code, value, ALARM_TEMPERATURE_THRESHOLD)
        elif value > active[0]["value"]:
            active[0]["value"] = value  # keep the worst reading while active
    else:
        for alarm in active:
            alarm["status"] = "RESOLVED"
            alarm["recoveredAt"] = time.time()
            DB.alarm_resolved(code)
            logging.info("alarm RESOLVED device=%s value=%s", code, value)


def process_message(topic, payload):
    """Apply one MQTT message to the in-memory state and the database.

    Raises on malformed input so tests can assert the failure; consume() wraps
    this in a catch-all so a single bad message can never kill the reader.
    """
    global ACTIVE_DEVICE_CODES_BY_SOURCE
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    with LOCK:
        if topic.endswith("/device/registry"):
            source = str(payload.get("source", "unknown"))
            active = set(payload.get("deviceCodes", [])) if isinstance(payload.get("deviceCodes"), list) else set()
            is_known_source = source in ACTIVE_DEVICE_CODES_BY_SOURCE
            ACTIVE_DEVICE_CODES_BY_SOURCE[source] = active
            if is_known_source:
                # Only prune against the union once every registry source has
                # been seen: pruning on the first arriving source would wipe
                # devices owned by sources whose registry has not landed yet.
                active_all = all_active_device_codes()
                for code in list(DATA["devices"]):
                    if code not in active_all:
                        DATA["devices"].pop(code, None)
                        DB.execute("DELETE FROM iot_device WHERE device_code=%s", (code,))
            for code in active:
                DATA["devices"].setdefault(code, {"deviceCode": code, "status": "OFFLINE", "points": {}})
            return
        if topic.endswith("/telemetry/normalized"):
            code = payload.get("deviceCode")
            if not code:
                raise ValueError("telemetry payload requires deviceCode")
            active_all = all_active_device_codes()
            if active_all and code not in active_all:
                logging.info("ignore telemetry for device outside registry: %s", code)
                return
            DATA["stats"]["received"] += 1
            DATA["telemetry"].append(payload)
            d = DATA["devices"].setdefault(code, {"deviceCode": code, "status": "ONLINE", "points": {}})
            d.setdefault("points", {})[payload["pointCode"]] = payload
            d["lastDataTime"] = payload["timestamp"]
            d["status"] = "ONLINE"
            DB.telemetry(payload)
            value = payload.get("value")
            if payload["pointCode"] == "temperature" and isinstance(value, (int, float)) and not isinstance(value, bool):
                evaluate_temperature_alarm(code, value)
            return
        if "/heartbeat" in topic:
            code = payload.get("deviceCode")
            if not code:
                raise ValueError("heartbeat payload requires deviceCode")
            active_all = all_active_device_codes()
            if active_all and code not in active_all:
                logging.info("ignore heartbeat for device outside registry: %s", code)
                return
            DATA["devices"].setdefault(code, {"deviceCode": code})["lastHeartbeat"] = payload.get("receivedTime")
            DB.heartbeat(payload)
            return
        if topic.endswith("/status") and payload.get("deviceCode"):
            code = payload["deviceCode"]
            # REMOVED is an explicit deletion event and must always be
            # honoured.  Other retained status messages from a deleted
            # device are ignored once the authoritative registry is
            # available, preventing stale devices from reappearing.
            active_all = all_active_device_codes()
            if payload.get("reason") != "REMOVED" and active_all and code not in active_all:
                logging.info("ignore status for device outside registry: %s", code)
                return
            device = DATA["devices"].setdefault(code, {"deviceCode": code})
            old_status = device.get("status")
            if payload.get("reason") == "REMOVED":
                DATA["devices"].pop(code, None)
                for source_codes in ACTIVE_DEVICE_CODES_BY_SOURCE.values():
                    source_codes.discard(code)
                DB.execute("DELETE FROM iot_device WHERE device_code=%s", (code,))
                DB.status(payload, old_status)
                return
            device["status"] = payload.get("status")
            DB.status(payload, old_status)
            return
        logging.debug("unhandled topic: %s", topic)


def consume():
    load_persisted_registry()

    def msg(topic, payload):
        try:
            process_message(topic, payload)
        except Exception:
            # A malformed message must only cost the message, never the
            # reader thread: losing it silently would freeze the dashboard
            # on stale data with no error anywhere.
            logging.exception("failed to process MQTT message topic=%s", topic)

    while True:
        try:
            c = Client("backend-001", keepalive=30, on_message=msg)
            c.connect()
            c.start_keepalive()
            for topic in ["factory/+/telemetry/normalized", "factory/+/device/+/heartbeat",
                          "factory/+/device/+/status", "factory/+/device/registry"]:
                c.subscribe(topic)
            while c.running:
                time.sleep(1)
        except Exception as exc:
            logging.warning("MQTT consumer reconnecting after: %s", exc)
            time.sleep(2)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def cors_origin(self):
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            return origin
        return ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else ""

    def send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", self.cors_origin())
        self.end_headers()
        self.wfile.write(raw)

    def require_auth(self):
        if not AUTH_ENABLED or self.headers.get("Authorization") == f"Bearer {AUTH_TOKEN}":
            return True
        self.send_json({"error": "unauthorized"}, 401)
        return False

    def do_OPTIONS(self):
        # Vite runs on port 5173 while the API runs on 8080. Browser JSON
        # requests therefore need a CORS preflight response before login.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.cors_origin())
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json({"status": "UP", "broker": "local-mqtt",
                                   "uptimeSeconds": int(time.time() - DATA["startedAt"])})
        if not self.require_auth():
            return
        with LOCK:
            if path == "/api/devices":
                return self.send_json(list(DATA["devices"].values()))
            if path == "/api/dashboard/statistics":
                return self.send_json({"deviceTotal": len(DATA["devices"]),
                                       "online": sum(d.get("status") == "ONLINE" for d in DATA["devices"].values()),
                                       "offline": sum(d.get("status") == "OFFLINE" for d in DATA["devices"].values()),
                                       "messages": DATA["stats"]["received"],
                                       "alarms": len([a for a in DATA["alarms"] if a["status"] == "ACTIVE"])})
            if path == "/api/history":
                stored = DB.history(limit=500)
                if stored is not None:
                    return self.send_json(stored)
                # Database unavailable: serve the in-memory ring buffer instead
                # of failing, mirroring the write path's fallback behaviour.
                return self.send_json(list(DATA["telemetry"])[-100:])
            if path == "/api/alarms":
                return self.send_json(list(DATA["alarms"]))
            if path == "/api/points":
                return self.send_json(DB.point_configs() or [])
            if path == "/api/alarm-rules":
                return self.send_json(DB.alarm_rules() or [])
        return self.send_json({"error": "not found"}, 404)

    def do_PUT(self):
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "points"]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                fields = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "invalid request"}, 400)
            if not isinstance(fields, dict):
                return self.send_json({"error": "invalid request"}, 400)
            ok = DB.update_point(parts[2].upper(), parts[3], fields)
            if ok:
                return self.send_json({"ok": True})
            return self.send_json({"error": "point not found or database unavailable"}, 404)
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/auth/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error": "invalid request"}, 400)
            if payload.get("username") != ADMIN_USER or payload.get("password") != ADMIN_PASSWORD:
                return self.send_json({"error": "invalid credentials"}, 401)
            return self.send_json({"token": AUTH_TOKEN, "user": {"username": ADMIN_USER, "role": "ADMIN"}})
        return self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    threading.Thread(target=consume, daemon=True).start()
    print("[backend] http://127.0.0.1:8080", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()

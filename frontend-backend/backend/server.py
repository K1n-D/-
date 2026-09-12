from __future__ import annotations
import json, logging, os, sys, threading, time
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def as_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return datetime.utcnow()


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

    def heartbeat(self, payload):
        self.execute(
            """INSERT INTO iot_heartbeat_log
               (device_code, client_id, sequence_no, sent_time, received_time, latency_ms, status, payload)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (payload.get("deviceCode"), payload.get("clientId"), payload.get("sequenceNo"),
             as_datetime(payload.get("sentTime")), as_datetime(payload.get("receivedTime")) or datetime.utcnow(),
             payload.get("latencyMs"), payload.get("status", "ONLINE"), json.dumps(payload, ensure_ascii=False)),
        )
        self.execute(
            """INSERT INTO iot_device (device_code, device_name, protocol, status, last_heartbeat, created_at, updated_at)
               VALUES (%s,%s,'MQTT','ONLINE',%s,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
               ON DUPLICATE KEY UPDATE status='ONLINE', last_heartbeat=VALUES(last_heartbeat), updated_at=CURRENT_TIMESTAMP""",
            (payload.get("deviceCode"), payload.get("deviceCode"), as_datetime(payload.get("receivedTime")) or datetime.utcnow()),
        )

    def status(self, payload, old_status=None):
        self.execute(
            """INSERT INTO iot_device_status_log
               (device_code, old_status, new_status, reason, event_time)
               VALUES (%s,%s,%s,%s,%s)""",
            (payload.get("deviceCode"), old_status, payload.get("status", "UNKNOWN"), payload.get("reason"),
             as_datetime(payload.get("timestamp")) or datetime.utcnow()),
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

DATA={"devices":{},"telemetry":[],"alarms":[],"stats":{"received":0},"startedAt":time.time()}
LOCK=threading.RLock()

# The simulator publishes an authoritative retained inventory.  MQTT status
# messages are also retained, so a broker can replay an old OFFLINE status
# for a device that has since been deleted.  Keep the latest registry in
# memory and reject those stale messages after the registry has been seen.
# ``None`` means the backend has not received a registry yet (for example when
# it is connected before the simulator starts); in that short window normal
# MQTT messages are still accepted so real devices are not blocked.
ACTIVE_SIMULATOR_CODES = None

def load_persisted_simulator_registry():
    """Use the simulator's durable inventory before MQTT retained delivery.

    The lightweight local broker keeps retained messages in memory only.  If
    the broker is restarted while the simulator is still running, its
    retained registry is temporarily unavailable.  Loading the same durable
    runtime-devices.json file prevents stale retained status messages from
    repopulating deleted devices during that interval.
    """
    global ACTIVE_SIMULATOR_CODES
    state_path = ROOT / "simulated-devices" / "configs" / "runtime-devices.json"
    try:
        records = json.loads(state_path.read_text(encoding="utf-8"))
        codes = {str(item.get("deviceCode", "")).strip().upper() for item in records
                 if isinstance(item, dict) and item.get("deviceCode")}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    if not codes:
        return
    ACTIVE_SIMULATOR_CODES = codes
    for code in codes:
        DATA["devices"].setdefault(code, {"deviceCode": code, "status": "OFFLINE", "points": {}})

def consume():
    load_persisted_simulator_registry()
    def msg(topic,payload):
        global ACTIVE_SIMULATOR_CODES
        with LOCK:
            if topic.endswith("/simulator/registry"):
                active = set(payload.get("deviceCodes", [])) if isinstance(payload.get("deviceCodes"), list) else set()
                ACTIVE_SIMULATOR_CODES = active
                for code in list(DATA["devices"]):
                    if code not in active:
                        DATA["devices"].pop(code, None)
                        DB.execute("DELETE FROM iot_device WHERE device_code=%s", (code,))
                for code in active:
                    DATA["devices"].setdefault(code, {"deviceCode": code, "status": "OFFLINE", "points": {}})
                return
            if topic.endswith("/telemetry/normalized"):
                code = payload.get("deviceCode")
                if ACTIVE_SIMULATOR_CODES is not None and code not in ACTIVE_SIMULATOR_CODES:
                    logging.info("ignore telemetry for device outside simulator registry: %s", code)
                    return
                DATA["stats"]["received"]+=1; DATA["telemetry"].append(payload); DATA["telemetry"]=DATA["telemetry"][-500:]
                d=DATA["devices"].setdefault(payload["deviceCode"],{"deviceCode":payload["deviceCode"],"status":"ONLINE","points":{}}); d.setdefault("points",{})[payload["pointCode"]]=payload; d["lastDataTime"]=payload["timestamp"]; d["status"]="ONLINE"
                DB.telemetry(payload)
                val=payload["value"]
                if payload["pointCode"]=="temperature" and isinstance(val,(int,float)) and val>90: 
                    if not any(a["deviceCode"]==d["deviceCode"] and a["pointCode"]=="temperature" and a["status"]=="ACTIVE" for a in DATA["alarms"]): DATA["alarms"].append({"deviceCode":d["deviceCode"],"pointCode":"temperature","level":"SERIOUS","value":val,"status":"ACTIVE","time":time.time()})
            elif "/heartbeat" in topic:
                code = payload.get("deviceCode")
                if ACTIVE_SIMULATOR_CODES is not None and code not in ACTIVE_SIMULATOR_CODES:
                    logging.info("ignore heartbeat for device outside simulator registry: %s", code)
                    return
                DATA["devices"].setdefault(payload.get("deviceCode"),{"deviceCode":payload.get("deviceCode")})["lastHeartbeat"]=payload.get("receivedTime")
                DB.heartbeat(payload)
            elif topic.endswith("/status") and payload.get("deviceCode"):
                code = payload["deviceCode"]
                # REMOVED is an explicit deletion event and must always be
                # honoured.  Other retained status messages from a deleted
                # simulator are ignored once the authoritative registry is
                # available, preventing stale devices from reappearing.
                if payload.get("reason") != "REMOVED" and ACTIVE_SIMULATOR_CODES is not None and code not in ACTIVE_SIMULATOR_CODES:
                    logging.info("ignore status for device outside simulator registry: %s", code)
                    return
                device = DATA["devices"].setdefault(code,{"deviceCode":code})
                old_status = device.get("status")
                if payload.get("reason") == "REMOVED":
                    DATA["devices"].pop(code, None)
                    if ACTIVE_SIMULATOR_CODES is not None:
                        ACTIVE_SIMULATOR_CODES.discard(code)
                    DB.execute("DELETE FROM iot_device WHERE device_code=%s", (code,))
                    DB.status(payload, old_status)
                    return
                device["status"] = payload.get("status")
                DB.status(payload, old_status)
    while True:
        try:
            c=Client("backend-001",keepalive=30,on_message=msg); c.connect(); c.start_keepalive(); [c.subscribe(x) for x in ["factory/+/telemetry/normalized","factory/+/device/+/heartbeat","factory/+/device/+/status","factory/+/simulator/registry"]]
            while c.running: time.sleep(1)
        except OSError: time.sleep(2)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send_json(self,obj,status=200):
        raw=json.dumps(obj,ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.send_header("Access-Control-Allow-Origin","*"); self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self):
        # Vite runs on port 5173 while the API runs on 8080. Browser JSON
        # requests therefore need a CORS preflight response before login.
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
    def do_GET(self):
        with LOCK:
            if self.path=="/api/health": return self.send_json({"status":"UP","broker":"local-mqtt","uptimeSeconds":int(time.time()-DATA["startedAt"])})
            if self.path.startswith("/api/devices"): return self.send_json(list(DATA["devices"].values()))
            if self.path.startswith("/api/dashboard/statistics"): return self.send_json({"deviceTotal":len(DATA["devices"]),"online":sum(d.get("status")=="ONLINE" for d in DATA["devices"].values()),"offline":sum(d.get("status")=="OFFLINE" for d in DATA["devices"].values()),"messages":DATA["stats"]["received"],"alarms":len([a for a in DATA["alarms"] if a["status"]=="ACTIVE"])})
            if self.path.startswith("/api/history"): return self.send_json(DATA["telemetry"][-100:])
            if self.path.startswith("/api/alarms"): return self.send_json(DATA["alarms"])
            return self.send_json({"error":"not found"},404)
    def do_POST(self):
        if self.path=="/api/auth/login":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self.send_json({"error":"invalid request"}, 400)
            expected_user = os.getenv("IOT_ADMIN_USER", "admin")
            expected_password = os.getenv("IOT_ADMIN_PASSWORD", "admin123")
            if payload.get("username") != expected_user or payload.get("password") != expected_password:
                return self.send_json({"error":"invalid credentials"}, 401)
            return self.send_json({"token":"local-demo-token","user":{"username":expected_user,"role":"ADMIN"}})
        return self.send_json({"ok":True})

if __name__=="__main__":
    threading.Thread(target=consume,daemon=True).start(); print("[backend] http://127.0.0.1:8080",flush=True); ThreadingHTTPServer(("127.0.0.1",8080),Handler).serve_forever()

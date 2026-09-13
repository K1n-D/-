from __future__ import annotations
import hmac, json, logging, os, queue, secrets, sys, threading, time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
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

# Alarm rules live in iot_alarm_rule (see RULE_ENGINE below); no hardcoded
# thresholds here any more.
MAX_ALARMS = int(os.getenv("IOT_MAX_ALARMS", "200"))

# Authentication is on by default: every data API (and the SSE stream)
# requires a Bearer token issued by /api/auth/login. Tokens are random,
# expire after TOKEN_TTL_SECONDS and die with the process. Set
# IOT_AUTH_ENABLED=0 for a no-login local demo.
AUTH_ENABLED = os.getenv("IOT_AUTH_ENABLED", "1").lower() in {"1", "true", "yes"}
TOKEN_TTL_SECONDS = int(os.getenv("IOT_TOKEN_TTL_SECONDS", str(8 * 3600)))
ADMIN_USER = os.getenv("IOT_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("IOT_ADMIN_PASSWORD", "admin123")

# issued tokens: token -> expiry (monotonic wall clock)
ACTIVE_TOKENS: dict = {}
# naive per-IP login throttle: consecutive failures lock the source out
LOGIN_FAILURES: dict = {}
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 300.0


def issue_token():
    token = secrets.token_urlsafe(32)
    ACTIVE_TOKENS[token] = time.time() + TOKEN_TTL_SECONDS
    return token


def token_valid(token):
    expiry = ACTIVE_TOKENS.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        ACTIVE_TOKENS.pop(token, None)
        return False
    return True


def revoke_token(token):
    ACTIVE_TOKENS.pop(token, None)


def login_allowed(client_ip):
    entry = LOGIN_FAILURES.get(client_ip)
    if not entry:
        return True
    count, locked_until = entry
    if count < LOGIN_MAX_FAILURES:
        return True
    if time.time() >= locked_until:
        LOGIN_FAILURES.pop(client_ip, None)
        return True
    return False


def record_login_failure(client_ip):
    count, _ = LOGIN_FAILURES.get(client_ip, (0, 0.0))
    count += 1
    locked_until = time.time() + LOGIN_LOCKOUT_SECONDS if count >= LOGIN_MAX_FAILURES else 0.0
    LOGIN_FAILURES[client_ip] = (count, locked_until)
    return locked_until


def record_login_success(client_ip):
    LOGIN_FAILURES.pop(client_ip, None)

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

    def _ensure_column(self, cur, table, column, ddl):
        """CREATE TABLE IF NOT EXISTS cannot add columns to an existing table,
        so schema evolution columns are migrated explicitly."""
        cur.execute(
            """SELECT COUNT(*) FROM information_schema.COLUMNS
               WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s""",
            (table, column))
        if cur.fetchone()[0] == 0:
            logging.info("migrating %s: adding column %s", table, column)
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _ensure_schema(self):
        candidates = [
            Path(__file__).resolve().parent / "db" / "schema.sql",
            ROOT / "frontend-backend" / "backend" / "db" / "schema.sql",
            # Legacy location from the removed Java variant.
            Path(__file__).resolve().parent / "src" / "main" / "resources" / "db" / "schema.sql",
        ]
        schema = next((path for path in candidates if path.exists()), None)
        if schema is None:
            raise FileNotFoundError(f"schema.sql not found in any of: {candidates}")
        statements = [part.strip() for part in schema.read_text(encoding="utf-8").split(";") if part.strip()]
        cur = self.conn.cursor()
        try:
            for statement in statements:
                cur.execute(statement)
            self._ensure_column(cur, "iot_alarm_record", "ack_time", "TIMESTAMP NULL")
            self._ensure_column(cur, "iot_alarm_record", "ack_by", "VARCHAR(64) NULL")
            # Point-table columns borrowed from thingsboard-gateway's design:
            # multi-register values with configurable byte order.
            self._ensure_column(cur, "iot_point_config", "data_type", "VARCHAR(16) NOT NULL DEFAULT 'uint16'")
            self._ensure_column(cur, "iot_point_config", "byte_order", "VARCHAR(8) NOT NULL DEFAULT 'ABCD'")
            self._ensure_column(cur, "iot_point_config", "register_count", "INT NOT NULL DEFAULT 1")
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
                      data_type, byte_order, register_count, scale_factor, dead_zone,
                      collect_interval_ms, unit, enabled
               FROM iot_point_config ORDER BY device_code, point_code""")
        if rows is None:
            return None
        return [{"deviceCode": d, "pointCode": p, "slaveId": s, "register": r,
                 "registerType": t, "dataType": dt, "byteOrder": bo, "registerCount": rc,
                 "scaleFactor": float(sf), "deadZone": float(dz),
                 "collectIntervalMs": ci, "unit": u, "enabled": bool(en)}
                for d, p, s, r, t, dt, bo, rc, sf, dz, ci, u, en in rows]

    POINT_EDITABLE = {"scale_factor": "scaleFactor", "dead_zone": "deadZone",
                      "collect_interval_ms": "collectIntervalMs", "unit": "unit",
                      "enabled": "enabled", "data_type": "dataType",
                      "byte_order": "byteOrder"}

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
            elif column == "data_type":
                value = str(value)[:16]
                if value not in ("int16", "uint16", "int32", "float32"):
                    return False
                # the register span follows the data type automatically
                sets.append("register_count=%s")
                params.append(2 if value in ("int32", "float32") else 1)
            elif column == "byte_order":
                value = str(value)[:8]
                if value not in ("ABCD", "CDAB", "BADC", "DCBA"):
                    return False
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
            """SELECT id, device_code, point_code, operator, threshold, duration_sec,
                      hysteresis, level, enabled FROM iot_alarm_rule ORDER BY id""")
        if rows is None:
            return None
        return [{"id": r[0], "deviceCode": r[1], "pointCode": r[2], "operator": r[3], "threshold": float(r[4]),
                 "durationSec": r[5], "hysteresis": float(r[6]), "level": r[7], "enabled": bool(r[8])}
                for r in rows]

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

    def history(self, limit=500, device=None, point=None, since=None, until=None, interval=None):
        """Latest stored telemetry, or None when the database is unavailable so
        callers can fall back to the in-memory buffer.

        ``since``/``until`` narrow the time window (naive-UTC datetimes).
        ``interval`` (seconds) enables SQL downsampling: one averaged row per
        bucket instead of every raw reading.
        """
        with self.lock:
            if self.conn is None and not self.connect():
                return None
            try:
                conditions, params = [], []
                if device:
                    conditions.append("device_code=%s")
                    params.append(device)
                if point:
                    conditions.append("point_code=%s")
                    params.append(point)
                if since is not None:
                    conditions.append("collect_time>=%s")
                    params.append(since)
                if until is not None:
                    conditions.append("collect_time<=%s")
                    params.append(until)
                where = f" WHERE {' AND '.join(conditions)}" if conditions else ""

                if interval and interval > 0 and device and point:
                    sql = ("""SELECT device_code, point_code,
                                     FROM_UNIXTIME(AVG(UNIX_TIMESTAMP(collect_time))) AS bucket_time,
                                     AVG(value_decimal) AS avg_value,
                                     unit, quality, source_protocol
                              FROM iot_history_data""" + where +
                          """ GROUP BY device_code, point_code,
                                     FLOOR(UNIX_TIMESTAMP(collect_time) / %s)
                              ORDER BY bucket_time ASC LIMIT %s""")
                    params += [interval, limit]
                else:
                    sql = ("""SELECT event_id, device_code, point_code, value_decimal, value_text,
                                     unit, quality, source_protocol, collect_time
                              FROM iot_history_data""" + where + " ORDER BY id DESC LIMIT %s")
                    params.append(limit)
                cur = self.conn.cursor()
                try:
                    cur.execute(sql, tuple(params))
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
            for row in rows:
                if interval and interval > 0 and device and point:
                    device_code, point_code, bucket_time, avg_value, unit, quality, protocol = row
                    event_id, value_text, collect_time = None, None, bucket_time
                    value_decimal = avg_value
                else:
                    event_id, device_code, point_code, value_decimal, value_text, unit, quality, protocol, collect_time = row
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

    def alarm_triggered(self, device_code, point_code, value, threshold, level):
        self.execute(
            """INSERT INTO iot_alarm_record
               (device_code, point_code, alarm_type, alarm_level, trigger_value,
                threshold_value, status, trigger_time)
               VALUES (%s,%s,'THRESHOLD',%s,%s,%s,'ACTIVE',%s)""",
            (device_code, point_code, level, value, threshold, utcnow_naive()),
        )

    def alarm_resolved(self, device_code, point_code):
        self.execute(
            """UPDATE iot_alarm_record SET status='RESOLVED', recover_time=%s
               WHERE device_code=%s AND point_code=%s AND status='ACTIVE'""",
            (utcnow_naive(), device_code, point_code),
        )

    def alarm_acked(self, device_code, point_code):
        self.execute(
            """UPDATE iot_alarm_record SET ack_time=%s
               WHERE id = (SELECT id FROM (
                     SELECT id FROM iot_alarm_record
                     WHERE device_code=%s AND point_code=%s AND status='ACTIVE'
                     ORDER BY id DESC LIMIT 1) latest)""",
            (utcnow_naive(), device_code, point_code),
        )

    RULE_EDITABLE = {"operator": str, "threshold": float, "duration_sec": int,
                     "hysteresis": float, "level": str, "enabled": bool}

    def alarm_rule_insert(self, device_code, point_code, operator, threshold,
                          duration_sec, hysteresis, level):
        return self.execute(
            """INSERT INTO iot_alarm_rule
               (device_code, point_code, operator, threshold, duration_sec, hysteresis, level)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (device_code.upper(), point_code, operator, threshold, duration_sec, hysteresis, level))

    def alarm_rule_update(self, rule_id, fields):
        sets, params = [], []
        for column, caster in self.RULE_EDITABLE.items():
            key = {"duration_sec": "durationSec", "operator": "operator",
                   "threshold": "threshold", "hysteresis": "hysteresis",
                   "level": "level", "enabled": "enabled"}[column]
            if key not in fields:
                continue
            value = fields[key]
            if caster is str:
                value = str(value)[:20]
            elif caster is int:
                value = max(0, int(value))
            elif caster is float:
                value = float(value)
            else:
                value = 1 if value else 0
            sets.append(f"{column}=%s")
            params.append(value)
        if not sets:
            return False
        params.append(rule_id)
        return self.execute(f"UPDATE iot_alarm_rule SET {', '.join(sets)} WHERE id=%s", tuple(params))

    def alarm_rule_delete(self, rule_id):
        return self.execute("DELETE FROM iot_alarm_rule WHERE id=%s", (rule_id,))

    def alarm_rule_rows(self):
        """Raw rule rows for the engine (id + numeric fields), None if DB down."""
        rows = self.query(
            """SELECT id, device_code, point_code, operator, threshold,
                      duration_sec, hysteresis, level, enabled FROM iot_alarm_rule""")
        if rows is None:
            return None
        return [{"id": r[0], "device_code": r[1], "point_code": r[2], "operator": r[3],
                 "threshold": float(r[4]), "duration_sec": r[5], "hysteresis": float(r[6]),
                 "level": r[7], "enabled": bool(r[8])} for r in rows]

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

# Server-Sent Events: one queue per connected dashboard; process_message
# fans every telemetry reading out to them so the UI does not need to poll
# for real-time values.
SSE_CLIENTS: list = []
SSE_LOCK = threading.Lock()

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


# Rule-driven alarm engine. Rules come from iot_alarm_rule (device '*' is a
# wildcard); state tracks how long each rule/device has been in breach so a
# rule with duration_sec only fires after a sustained violation, and the
# hysteresis band prevents flapping around the threshold.
RULE_ENGINE = {"rules": [], "loadedAt": 0.0, "state": {}}
RULE_RELOAD_SECONDS = 30.0
OPERATORS = (">", ">=", "<", "<=")

# History retention: rows older than the configured number of days are purged
# hourly (0 disables). Keeps iot_history_data bounded in long-running demos.
HISTORY_RETENTION_DAYS = int(os.getenv("IOT_HISTORY_RETENTION_DAYS", "7"))
RETENTION_SWEEP_SECONDS = 3600.0


def retention_loop():
    while True:
        if HISTORY_RETENTION_DAYS > 0:
            cutoff_time = (datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)).replace(tzinfo=None)
            if DB.execute("DELETE FROM iot_history_data WHERE collect_time < %s", (cutoff_time,)):
                logging.info("history retention sweep done (kept %s days)", HISTORY_RETENTION_DAYS)
        time.sleep(RETENTION_SWEEP_SECONDS)


def load_rules(force=False):
    now = time.time()
    if not force and now - RULE_ENGINE["loadedAt"] < RULE_RELOAD_SECONDS:
        return
    rows = DB.alarm_rule_rows()
    if rows is not None:
        RULE_ENGINE["rules"] = rows
        RULE_ENGINE["loadedAt"] = now


def rule_matches(rule, code, point):
    return rule["point_code"] == point and rule["device_code"] in ("*", code)


def rule_violated(rule, value):
    if rule["operator"] == ">":
        return value > rule["threshold"]
    if rule["operator"] == ">=":
        return value >= rule["threshold"]
    if rule["operator"] == "<":
        return value < rule["threshold"]
    if rule["operator"] == "<=":
        return value <= rule["threshold"]
    return False


def evaluate_rules(code, point, value, now):
    """Run the alarm engine for one reading.

    Two phases: first the reading updates the per-rule/device breach state,
    then a scan over *all* states fires every rule whose duration gate has
    elapsed. The scan matters because a dead-zone-suppressed device may stop
    sending frames while still in breach — other devices' frames keep the
    engine ticking and the pending alarm still fires.
    """
    load_rules()
    rules_by_id = {rule["id"]: rule for rule in RULE_ENGINE["rules"]}
    for rule in RULE_ENGINE["rules"]:
        if not rule.get("enabled", True) or not rule_matches(rule, code, point):
            continue
        key = (rule["id"], code)
        state = RULE_ENGINE["state"].setdefault(key, {"since": None, "active": False})
        state["last_value"] = value
        violated = rule_violated(rule, value)
        if not state["active"]:
            if not violated:
                state["since"] = None
                continue
            if state["since"] is None:
                state["since"] = now
    _fire_due_alarms(now)
    _resolve_recovered(code, point, value, now)


def _fire_due_alarms(now):
    """Open every alarm whose breach has lasted at least duration_sec."""
    for (rule_id, code), state in RULE_ENGINE["state"].items():
        if state["active"] or state["since"] is None:
            continue
        rule = next((r for r in RULE_ENGINE["rules"] if r["id"] == rule_id), None)
        if rule is None or not rule.get("enabled", True):
            continue
        if now - state["since"] < rule["duration_sec"]:
            continue
        state["active"] = True
        DATA["alarms"].append({"deviceCode": code, "pointCode": rule["point_code"],
                               "level": rule["level"], "value": state.get("last_value"),
                               "threshold": rule["threshold"], "ruleId": rule_id,
                               "status": "ACTIVE", "ack": False, "time": now})
        DB.alarm_triggered(code, rule["point_code"], state.get("last_value"), rule["threshold"], rule["level"])
        logging.warning("alarm OPEN device=%s point=%s value=%s rule=%s threshold=%s",
                        code, rule["point_code"], state.get("last_value"), rule_id, rule["threshold"])


def _resolve_recovered(code, point, value, now):
    """Resolve active alarms of this device/point once the hysteresis band is crossed."""
    for rule in RULE_ENGINE["rules"]:
        if not rule_matches(rule, code, point) or not rule.get("enabled", True):
            continue
        state = RULE_ENGINE["state"].get((rule["id"], code))
        if not state or not state["active"]:
            continue
        if rule["operator"] in (">", ">="):
            recovered = value < rule["threshold"] - rule["hysteresis"]
        else:
            recovered = value > rule["threshold"] + rule["hysteresis"]
        if recovered:
            state["active"] = False
            state["since"] = None
            for alarm in DATA["alarms"]:
                if (alarm["deviceCode"] == code and alarm["pointCode"] == point
                        and alarm.get("ruleId") == rule["id"] and alarm["status"] == "ACTIVE"):
                    alarm["status"] = "RESOLVED"
                    alarm["recoveredAt"] = now
            DB.alarm_resolved(code, point)
            logging.info("alarm RESOLVED device=%s point=%s value=%s", code, point, value)


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
            for client_queue in list(SSE_CLIENTS):
                try:
                    client_queue.put_nowait(payload)
                except queue.Full:
                    pass
            value = payload.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                evaluate_rules(code, payload["pointCode"], value, time.time())
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


def restore_active_alarms():
    """Rebuild the in-memory alarm list after a restart.

    Without this the dashboard shows an empty alarm centre even though the
    database still holds unresolved alarms. The matching rule's state is
    marked active as well, so a device still in breach does not open a
    duplicate alarm on the next reading.
    """
    load_rules(force=True)
    rows = DB.query(
        """SELECT device_code, point_code, alarm_level, trigger_value,
                  threshold_value, trigger_time, ack_time
           FROM iot_alarm_record WHERE status='ACTIVE' ORDER BY id""")
    if not rows:
        return
    restored = 0
    for device, point, level, value, threshold, trigger_time, ack_time in rows:
        if len(DATA["alarms"]) >= MAX_ALARMS:
            break
        triggered = trigger_time.replace(tzinfo=timezone.utc).timestamp() if trigger_time else time.time()
        # Carry the matching rule id: the recovery branch of evaluate_rules
        # matches alarms by ruleId, so a restored alarm without it would
        # stay ACTIVE forever.
        alarm = {"deviceCode": device, "pointCode": point, "level": level,
                 "value": float(value) if value is not None else None,
                 "threshold": float(threshold) if threshold is not None else None,
                 "status": "ACTIVE", "ack": ack_time is not None, "time": triggered}
        for rule in RULE_ENGINE["rules"]:
            if rule_matches(rule, device, point):
                RULE_ENGINE["state"][(rule["id"], device)] = {"since": None, "active": True}
                if alarm.get("ruleId") is None:
                    alarm["ruleId"] = rule["id"]
        DATA["alarms"].append(alarm)
        restored += 1
    if restored:
        logging.info("restored %s active alarm(s) from the database", restored)


def consume():
    load_persisted_registry()
    restore_active_alarms()

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

    def client_ip(self):
        return self.client_address[0]

    def bearer_token(self):
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip()
        return None

    def require_auth(self):
        """Bearer-token gate for data APIs. Exempts health/auth so probes and
        the login flow itself stay reachable."""
        if not AUTH_ENABLED:
            return True
        token = self.bearer_token()
        if token and token_valid(token):
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
        if path == "/api/stream":
            return self.stream_events()
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
                params = parse_qs(urlparse(self.path).query)
                device = (params.get("device") or [None])[0]
                point = (params.get("point") or [None])[0]
                try:
                    limit = min(1000, int((params.get("limit") or ["300"])[0]))
                except ValueError:
                    limit = 300
                try:
                    interval = max(0, int((params.get("interval") or ["0"])[0]))
                except ValueError:
                    interval = 0
                since = as_datetime((params.get("from") or [""])[0])
                until = as_datetime((params.get("to") or [""])[0])
                stored = DB.history(limit=limit, device=device, point=point,
                                    since=since, until=until,
                                    interval=interval if (since or until) else None)
                if stored is not None:
                    return self.send_json(stored)
                # Database unavailable: serve the in-memory ring buffer instead
                # of failing, mirroring the write path's fallback behaviour.
                buffered = list(DATA["telemetry"])
                if device:
                    buffered = [row for row in buffered if row.get("deviceCode") == device]
                if point:
                    buffered = [row for row in buffered if row.get("pointCode") == point]
                return self.send_json(buffered[-limit:])
            if path == "/api/alarms":
                return self.send_json(list(DATA["alarms"]))
            if path == "/api/points":
                return self.send_json(DB.point_configs() or [])
            if path == "/api/alarm-rules":
                return self.send_json(DB.alarm_rules() or [])
            if path == "/api/debug/engine":
                return self.send_json({
                    "rules": RULE_ENGINE["rules"],
                    "loadedAt": RULE_ENGINE["loadedAt"],
                    "state": {f"{k[0]}/{k[1]}": v for k, v in RULE_ENGINE["state"].items()},
                    "registrySources": {s: sorted(c) for s, c in ACTIVE_DEVICE_CODES_BY_SOURCE.items()},
                })
        return self.send_json({"error": "not found"}, 404)

    def stream_events(self):
        """Serve one Server-Sent Events connection until the client hangs up.

        EventSource cannot send headers, so the token arrives as a query
        parameter and is validated before the stream opens."""
        if AUTH_ENABLED:
            params = parse_qs(urlparse(self.path).query)
            token = (params.get("token") or [None])[0]
            if not token or not token_valid(token):
                return self.send_json({"error": "unauthorized"}, 401)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", self.cors_origin())
        self.end_headers()
        client_queue = queue.Queue(maxsize=200)
        with SSE_LOCK:
            SSE_CLIENTS.append(client_queue)
        try:
            while True:
                try:
                    payload = client_queue.get(timeout=15)
                    line = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    line = ": ping\n\n"  # keep intermediaries from closing the stream
                self.wfile.write(line.encode())
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass
        finally:
            with SSE_LOCK:
                if client_queue in SSE_CLIENTS:
                    SSE_CLIENTS.remove(client_queue)

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def do_PUT(self):
        if not self.require_auth():
            return
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "points"]:
            fields = self.read_body()
            if fields is None:
                return self.send_json({"error": "invalid request"}, 400)
            ok = DB.update_point(parts[2].upper(), parts[3], fields)
            if ok:
                return self.send_json({"ok": True})
            return self.send_json({"error": "point not found or database unavailable"}, 404)
        if len(parts) == 3 and parts[:2] == ["api", "alarm-rules"] and parts[2].isdigit():
            fields = self.read_body()
            if fields is None:
                return self.send_json({"error": "invalid request"}, 400)
            if not DB.alarm_rule_update(int(parts[2]), fields):
                return self.send_json({"error": "rule not found or nothing to update"}, 404)
            load_rules(force=True)
            return self.send_json({"ok": True})
        return self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self.require_auth():
            return
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "alarm-rules"] and parts[2].isdigit():
            if DB.alarm_rule_delete(int(parts[2])):
                load_rules(force=True)
                return self.send_json({"ok": True})
            return self.send_json({"error": "rule not found or database unavailable"}, 404)
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/auth/login", "/api/auth/logout") and not self.require_auth():
            return
        if path == "/api/alarm-rules":
            body = self.read_body()
            if not body:
                return self.send_json({"error": "invalid request"}, 400)
            try:
                ok = DB.alarm_rule_insert(
                    str(body.get("deviceCode", "*")), str(body.get("pointCode", "")),
                    str(body.get("operator", ">")), float(body.get("threshold", 0)),
                    int(body.get("durationSec", 0)), float(body.get("hysteresis", 0)),
                    str(body.get("level", "SERIOUS")))
            except (ValueError, TypeError):
                return self.send_json({"error": "invalid rule fields"}, 400)
            if not ok:
                return self.send_json({"error": "database unavailable or duplicate rule"}, 400)
            load_rules(force=True)
            return self.send_json({"ok": True}, 201)
        if path == "/api/alarms/ack":
            body = self.read_body()
            if not body:
                return self.send_json({"error": "invalid request"}, 400)
            code, point = body.get("deviceCode"), body.get("pointCode")
            if not code or not point:
                return self.send_json({"error": "deviceCode/pointCode required"}, 400)
            now = time.time()
            with LOCK:
                acked = [a for a in DATA["alarms"]
                         if a["deviceCode"] == code and a["pointCode"] == point and a["status"] == "ACTIVE"]
                for alarm in acked:
                    alarm["ack"] = True
                    alarm["ackTime"] = now
            DB.alarm_acked(code, point)
            return self.send_json({"ok": True, "acked": len(acked)})
        if path == "/api/auth/login":
            payload = self.read_body()
            if not payload:
                return self.send_json({"error": "invalid request"}, 400)
            ip = self.client_ip()
            if not login_allowed(ip):
                retry = int(LOGIN_FAILURES.get(ip, (0, 0.0))[1] - time.time()) + 1
                logging.warning("login throttled for %s", ip)
                return self.send_json({"error": f"too many failed attempts, retry in {retry}s"}, 429)
            user_ok = hmac.compare_digest(str(payload.get("username", "")), ADMIN_USER)
            pass_ok = hmac.compare_digest(str(payload.get("password", "")), ADMIN_PASSWORD)
            if not (user_ok and pass_ok):
                locked_until = record_login_failure(ip)
                if locked_until:
                    logging.warning("login lockout engaged for %s", ip)
                return self.send_json({"error": "invalid credentials"}, 401)
            record_login_success(ip)
            return self.send_json({"token": issue_token(),
                                   "user": {"username": ADMIN_USER, "role": "ADMIN"},
                                   "expiresInSeconds": TOKEN_TTL_SECONDS})
        if path == "/api/auth/logout":
            token = self.bearer_token()
            if token:
                revoke_token(token)
            return self.send_json({"ok": True})
        return self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    threading.Thread(target=consume, daemon=True).start()
    threading.Thread(target=retention_loop, daemon=True).start()
    print("[backend] http://127.0.0.1:8080", flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()

"""Edge collector entry point.

The point table (the facts about which registers to poll) lives in MySQL
(``iot_point_config``); this process reads it at startup and falls back to
the YAML file when the database is unavailable.

Run:  python -m edge_collector.main --config edge-collector/configs/point-table.yml
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "middleware" / "src"))

from edge_collector.collector import CollectorDevice
from edge_collector.point_table import GatewayConfig, PointConfig, load_point_table
from edge_collector.registers import CODE_TO_SCENARIO, FX_HOLDING, SCENARIO_REGISTER

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CONTROL_PORT_DEFAULT = 8093


def make_control_server(collector: CollectorDevice, host: str, control_port: int):
    """HTTP control endpoint for the collector.

    POST /api/scenario {"scenario": "..."}  -> write the scenario register to
    the PLC over a short-lived Modbus connection (the collection thread keeps
    its own connection; Modbus TCP serves them independently).
    GET  /api/scenario                      -> current scenario register value
    GET  /health                            -> liveness
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class ControlHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _json(self, payload, status=200):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _modbus_session(self):
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(collector.gateway.host, port=collector.gateway.port, timeout=3)
            connected = client.connect()
            return client, connected

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/health":
                return self._json({"status": "UP", "gateway": collector.gateway.id,
                                   "devices": collector.device_codes})
            if path == "/api/scenario":
                client, connected = self._modbus_session()
                if not connected:
                    client.close()
                    return self._json({"error": "PLC unreachable"}, 503)
                try:
                    raw = client.read_holding_registers(SCENARIO_REGISTER, count=1, slave=1).registers[0]
                finally:
                    client.close()
                return self._json({"scenario": CODE_TO_SCENARIO.get(raw, "unknown"), "raw": raw})
            return self._json({"error": "not found"}, 404)

        def do_POST(self):
            if urlparse(self.path).path != "/api/scenario":
                return self._json({"error": "not found"}, 404)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                scenario = str(body.get("scenario", ""))
                from edge_collector.registers import SCENARIO_CODES
                raw = SCENARIO_CODES[scenario]
            except (ValueError, json.JSONDecodeError, KeyError):
                return self._json({"error": "scenario must be one of normal/high-temperature/low-pressure"}, 400)
            client, connected = self._modbus_session()
            if not connected:
                client.close()
                return self._json({"error": "PLC unreachable"}, 503)
            try:
                client.write_register(SCENARIO_REGISTER, raw, slave=1)
            finally:
                client.close()
            logging.info("scenario register written: %s (HR15=%s)", scenario, raw)
            return self._json({"ok": True, "scenario": scenario})

    return ThreadingHTTPServer((host, control_port), ControlHandler)


def load_points_from_db():
    """Read the enabled point-table rows from MySQL; None when unavailable."""
    try:
        import mysql.connector
    except ImportError:
        return None
    try:
        conn = mysql.connector.connect(
            host=os.getenv("IOT_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("IOT_DB_PORT", "3306")),
            user=os.getenv("IOT_DB_USER", "root"),
            password=os.getenv("IOT_DB_PASSWORD", "root1234"),
            database=os.getenv("IOT_DB_NAME", "iot_monitor"),
            autocommit=True,
        )
    except Exception as exc:
        logging.warning("point-table database unavailable (%s), falling back to YAML", exc)
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT device_code, point_code, slave_id, register, register_type,
                      scale_factor, dead_zone, collect_interval_ms, unit
               FROM iot_point_config WHERE enabled=1 ORDER BY device_code, point_code""")
        rows = cur.fetchall()
        cur.close()
    except Exception as exc:
        logging.warning("point-table query failed (%s), falling back to YAML", exc)
        conn.close()
        return None
    conn.close()
    return [PointConfig(device_code=r[0], point_code=r[1], slave_id=r[2], register=r[3],
                        register_type=r[4], scale_factor=float(r[5]), dead_zone=float(r[6]),
                        collect_interval_ms=r[7], unit=r[8]) for r in rows]


def main():
    parser = argparse.ArgumentParser(description="Modbus TCP edge collector")
    parser.add_argument("--config", default=None, help="fallback point-table YAML file")
    parser.add_argument("--slave-host", default=None, help="Modbus slave host (default: YAML or 127.0.0.1)")
    parser.add_argument("--slave-port", type=int, default=None, help="Modbus slave port (default: YAML or 1502)")
    parser.add_argument("--control-port", type=int, default=CONTROL_PORT_DEFAULT,
                        help="HTTP control endpoint port (0 disables)")
    parser.add_argument("--mqtt-host", default=os.getenv("IOT_MQTT_HOST", "127.0.0.1"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.getenv("IOT_MQTT_PORT", "1883")))
    args = parser.parse_args()

    fallback = load_point_table(args.config) if args.config else None
    db_points = load_points_from_db()
    if db_points:
        source = "database"
        gateway = GatewayConfig(
            id=(fallback.id if fallback else "gw-001"),
            host=args.slave_host or (fallback.host if fallback else "127.0.0.1"),
            port=args.slave_port or (fallback.port if fallback else 1502),
            heartbeat_interval_sec=(fallback.heartbeat_interval_sec if fallback else 10.0),
            points=db_points,
        )
    elif fallback:
        source = "yaml fallback"
        gateway = fallback
        if args.slave_host:
            gateway.host = args.slave_host
        if args.slave_port:
            gateway.port = args.slave_port
    else:
        raise SystemExit("no point table: database unavailable and no --config YAML given")

    collector = CollectorDevice(gateway, mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port)
    print(f"[edge-collector] gateway={gateway.id} source={source} points={len(gateway.points)} "
          f"devices={collector.device_codes} modbus={gateway.host}:{gateway.port}", flush=True)
    if args.control_port:
        import threading
        control = make_control_server(collector, "127.0.0.1", args.control_port)
        threading.Thread(target=control.serve_forever, name="collector-control", daemon=True).start()
        print(f"[edge-collector] control API http://127.0.0.1:{args.control_port}/api/scenario", flush=True)
    try:
        collector.run()
    except KeyboardInterrupt:
        collector.stop()


if __name__ == "__main__":
    main()

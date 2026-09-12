"""Edge collector entry point.

Run:  python -m edge_collector.main --config edge-collector/configs/point-table.yml
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "middleware" / "src"))

from edge_collector.collector import CollectorDevice
from edge_collector.point_table import load_point_table

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Modbus TCP edge collector")
    parser.add_argument("--config", required=True, help="point-table YAML file")
    parser.add_argument("--mqtt-host", default=os.getenv("IOT_MQTT_HOST", "127.0.0.1"))
    parser.add_argument("--mqtt-port", type=int, default=int(os.getenv("IOT_MQTT_PORT", "1883")))
    args = parser.parse_args()
    gateway = load_point_table(args.config)
    collector = CollectorDevice(gateway, mqtt_host=args.mqtt_host, mqtt_port=args.mqtt_port)
    print(f"[edge-collector] gateway={gateway.id} points={len(gateway.points)} "
          f"devices={collector.device_codes} modbus={gateway.host}:{gateway.port}", flush=True)
    try:
        collector.run()
    except KeyboardInterrupt:
        collector.stop()


if __name__ == "__main__":
    main()

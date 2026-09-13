"""Point-table loading: the configuration that maps registers to telemetry."""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml

DATA_TYPES = ("int16", "uint16", "int32", "float32")
BYTE_ORDERS = ("ABCD", "CDAB", "BADC", "DCBA")
COUNT_FOR_TYPE = {"int16": 1, "uint16": 1, "int32": 2, "float32": 2}


@dataclass
class PointConfig:
    device_code: str
    point_code: str
    register: int
    slave_id: int = 1
    register_type: str = "holding"      # holding | input
    data_type: str = "uint16"           # int16 | uint16 | int32 | float32
    byte_order: str = "ABCD"            # ABCD | CDAB | BADC | DCBA (multi-register)
    register_count: int = 1             # derived from data_type when omitted
    scale_factor: float = 1.0           # engineering value = raw * scale_factor
    dead_zone: float = 0.0              # report only when |value - last| >= dead_zone
    collect_interval_ms: int = 1000
    unit: str = ""

    def __post_init__(self):
        if self.data_type not in DATA_TYPES:
            raise ValueError(f"unsupported data_type {self.data_type!r}")
        if self.byte_order not in BYTE_ORDERS:
            raise ValueError(f"unsupported byte_order {self.byte_order!r}")
        if not self.register_count:
            self.register_count = COUNT_FOR_TYPE[self.data_type]

    @staticmethod
    def from_dict(payload):
        required = ("device_code", "point_code", "register")
        for key in required:
            if payload.get(key) is None:
                raise ValueError(f"point table entry missing {key}: {payload}")
        return PointConfig(
            device_code=str(payload["device_code"]).strip().upper(),
            point_code=str(payload["point_code"]).strip(),
            register=int(payload["register"]),
            slave_id=int(payload.get("slave_id", 1)),
            register_type=str(payload.get("register_type", "holding")),
            data_type=str(payload.get("data_type", "uint16")),
            byte_order=str(payload.get("byte_order", "ABCD")),
            register_count=int(payload.get("register_count") or 0),
            scale_factor=float(payload.get("scale_factor", 1.0)),
            dead_zone=float(payload.get("dead_zone", 0.0)),
            collect_interval_ms=max(200, int(payload.get("collect_interval_ms", 1000))),
            unit=str(payload.get("unit", "")),
        )


@dataclass
class GatewayConfig:
    id: str = "gw-001"
    host: str = "127.0.0.1"
    port: int = 1502
    heartbeat_interval_sec: float = 10.0
    points: list = field(default_factory=list)


def load_point_table(path) -> GatewayConfig:
    """Load and validate a point-table YAML file."""
    with open(path, encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if not isinstance(document.get("points"), list) or not document["points"]:
        raise ValueError("point table must define a non-empty 'points' list")
    gateway = GatewayConfig(
        id=str(document.get("gateway", {}).get("id", "gw-001")),
        host=str(document.get("gateway", {}).get("host", "127.0.0.1")),
        port=int(document.get("gateway", {}).get("port", 1502)),
        heartbeat_interval_sec=float(document.get("gateway", {}).get("heartbeat_interval_sec", 10)),
    )
    gateway.points = [PointConfig.from_dict(item) for item in document["points"]]
    codes = {(point.device_code, point.point_code) for point in gateway.points}
    if len(codes) != len(gateway.points):
        raise ValueError("duplicate device_code/point_code entries in point table")
    return gateway

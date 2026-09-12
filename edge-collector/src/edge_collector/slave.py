"""Modbus TCP slave simulator — stands in for a real metering PLC.

Exposes the fixed-point register layout from ``registers.py`` on port 1502
(port 502 needs privileges). A writer task keeps the registers changing like
a live process; ``--scenario`` reproduces the demo fault conditions.

Run:  python -m edge_collector.slave --port 1502 --scenario normal
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import StartAsyncTcpServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from edge_collector.registers import FX_HOLDING, REGISTER_COUNT, RegisterImage, register_writer


def build_context(image: RegisterImage):
    """Seed the slave's holding registers from the current process image.

    Returns ``(context, store)``. All four blocks are passed explicitly:
    pymodbus 3.9.2's ModbusSlaveContext decides ``co/ir/hr`` based on
    ``di is not None`` alone, so a lone ``hr=`` would silently be replaced
    by an empty block.
    """
    initial = image.encode()
    registers = initial + [0] * (REGISTER_COUNT - len(initial))
    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        co=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        ir=ModbusSequentialDataBlock(0, [0] * REGISTER_COUNT),
        hr=ModbusSequentialDataBlock(0, registers),
    )
    return ModbusServerContext(slaves={1: store}, single=False), store


async def refresh_registers(store: ModbusSlaveContext, image: RegisterImage, interval=1.0):
    """Push the process image into the slave's register block each tick."""
    while True:
        image.update()
        store.setValues(FX_HOLDING, 0, image.encode())
        await asyncio.sleep(interval)


async def main_async(args):
    image = RegisterImage(scenario=args.scenario)
    context, store = build_context(image)
    print(f"[modbus-slave] device_id=1 listening on {args.host}:{args.port} scenario={image.scenario}", flush=True)
    asyncio.create_task(refresh_registers(store, image, args.tick))
    await StartAsyncTcpServer(context, address=(args.host, args.port))


def main():
    parser = argparse.ArgumentParser(description="Simulated Modbus TCP metering PLC")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1502)
    parser.add_argument("--scenario", default="normal")
    parser.add_argument("--tick", type=float, default=1.0, help="register update interval, seconds")
    args = parser.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

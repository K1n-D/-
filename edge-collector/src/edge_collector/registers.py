"""Simulated PLC register image.

Holding-register layout (16-bit, integer fixed-point — the same layout a real
metering PLC would expose):

  HR0  temperature x10     (650 -> 65.0 C)
  HR1  pressure x1000      (650 -> 0.650 MPa)
  HR2  liquid_level x10    (720 -> 72.0 %)
  HR3  running status word (1 = running)
  HR15 process scenario    (0 = normal, 1 = high-temperature, 2 = low-pressure)

A background writer keeps the values moving the way a real process does so
collectors observe changing data instead of a constant snapshot. Operators
change the process behaviour by writing the scenario register (HR15) — the
same "write a setpoint to the PLC" interaction a real control system uses.
"""
from __future__ import annotations

import asyncio
import math
import random
import threading
import time

FX_HOLDING = 3

REGISTER_COUNT = 16
SCENARIO_REGISTER = 15
SCENARIO_CODES = {"normal": 0, "high-temperature": 1, "low-pressure": 2}
CODE_TO_SCENARIO = {code: name for name, code in SCENARIO_CODES.items()}


class RegisterImage:
    """The slave-side process values and their fixed-point encoding."""

    BASES = {"temperature": 65.0, "pressure": 0.65, "liquid_level": 72.0}
    SCALES = {"temperature": 10, "pressure": 1000, "liquid_level": 10}
    ADDRESSES = {"temperature": 0, "pressure": 1, "liquid_level": 2}

    def __init__(self, scenario="normal"):
        self.scenario = scenario if scenario in SCENARIO_CODES else "normal"
        self._lock = threading.RLock()
        self._values = dict(self.BASES)
        self._started = time.time()
        self.running = True

    def update(self, now=None):
        """Advance the process model one step and return the raw registers."""
        now = time.time() if now is None else now
        elapsed = now - self._started
        with self._lock:
            if self.scenario == "high-temperature":
                self._values["temperature"] = 95.0
            elif self.scenario == "low-pressure":
                self._values["pressure"] = 0.2
            else:
                for name, base in self.BASES.items():
                    variation = 0.05 * base
                    self._values[name] = base + math.sin(elapsed / 8) * variation \
                        + random.uniform(-variation * .2, variation * .2)
            self._values["temperature"] = max(-50.0, self._values["temperature"])
            return self._encode_locked()

    def set_scenario(self, scenario):
        with self._lock:
            if scenario in SCENARIO_CODES:
                self.scenario = scenario

    def encode(self):
        """Encode engineering values into the integer register image."""
        with self._lock:
            return self._encode_locked()

    def _encode_locked(self):
        """Caller must hold self._lock (a plain Lock, so never nest)."""
        registers = [0] * REGISTER_COUNT
        for name, address in self.ADDRESSES.items():
            registers[address] = int(round(self._values[name] * self.SCALES[name]))
        registers[3] = 1 if self.running else 0
        registers[SCENARIO_REGISTER] = SCENARIO_CODES[self.scenario]
        return registers


def register_writer(image, interval=1.0):
    """Return an async task coroutine that keeps the register image moving."""

    async def loop():
        while True:
            image.update()
            await asyncio.sleep(interval)

    return loop

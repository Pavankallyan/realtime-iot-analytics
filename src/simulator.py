"""IoT sensor simulator — TCP server emitting realistic multivariate readings.

Each tick (default 1s) every device in the fleet emits one JSON reading with
temperature, humidity, power and/or vibration, depending on its type.
Signals follow realistic patterns (daily-ish temperature wave, appliance power
cycles, vibration bursts) with sensor noise, and random faults are injected:

  stuck    - sensor flatlines (frozen values) for a while
  spike    - sudden temperature jump
  surge    - power draw multiplied
  dropout  - device goes silent (no readings at all)

The injected fault kind is included in each message as ``injected_fault`` so
you can validate the detector. The stream processor deliberately ignores this
field and detects faults from the values alone.

Run:
    python -m src.simulator [--port 9999] [--interval 1.0]
                            [--anomaly-prob 0.008] [--seed 42]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.models import ALL_METRICS, FLEET, DeviceProfile


class DeviceSim:
    """Stateful signal generator for one device."""

    def __init__(self, profile: DeviceProfile, rng: random.Random):
        self.profile = profile
        self.rng = rng
        self.tick = 0
        self.last_values: dict = {}
        # appliance-cycle state (power / vibration)
        self.cycle_left = 0
        self.cycle_level = 0.0
        # fault state: None or dict(kind=..., ticks_left=...)
        self.fault = None

    # -- signal models ----------------------------------------------------
    def _temperature(self) -> float:
        # time-compressed daily wave (4-minute period) + noise, so the demo
        # visibly moves; real deployments use wall-clock time instead.
        wave = 2.5 * math.sin(2 * math.pi * self.tick / 240.0)
        return self.profile.base_temp_c + wave + self.rng.gauss(0, 0.25)

    def _humidity(self, temp: float) -> float:
        h = 48.0 - 1.2 * (temp - self.profile.base_temp_c) + self.rng.gauss(0, 1.0)
        return max(25.0, min(75.0, h))

    def _power(self) -> float:
        if self.cycle_left <= 0 and self.rng.random() < 0.02:
            # an appliance kicks on for a while
            self.cycle_left = self.rng.randint(20, 60)
            self.cycle_level = self.rng.uniform(700, 1600)
        if self.cycle_left > 0:
            self.cycle_left -= 1
            return self.cycle_level + self.rng.gauss(0, 15)
        return self.profile.base_power_w + self.rng.gauss(0, 3)

    def _vibration(self) -> float:
        if self.cycle_left <= 0 and self.rng.random() < 0.015:
            self.cycle_left = self.rng.randint(15, 40)
            self.cycle_level = self.rng.uniform(0.3, 0.9)
        if self.cycle_left > 0:
            self.cycle_left -= 1
            return abs(self.cycle_level + self.rng.gauss(0, 0.05))
        return abs(0.02 + self.rng.gauss(0, 0.01))

    # -- fault injection ---------------------------------------------------
    def _maybe_start_fault(self, anomaly_prob: float) -> None:
        if self.fault is not None or self.rng.random() >= anomaly_prob:
            return
        kind = self.rng.choice(["stuck", "spike", "surge", "dropout"])
        durations = {"stuck": (30, 90), "spike": (5, 15),
                     "surge": (5, 15), "dropout": (20, 60)}
        lo, hi = durations[kind]
        self.fault = {"kind": kind, "ticks_left": self.rng.randint(lo, hi)}

    def _apply_fault(self, values: dict) -> dict:
        """Mutate values according to the active fault. Returns None on dropout."""
        if self.fault is None:
            return values
        kind = self.fault["kind"]
        self.fault["ticks_left"] -= 1
        if self.fault["ticks_left"] <= 0:
            self.fault = None
        if kind == "dropout":
            return None
        if kind == "stuck":
            # freeze at last emitted values (flatline)
            return dict(self.last_values) if self.last_values else values
        if kind == "spike" and values.get("temperature_c") is not None:
            values["temperature_c"] += self.rng.uniform(6, 10)
        if kind == "surge" and values.get("power_w") is not None:
            values["power_w"] *= self.rng.uniform(3, 5)
        return values

    # -- public -------------------------------------------------------------
    def emit(self, anomaly_prob: float) -> dict | None:
        self.tick += 1
        self._maybe_start_fault(anomaly_prob)

        values = {m: None for m in ALL_METRICS}
        metrics = self.profile.metrics
        temp = None
        if "temperature_c" in metrics:
            temp = self._temperature()
            values["temperature_c"] = round(temp, 2)
        if "humidity_pct" in metrics:
            values["humidity_pct"] = round(self._humidity(temp or self.profile.base_temp_c), 1)
        if "power_w" in metrics:
            values["power_w"] = round(max(0.0, self._power()), 1)
        if "vibration_g" in metrics:
            values["vibration_g"] = round(self._vibration(), 3)

        values = self._apply_fault(values)
        if values is None:                       # dropout: silent this tick
            return None
        self.last_values = dict(values)

        return {
            "device_id": self.profile.device_id,
            "device_type": self.profile.device_type,
            "location": self.profile.location,
            "ts": datetime.now(timezone.utc).isoformat(),
            **values,
            "injected_fault": self.fault["kind"] if self.fault else None,
        }


def serve_forever(port: int, interval: float, anomaly_prob: float, seed: int | None) -> None:
    rng = random.Random(seed)
    devices = [DeviceSim(p, rng) for p in FLEET]
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((config.STREAM_HOST, port))
    srv.listen(1)
    print(f"[simulator] fleet of {len(devices)} devices on "
          f"{config.STREAM_HOST}:{port} (interval={interval}s, seed={seed})")
    print("[simulator] waiting for stream processor to connect...")
    while True:
        conn, addr = srv.accept()
        print(f"[simulator] processor connected from {addr}")
        try:
            with conn:
                while True:
                    lines = []
                    for dev in devices:
                        reading = dev.emit(anomaly_prob)
                        if reading is not None:
                            lines.append(json.dumps(reading))
                    if lines:
                        conn.sendall(("\n".join(lines) + "\n").encode())
                    time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError):
            print("[simulator] processor disconnected, waiting to reconnect...")
        except KeyboardInterrupt:
            print("\n[simulator] shutting down")
            return


def main() -> None:
    ap = argparse.ArgumentParser(description="IoT sensor stream simulator")
    ap.add_argument("--port", type=int, default=config.STREAM_PORT)
    ap.add_argument("--interval", type=float, default=config.TICK_INTERVAL_SEC)
    ap.add_argument("--anomaly-prob", type=float, default=config.DEFAULT_ANOMALY_PROB)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    serve_forever(args.port, args.interval, args.anomaly_prob, args.seed)


if __name__ == "__main__":
    main()

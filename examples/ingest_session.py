#!/usr/bin/env python3
"""Walkthrough: drive the stream processor in-process (no network, no simulator).

Feeds one synthetic sensor session straight through Processor.detect() /
gap_sweep() / fleet_snapshot():
    normal warm-up -> temperature spike -> stuck sensor -> dropout -> recovery
then prints every alert raised and the final SQLite table counts.

Run:
    python examples/ingest_session.py
"""

import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config

# Point the processor at a throwaway DB so the demo never touches data/iot.db.
TMP = Path(tempfile.mkdtemp(prefix="ingest-session-"))
config.DATA_DIR = TMP / "data"
config.DB_PATH = config.DATA_DIR / "iot.db"

from src.processor import Processor


def reading(device_id, **metrics):
    r = {"device_id": device_id,
         "ts": datetime.now(timezone.utc).isoformat()}
    r.update(metrics)
    return r


def feed(proc, device_id, readings):
    """Push readings through detect() + store_reading() like the real loop."""
    anomalous = 0
    for r in readings:
        is_anomaly = proc.detect(reading(device_id, **r))
        proc.store_reading(reading(device_id, **r), is_anomaly)
        anomalous += is_anomaly
    return anomalous


def show_alerts(proc, label):
    print(f"\n--- alerts after {label} ---")
    rows = proc.db.execute(
        "SELECT device_id, kind, metric, value, message FROM alerts"
        " ORDER BY id").fetchall()
    if not rows:
        print("  (none)")
    for device_id, kind, metric, value, message in rows:
        extra = f" {metric}={value}" if metric else ""
        print(f"  [{kind}]{extra} {device_id}: {message}")


def main():
    proc = Processor(port=9999, interval=1.0)
    dev = "living-room-thermostat"

    print("[1] warm-up: 30 normal ticks (temperature ~22 C, humidity ~45 %)")
    n = feed(proc, dev,
             [{"temperature_c": 22.0 + (0.1 if i % 2 else -0.1),
               "humidity_pct": 45.0 + (0.5 if i % 2 else -0.5)} for i in range(30)])
    print(f"    anomalous: {n} (expected 0)")
    show_alerts(proc, "warm-up")

    print("\n[2] temperature spike: 35.0 C for 3 ticks")
    n = feed(proc, dev,
             [{"temperature_c": 35.0,
               "humidity_pct": 45.0 + (0.5 if i % 2 else -0.5)} for i in range(3)])
    print(f"    anomalous: {n} (expected >= 1)")
    show_alerts(proc, "spike")

    print("\n[3] stuck sensor: temperature flatlines at 20.0 C")
    n = feed(proc, dev,
             [{"temperature_c": 20.0,
               "humidity_pct": 45.0 + (0.5 if i % 2 else -0.5)}
              for i in range(config.FLATLINE_WINDOW + 2)])
    print(f"    anomalous: {n} (expected 0 - flatline is its own alert kind)")
    show_alerts(proc, "flatline")

    print("\n[4] dropout: sensor goes silent for 10 s")
    proc.states[dev].last_seen = time.time() - 10
    proc.gap_sweep()
    show_alerts(proc, "gap sweep")

    print("\n[5] recovery: sensor resumes")
    n = feed(proc, dev,
             [{"temperature_c": 22.0, "humidity_pct": 45.3}])
    print(f"    anomalous: {n} (expected 0)")
    show_alerts(proc, "recovery")

    proc.fleet_snapshot()
    proc.db.commit()
    print("\n--- SQLite table counts ---")
    for table in ("readings", "alerts", "fleet_stats"):
        count = proc.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count} rows")
    proc.db.close()
    print(f"\nDemo complete. Throwaway DB was at {config.DB_PATH}")


if __name__ == "__main__":
    main()

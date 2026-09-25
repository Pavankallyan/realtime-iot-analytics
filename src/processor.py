"""Stream processor: consumes the simulator TCP stream, maintains per-device
sliding windows, detects anomalies, and persists everything to SQLite.

Detections (all edge-triggered, so one alert per episode — no alert storms):
  zscore   - latest reading's |z| vs. its sliding window exceeds threshold
  stuck    - a metric flatlines (zero variance over FLATLINE_WINDOW ticks)
  offline  - no reading for GAP_MULTIPLIER * tick interval (gap sweep)
  recovered- device resumes after an offline episode

SQLite stands in for a production time-series store (e.g. AWS Timestream /
InfluxDB); the schema maps 1:1. See README for the Kafka production mapping.

Run:
    python -m src.processor [--port 9999]
"""

from __future__ import annotations

import argparse
import json
import socket
import sqlite3
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.models import ALL_METRICS, FLEET

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    temperature_c REAL,
    humidity_pct REAL,
    power_w REAL,
    vibration_g REAL,
    is_anomaly INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings(device_id, ts);
CREATE TABLE IF NOT EXISTS alerts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    metric TEXT,
    value REAL,
    zscore REAL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS fleet_stats(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    active_devices INTEGER,
    avg_temp_c REAL,
    total_power_w REAL,
    alerts_last_5min INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeviceState:
    """Sliding-window state and alert bookkeeping for one device."""

    def __init__(self) -> None:
        self.window: deque = deque(maxlen=config.WINDOW_SIZE)
        self.last_seen: float | None = None
        self.offline = False
        # (kind, metric) pairs currently in alert, for edge triggering
        self.active: set[tuple[str, str | None]] = set()

    def stats(self, metric: str) -> tuple[float, float] | tuple[None, None]:
        vals = [r[metric] for r in self.window
                if r.get(metric) is not None]
        if len(vals) < config.MIN_WINDOW_FOR_STATS:
            return None, None
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        return mean, var ** 0.5


class Processor:
    def __init__(self, port: int, interval: float):
        self.port = port
        self.interval = interval
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(config.DB_PATH, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL;")
        self.db.executescript(SCHEMA)
        self.states: dict[str, DeviceState] = defaultdict(DeviceState)
        self.processed = 0

    # -- persistence ------------------------------------------------------
    def store_reading(self, reading: dict, is_anomaly: bool) -> None:
        self.db.execute(
            """INSERT INTO readings
               (device_id, ts, temperature_c, humidity_pct, power_w, vibration_g, is_anomaly)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (reading["device_id"], reading["ts"],
             reading.get("temperature_c"), reading.get("humidity_pct"),
             reading.get("power_w"), reading.get("vibration_g"),
             int(is_anomaly)))

    def raise_alert(self, device_id: str, kind: str, metric: str | None,
                    value: float | None, zscore: float | None, message: str) -> None:
        self.db.execute(
            """INSERT INTO alerts (ts, device_id, kind, metric, value, zscore, message)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (now_iso(), device_id, kind, metric, value, zscore, message))
        print(f"[alert] {device_id} {kind} {metric or ''}: {message}")

    # -- detection ----------------------------------------------------------
    def _edge(self, state: DeviceState, key: tuple[str, str | None],
              active: bool, raiser) -> None:
        """Fire raiser() only on False->True transitions of an alert condition."""
        if active and key not in state.active:
            state.active.add(key)
            raiser()
        elif not active and key in state.active:
            state.active.discard(key)

    def detect(self, reading: dict) -> bool:
        """Update windows and run detectors. Returns True if anomalous."""
        device_id = reading["device_id"]
        state = self.states[device_id]
        now = time.time()
        anomalous = False

        if state.offline:  # device came back
            state.offline = False
            self.raise_alert(device_id, "recovered", None, None, None,
                             f"{device_id} is sending readings again")

        for metric in ALL_METRICS:
            value = reading.get(metric)
            if value is None:
                continue
            mean, std = state.stats(metric)
            # z-score vs the window *before* this reading
            if mean is not None and std is not None and std > 1e-9:
                z = (value - mean) / std
                is_out = abs(z) > config.ZSCORE_THRESHOLD
                anomalous |= is_out
                self._edge(
                    state, ("zscore", metric), is_out,
                    lambda z=z, v=value, m=metric: self.raise_alert(
                        device_id, "zscore", m, v, round(z, 2),
                        f"{m}={v} (|z|={abs(z):.1f}) outside rolling window"))
            # flatline: sensor stuck (zero variance over recent history)
            recent = [r[metric] for r in list(state.window)[-config.FLATLINE_WINDOW:]
                      if r.get(metric) is not None]
            flat = (len(recent) >= config.FLATLINE_WINDOW
                    and max(recent) - min(recent) < 1e-9)
            self._edge(
                state, ("stuck", metric), flat,
                lambda v=value, m=metric: self.raise_alert(
                    device_id, "stuck", m, v,
                    None, f"{m} flatlined at {v} — sensor likely stuck"))

        state.window.append({m: reading.get(m) for m in ALL_METRICS})
        state.last_seen = now
        return anomalous

    def gap_sweep(self) -> None:
        """Flag devices that went silent (dropout faults / real outages)."""
        now = time.time()
        for device_id, state in self.states.items():
            if state.last_seen is None or state.offline:
                continue
            if now - state.last_seen > config.GAP_MULTIPLIER * self.interval:
                state.offline = True
                state.active.clear()  # reset edge triggers for the next episode
                self.raise_alert(device_id, "offline", None, None, None,
                                 f"no readings for {now - state.last_seen:.0f}s — "
                                 f"{device_id} may be offline")

    def fleet_snapshot(self) -> None:
        """Persist one fleet-wide aggregate row (CloudWatch-style metrics)."""
        temps, powers, active = [], [], 0
        for profile in FLEET:
            st = self.states.get(profile.device_id)
            if not st or not st.window or st.offline:
                continue
            active += 1
            last = st.window[-1]
            if last.get("temperature_c") is not None:
                temps.append(last["temperature_c"])
            if last.get("power_w") is not None:
                powers.append(last["power_w"])
        alerts_5min = self.db.execute(
            "SELECT COUNT(*) FROM alerts WHERE ts >= datetime('now', '-5 minutes')"
        ).fetchone()[0]
        self.db.execute(
            """INSERT INTO fleet_stats
               (ts, active_devices, avg_temp_c, total_power_w, alerts_last_5min)
               VALUES (?, ?, ?, ?, ?)""",
            (now_iso(), active,
             sum(temps) / len(temps) if temps else None,
             round(sum(powers), 1), alerts_5min))

    # -- main loop ----------------------------------------------------------
    def run(self) -> None:
        backoff = 1
        while True:
            try:
                print(f"[processor] connecting to "
                      f"{config.STREAM_HOST}:{self.port}...")
                sock = socket.create_connection(
                    (config.STREAM_HOST, self.port), timeout=10)
                print("[processor] connected, processing stream")
                backoff = 1
                with sock, sock.makefile("r") as stream:
                    for line in stream:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            reading = json.loads(line)
                        except json.JSONDecodeError:
                            continue  # skip corrupt frames, keep the stream alive
                        anomalous = self.detect(reading)
                        self.store_reading(reading, anomalous)
                        self.processed += 1
                        if self.processed % config.FLEET_SNAPSHOT_EVERY == 0:
                            self.gap_sweep()
                            self.fleet_snapshot()
                            self.db.commit()
            except (ConnectionRefusedError, ConnectionResetError,
                    BrokenPipeError, socket.timeout, OSError) as e:
                print(f"[processor] stream error ({e}); retrying in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            except KeyboardInterrupt:
                print("\n[processor] shutting down")
                self.db.commit()
                self.db.close()
                return


def main() -> None:
    ap = argparse.ArgumentParser(description="IoT stream processor")
    ap.add_argument("--port", type=int, default=config.STREAM_PORT)
    ap.add_argument("--interval", type=float, default=config.TICK_INTERVAL_SEC,
                    help="expected seconds between readings (gap detection)")
    args = ap.parse_args()
    Processor(args.port, args.interval).run()


if __name__ == "__main__":
    main()

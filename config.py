"""Shared configuration for the realtime-iot-analytics demo."""

from pathlib import Path

# Project root (parent of src/)
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "iot.db"

# TCP transport between simulator and processor.
# In production this is replaced by a Kafka topic (see README).
STREAM_HOST = "127.0.0.1"
STREAM_PORT = 9999

# Simulator settings
TICK_INTERVAL_SEC = 1.0        # one reading per device per tick
DEFAULT_ANOMALY_PROB = 0.008   # per-device, per-tick fault injection probability

# Processor settings
WINDOW_SIZE = 120              # sliding window length (readings per device)
MIN_WINDOW_FOR_STATS = 20      # warm-up before z-scores are trusted
ZSCORE_THRESHOLD = 3.0         # |z| above this => anomaly
FLATLINE_WINDOW = 30           # identical readings over this many ticks => stuck sensor
GAP_MULTIPLIER = 4             # no reading for GAP_MULTIPLIER * interval => offline
FLEET_SNAPSHOT_EVERY = 5       # processed readings between fleet snapshots

# Dashboard settings
DASHBOARD_REFRESH_SEC = 2
DEFAULT_CHART_MINUTES = 10

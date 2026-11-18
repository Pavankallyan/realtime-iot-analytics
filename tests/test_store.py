"""Unit tests for processor persistence (src/processor.py).

Covers the SQLite writes the dashboard reads: readings, alerts, and the
fleet-stats snapshots — plus the offline/recovered round trip. Runs entirely
in-process against a temporary DB; no simulator, no sockets.
"""

import time
from datetime import datetime, timezone

import pytest

import config
from src.processor import Processor


@pytest.fixture()
def proc(tmp_path, monkeypatch):
    """Processor writing to a fresh temp-dir SQLite DB."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "iot.db")
    p = Processor(port=19999, interval=1.0)
    yield p
    p.db.close()


def _reading(device_id="living-room-thermostat", **metrics):
    r = {"device_id": device_id,
         "ts": datetime.now(timezone.utc).isoformat()}
    r.update(metrics)
    return r


def _rows(proc, sql, params=()):
    return proc.db.execute(sql, params).fetchall()


# -- store_reading ----------------------------------------------------------

def test_store_reading_persists_all_fields(proc):
    proc.store_reading(_reading(temperature_c=22.5, humidity_pct=45.0,
                                power_w=None, vibration_g=None), False)
    proc.db.commit()
    rows = _rows(proc, "SELECT device_id, temperature_c, humidity_pct, "
                       "power_w, vibration_g, is_anomaly FROM readings")
    assert len(rows) == 1
    assert rows[0] == ("living-room-thermostat", 22.5, 45.0, None, None, 0)


def test_store_reading_marks_anomaly_flag(proc):
    proc.store_reading(_reading(temperature_c=99.0), True)
    proc.db.commit()
    assert _rows(proc, "SELECT is_anomaly FROM readings")[0][0] == 1


def test_store_reading_missing_metrics_become_null(proc):
    proc.store_reading(_reading(power_w=60.0), False)
    proc.db.commit()
    row = _rows(proc, "SELECT temperature_c, humidity_pct, power_w, "
                      "vibration_g FROM readings")[0]
    assert row == (None, None, 60.0, None)


def test_store_reading_multiple_devices(proc):
    for dev in ("living-room-thermostat", "kitchen-plug", "hvac-vibration"):
        proc.store_reading(_reading(device_id=dev, temperature_c=21.0), False)
    proc.db.commit()
    assert _rows(proc, "SELECT COUNT(*) FROM readings")[0][0] == 3
    assert _rows(proc, "SELECT COUNT(DISTINCT device_id) FROM readings")[0][0] == 3


# -- raise_alert ------------------------------------------------------------

def test_raise_alert_persists_row(proc):
    proc.raise_alert("kitchen-plug", "zscore", "power_w", 350.0, 4.2,
                     "power_w=350.0 (|z|=4.2) outside rolling window")
    proc.db.commit()
    row = _rows(proc, "SELECT device_id, kind, metric, value, zscore, "
                      "message FROM alerts")[0]
    assert row == ("kitchen-plug", "zscore", "power_w", 350.0, 4.2,
                   "power_w=350.0 (|z|=4.2) outside rolling window")


def test_raise_alert_allows_null_metric_fields(proc):
    proc.raise_alert("hvac-vibration", "offline", None, None, None,
                     "no readings for 5s — hvac-vibration may be offline")
    proc.db.commit()
    row = _rows(proc, "SELECT kind, metric, value, zscore FROM alerts")[0]
    assert row == ("offline", None, None, None)


def test_raise_alert_sets_timestamp(proc):
    before = datetime.now(timezone.utc)
    proc.raise_alert("dev-1", "stuck", "temperature_c", 22.0, None, "flatlined")
    proc.db.commit()
    ts = _rows(proc, "SELECT ts FROM alerts")[0][0]
    parsed = datetime.fromisoformat(ts)
    assert before <= parsed <= datetime.now(timezone.utc)


# -- gap sweep + recovery ----------------------------------------------------

def test_gap_sweep_flags_silent_device_offline(proc):
    proc.detect(_reading(temperature_c=22.0))
    state = proc.states["living-room-thermostat"]
    state.last_seen = time.time() - 100  # silent far beyond 4 x interval
    proc.gap_sweep()
    proc.db.commit()
    assert state.offline is True
    assert _rows(proc, "SELECT kind FROM alerts WHERE kind='offline'") != []


def test_gap_sweep_ignores_recent_devices(proc):
    proc.detect(_reading(temperature_c=22.0))
    proc.gap_sweep()
    assert proc.states["living-room-thermostat"].offline is False
    assert _rows(proc, "SELECT COUNT(*) FROM alerts")[0][0] == 0


def test_recovered_alert_when_device_resumes(proc):
    proc.detect(_reading(temperature_c=22.0))
    state = proc.states["living-room-thermostat"]
    state.last_seen = time.time() - 100
    proc.gap_sweep()
    proc.detect(_reading(temperature_c=22.1))
    proc.db.commit()
    assert state.offline is False
    assert _rows(proc, "SELECT kind FROM alerts WHERE kind='recovered'") != []


def test_gap_sweep_skips_unknown_devices(proc):
    # A state with no readings yet has last_seen None and must not alert.
    proc.states["never-seen"].last_seen = None
    proc.gap_sweep()
    assert _rows(proc, "SELECT COUNT(*) FROM alerts")[0][0] == 0


# -- fleet_snapshot ----------------------------------------------------------

def test_fleet_snapshot_empty_fleet(proc):
    proc.fleet_snapshot()
    proc.db.commit()
    row = _rows(proc, "SELECT active_devices, avg_temp_c, total_power_w, "
                      "alerts_last_5min FROM fleet_stats")[0]
    assert row == (0, None, 0.0, 0)


def test_fleet_snapshot_counts_active_devices(proc):
    proc.detect(_reading(temperature_c=22.0))
    proc.detect(_reading(device_id="kitchen-plug", power_w=60.0))
    proc.fleet_snapshot()
    proc.db.commit()
    row = _rows(proc, "SELECT active_devices, avg_temp_c, total_power_w "
                      "FROM fleet_stats")[0]
    assert row[0] == 2
    assert row[1] == pytest.approx(22.0)
    assert row[2] == pytest.approx(60.0)


def test_fleet_snapshot_ignores_offline_devices(proc):
    proc.detect(_reading(temperature_c=22.0))
    proc.detect(_reading(device_id="kitchen-plug", power_w=60.0))
    proc.states["kitchen-plug"].offline = True
    proc.fleet_snapshot()
    proc.db.commit()
    row = _rows(proc, "SELECT active_devices, total_power_w "
                      "FROM fleet_stats")[0]
    assert row == (1, 0.0)


def test_fleet_snapshot_counts_recent_alerts(proc):
    proc.raise_alert("kitchen-plug", "zscore", "power_w", 350.0, 4.2, "spike")
    proc.fleet_snapshot()
    proc.db.commit()
    assert _rows(proc, "SELECT alerts_last_5min FROM fleet_stats")[0][0] == 1

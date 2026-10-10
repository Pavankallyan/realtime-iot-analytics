"""Unit tests for the stream-processor detection logic (src/processor.py).

Runs entirely in-process: no simulator, no sockets. A Processor instance is
built against a temporary SQLite DB via monkeypatched config paths.
"""

import time
from datetime import datetime, timezone

import pytest

import config
from src.processor import DeviceState, Processor


@pytest.fixture()
def proc(tmp_path, monkeypatch):
    """Processor writing to a fresh temp-dir SQLite DB."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "iot.db")
    p = Processor(port=19999, interval=1.0)
    yield p
    p.db.close()


def _reading(device_id="dev-1", **metrics):
    r = {"device_id": device_id,
         "ts": datetime.now(timezone.utc).isoformat()}
    r.update(metrics)
    return r


def _alerts(proc, kind=None):
    if kind:
        return proc.db.execute(
            "SELECT kind, metric, message FROM alerts WHERE kind=?",
            (kind,)).fetchall()
    return proc.db.execute(
        "SELECT kind, metric, message FROM alerts").fetchall()


def _noisy_temp_series(n, base=22.0, wiggle=0.1):
    """Values with enough variance for z-scores to be meaningful."""
    return [base + (wiggle if i % 2 else -wiggle) for i in range(n)]


# -- DeviceState.stats -----------------------------------------------------

def test_stats_needs_warmup():
    state = DeviceState()
    assert state.stats("temperature_c") == (None, None)
    for i in range(config.MIN_WINDOW_FOR_STATS - 1):
        state.window.append({"temperature_c": 20.0})
    assert state.stats("temperature_c") == (None, None)


def test_stats_mean_std():
    state = DeviceState()
    for _ in range(20):
        state.window.append({"temperature_c": 20.0})
    for _ in range(20):
        state.window.append({"temperature_c": 30.0})
    mean, std = state.stats("temperature_c")
    assert mean == pytest.approx(25.0)
    assert std == pytest.approx(5.0)


# -- z-score detection is edge-triggered ------------------------------------

def test_zscore_detects_spike_and_fires_once_per_episode(proc):
    for v in _noisy_temp_series(25):
        assert proc.detect(_reading(temperature_c=v)) is False
    assert _alerts(proc, "zscore") == []

    spike = _reading(temperature_c=30.0)
    assert proc.detect(spike) is True
    assert len(_alerts(proc, "zscore")) == 1
    kind, metric, message = _alerts(proc, "zscore")[0]
    assert metric == "temperature_c"
    assert "temperature_c=" in message

    # same episode still anomalous but no alert storm
    assert proc.detect(spike) is True
    assert len(_alerts(proc, "zscore")) == 1

    # back to normal clears the edge; a new spike re-arms and alerts again
    assert proc.detect(_reading(temperature_c=22.0)) is False
    assert proc.detect(spike) is True
    assert len(_alerts(proc, "zscore")) == 2


def test_zero_variance_window_never_zscores(proc):
    """A perfectly flat window has std == 0 -> z-scores are not trusted."""
    for _ in range(30):
        assert proc.detect(_reading(temperature_c=21.5)) is False
    assert _alerts(proc, "zscore") == []


# -- stuck-sensor (flatline) detection --------------------------------------

def test_flatline_fires_once_after_window(proc):
    for _ in range(config.FLATLINE_WINDOW + 5):
        proc.detect(_reading(temperature_c=21.5))
    stuck = _alerts(proc, "stuck")
    assert len(stuck) == 1
    assert stuck[0][1] == "temperature_c"

    # further identical readings: still one alert (edge-triggered)
    for _ in range(10):
        proc.detect(_reading(temperature_c=21.5))
    assert len(_alerts(proc, "stuck")) == 1


# -- offline / recovered -----------------------------------------------------

def test_gap_sweep_marks_offline_once(proc):
    proc.detect(_reading(temperature_c=22.0))
    state = proc.states["dev-1"]
    state.last_seen = time.time() - 100  # way past GAP_MULTIPLIER * interval

    proc.gap_sweep()
    assert state.offline is True
    offline = _alerts(proc, "offline")
    assert len(offline) == 1
    assert offline[0][1] is None

    proc.gap_sweep()  # already offline: no duplicate alert
    assert len(_alerts(proc, "offline")) == 1


def test_device_recovery_after_offline(proc):
    proc.detect(_reading(temperature_c=22.0))
    state = proc.states["dev-1"]
    state.last_seen = time.time() - 100
    proc.gap_sweep()
    assert state.offline is True

    proc.detect(_reading(temperature_c=22.0))
    assert state.offline is False
    recovered = _alerts(proc, "recovered")
    assert len(recovered) == 1


# -- persistence --------------------------------------------------------------

def test_store_reading_and_fleet_snapshot(proc):
    for _ in range(25):
        r = _reading(device_id="kitchen-plug", power_w=60.0)
        proc.store_reading(r, proc.detect(r))
    proc.fleet_snapshot()
    row = proc.db.execute(
        "SELECT active_devices, avg_temp_c, total_power_w, alerts_last_5min"
        " FROM fleet_stats").fetchone()
    assert row is not None
    active, avg_temp, total_power, alerts = row
    assert active == 1            # kitchen-plug is in FLEET and reporting
    assert avg_temp is None       # plugs don't report temperature
    assert total_power == pytest.approx(60.0)  # latest reading per device
    assert alerts >= 0
    n = proc.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    assert n == 25

"""Unit tests for src/simulator.py — DeviceSim signal + fault behaviour.

Faults are forced by setting ``dev.fault`` directly, and compared against a
seed-identical twin running with no fault, so assertions are exact rather
than statistical. ``anomaly_prob=0`` keeps random fault starts off, which
also keeps the twin's rng stream aligned (a set fault short-circuits the
start-fault draw).
"""

import random

import pytest

from src.models import FLEET, DeviceProfile
from src.simulator import DeviceSim

THERMO = FLEET[0]  # living-room-thermostat: temperature + humidity
PLUG = FLEET[2]    # kitchen-plug: power only


def make(profile=THERMO, seed=42):
    return DeviceSim(profile, random.Random(seed))


def twin(dev):
    """A second DeviceSim with the same profile and rng seed."""
    return DeviceSim(dev.profile, random.Random(42))


def noop(dev):
    """Give a device a no-op fault so it skips the fault-start rng draw
    (matching a device under test) while its values pass through unchanged."""
    dev.fault = {"kind": "noop", "ticks_left": 10 ** 9}
    return dev


def values_only(reading):
    return {k: v for k, v in reading.items() if k not in ("ts", "injected_fault")}


# -- schema / baseline -----------------------------------------------------

def test_emit_schema():
    r = make().emit(0.0)
    assert r["device_id"] == "living-room-thermostat"
    assert r["device_type"] == "thermostat"
    assert r["location"] == "Living Room"
    assert r["injected_fault"] is None
    assert isinstance(r["temperature_c"], float)
    assert isinstance(r["humidity_pct"], float)
    assert r["power_w"] is None          # thermostat reports no power
    assert r["vibration_g"] is None      # ... and no vibration


def test_non_applicable_metrics_are_null():
    r = make(PLUG).emit(0.0)
    assert r["temperature_c"] is None
    assert r["humidity_pct"] is None
    assert r["vibration_g"] is None
    assert isinstance(r["power_w"], float)


def test_seeded_determinism():
    a, b = make(), make()
    seq_a = [values_only(a.emit(0.0)) for _ in range(50)]
    seq_b = [values_only(b.emit(0.0)) for _ in range(50)]
    assert seq_a == seq_b


def test_fleet_devices_emit_own_ids():
    devs = [DeviceSim(p, random.Random(i)) for i, p in enumerate(FLEET)]
    ids = {d.emit(0.0)["device_id"] for d in devs}
    assert ids == {p.device_id for p in FLEET}
    assert len(devs) == 8


# -- signal ranges ----------------------------------------------------------

def test_temperature_stays_near_baseline():
    dev = make()
    temps = [dev.emit(0.0)["temperature_c"] for _ in range(240)]
    assert all(abs(t - THERMO.base_temp_c) <= 5.0 for t in temps)


def test_humidity_is_clamped():
    dev = make()
    hums = [dev.emit(0.0)["humidity_pct"] for _ in range(200)]
    assert all(25.0 <= h <= 75.0 for h in hums)


def test_power_and_vibration_never_negative():
    dev = make(PLUG)
    assert all(dev.emit(0.0)["power_w"] >= 0 for _ in range(200))
    vib = make(FLEET[5])  # hvac-vibration
    assert all(vib.emit(0.0)["vibration_g"] >= 0 for _ in range(200))


# -- fault injection ---------------------------------------------------------

def test_stuck_flatlines():
    dev, ref = make(), twin(make())
    first = dev.emit(0.0)
    dev.fault = {"kind": "stuck", "ticks_left": 5}
    frozen = dev.emit(0.0)
    assert values_only(frozen) == values_only(first)
    assert frozen["injected_fault"] == "stuck"


def test_stuck_on_first_tick_does_not_crash():
    dev = make()
    dev.fault = {"kind": "stuck", "ticks_left": 3}
    r = dev.emit(0.0)
    assert r is not None and r["injected_fault"] == "stuck"


def test_spike_raises_temperature_only():
    dev = make()
    dev.emit(0.0)  # warm up last_values
    clean = twin(make())
    clean.emit(0.0)
    dev.fault = {"kind": "spike", "ticks_left": 3}
    noop(clean)
    spiked, plain = dev.emit(0.0), clean.emit(0.0)
    delta = spiked["temperature_c"] - plain["temperature_c"]
    assert 6.0 <= delta <= 10.0
    assert spiked["humidity_pct"] == plain["humidity_pct"]
    assert spiked["injected_fault"] == "spike"


def test_surge_multiplies_power_only():
    dev, clean = make(PLUG), twin(make(PLUG))
    dev.fault = {"kind": "surge", "ticks_left": 3}
    noop(clean)
    surged, plain = dev.emit(0.0), clean.emit(0.0)
    ratio = surged["power_w"] / plain["power_w"]
    assert 3.0 <= ratio <= 5.0
    assert surged["injected_fault"] == "surge"


def test_dropout_returns_none():
    dev = make()
    dev.fault = {"kind": "dropout", "ticks_left": 3}
    assert dev.emit(0.0) is None
    assert dev.emit(0.0) is None
    assert dev.emit(0.0) is None  # still silent while the fault lasts


def test_fault_expires_after_its_duration():
    dev = make()
    dev.fault = {"kind": "spike", "ticks_left": 1}
    dev.emit(0.0)
    assert dev.fault is None
    assert dev.emit(0.0)["injected_fault"] is None


def test_no_fault_starts_at_zero_probability():
    dev = make()
    assert all(dev.emit(0.0)["injected_fault"] is None for _ in range(200))

"""Unit tests for the device-fleet definition (src/models.py).

The fleet is the ground truth the simulator emits and the dashboard renders,
so its invariants are worth pinning: unique ids, valid types, metrics that
exist, and sane baselines.
"""

from src.models import ALL_METRICS, FLEET, METRIC_META, DeviceProfile

VALID_TYPES = {"thermostat", "smart_plug", "vibration_sensor", "multisensor"}


def _by_id(device_id):
    return next(d for d in FLEET if d.device_id == device_id)


# -- fleet shape ------------------------------------------------------------

def test_fleet_has_eight_devices():
    assert len(FLEET) == 8


def test_device_ids_unique():
    ids = [d.device_id for d in FLEET]
    assert len(set(ids)) == len(ids)


def test_device_types_valid():
    assert all(d.device_type in VALID_TYPES for d in FLEET)


def test_every_type_represented():
    types = {d.device_type for d in FLEET}
    assert types == VALID_TYPES


def test_locations_nonempty():
    assert all(d.location.strip() for d in FLEET)


def test_fleet_covers_expected_devices():
    ids = {d.device_id for d in FLEET}
    assert ids == {
        "living-room-thermostat", "bedroom-thermostat",
        "kitchen-plug", "hvac-plug", "washer-plug", "hvac-vibration",
        "hallway-multisensor", "garage-multisensor",
    }


# -- per-type metric contracts ----------------------------------------------

def test_metrics_are_known():
    for d in FLEET:
        assert set(d.metrics) <= set(ALL_METRICS)


def test_thermostats_report_temp_and_humidity():
    for d in FLEET:
        if d.device_type == "thermostat":
            assert set(d.metrics) == {"temperature_c", "humidity_pct"}


def test_smart_plugs_report_power_only():
    for d in FLEET:
        if d.device_type == "smart_plug":
            assert d.metrics == ("power_w",)


def test_vibration_sensor_reports_vibration_only():
    vib = _by_id("hvac-vibration")
    assert vib.metrics == ("vibration_g",)


def test_multisensors_report_three_metrics():
    for d in FLEET:
        if d.device_type == "multisensor":
            assert set(d.metrics) == {
                "temperature_c", "humidity_pct", "power_w"}


def test_every_metric_emitted_by_some_device():
    emitted = set()
    for d in FLEET:
        emitted.update(d.metrics)
    assert emitted == set(ALL_METRICS)


# -- baselines ---------------------------------------------------------------

def test_temp_baselines_in_room_range():
    for d in FLEET:
        if "temperature_c" in d.metrics:
            assert 15.0 <= d.base_temp_c <= 25.0


def test_garage_cooler_than_living_room():
    assert (_by_id("garage-multisensor").base_temp_c
            < _by_id("living-room-thermostat").base_temp_c)


def test_hvac_plug_highest_idle_draw():
    plugs = [d for d in FLEET if d.device_type == "smart_plug"]
    assert max(plugs, key=lambda d: d.base_power_w).device_id == "hvac-plug"


def test_power_baselines_positive():
    for d in FLEET:
        if "power_w" in d.metrics:
            assert d.base_power_w > 0


# -- metric metadata ----------------------------------------------------------

def test_metric_meta_covers_all_metrics():
    assert set(METRIC_META) == set(ALL_METRICS)


def test_metric_meta_labels_and_units():
    assert METRIC_META["temperature_c"] == {"label": "Temperature", "unit": "°C"}
    assert METRIC_META["humidity_pct"] == {"label": "Humidity", "unit": "%"}
    assert METRIC_META["power_w"] == {"label": "Power", "unit": "W"}
    assert METRIC_META["vibration_g"] == {"label": "Vibration", "unit": "g"}


# -- DeviceProfile defaults ----------------------------------------------------

def test_profile_defaults():
    p = DeviceProfile("x", "thermostat", "Nowhere")
    assert p.base_temp_c == 22.0
    assert p.base_power_w == 40.0
    assert p.metrics == ()

"""Device fleet definition for the IoT simulator.

Each device has a type; the type decides which metrics it reports.
Metrics not applicable to a device are emitted as null.
"""

from dataclasses import dataclass, field


@dataclass
class DeviceProfile:
    device_id: str
    device_type: str                      # thermostat | smart_plug | vibration_sensor | multisensor
    location: str
    base_temp_c: float = 22.0             # per-device temperature offset baseline
    base_power_w: float = 40.0            # idle draw for plugs
    metrics: tuple = field(default_factory=tuple)


FLEET = [
    DeviceProfile("living-room-thermostat", "thermostat", "Living Room",
                  base_temp_c=22.5, metrics=("temperature_c", "humidity_pct")),
    DeviceProfile("bedroom-thermostat", "thermostat", "Bedroom",
                  base_temp_c=21.0, metrics=("temperature_c", "humidity_pct")),
    DeviceProfile("kitchen-plug", "smart_plug", "Kitchen",
                  base_power_w=60.0, metrics=("power_w",)),
    DeviceProfile("hvac-plug", "smart_plug", "Utility Closet",
                  base_power_w=120.0, metrics=("power_w",)),
    DeviceProfile("washer-plug", "smart_plug", "Laundry",
                  base_power_w=25.0, metrics=("power_w",)),
    DeviceProfile("hvac-vibration", "vibration_sensor", "Utility Closet",
                  metrics=("vibration_g",)),
    DeviceProfile("hallway-multisensor", "multisensor", "Hallway",
                  base_temp_c=22.0, base_power_w=15.0,
                  metrics=("temperature_c", "humidity_pct", "power_w")),
    DeviceProfile("garage-multisensor", "multisensor", "Garage",
                  base_temp_c=18.5, base_power_w=15.0,
                  metrics=("temperature_c", "humidity_pct", "power_w")),
]

ALL_METRICS = ("temperature_c", "humidity_pct", "power_w", "vibration_g")

# Human-friendly labels / units for the dashboard.
METRIC_META = {
    "temperature_c": {"label": "Temperature", "unit": "°C"},
    "humidity_pct": {"label": "Humidity", "unit": "%"},
    "power_w": {"label": "Power", "unit": "W"},
    "vibration_g": {"label": "Vibration", "unit": "g"},
}

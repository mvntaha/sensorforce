"""The sensor interface shared by the real Raspberry Pi drivers and the simulator.

One :class:`Reading` describes one poll of one zone. A reading carries values
*and* the set of sensors that failed to produce them, and the two are kept
consistent by construction: a missing value always has a matching fault, and a
present value never does.

That invariant is the reason a faulty sensor can never be reported as Fresh.
It matches the CHECK constraints on the ``readings`` table, so the rule is
enforced twice: once in Python when the reading is built, and once in SQLite
when it is stored.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

#: Sensors that can independently fail. The reed switch is absent on purpose:
#: a disconnected reed switch is indistinguishable from a closed door, so the
#: data model has no fault flag for it.
SENSOR_FIELDS = ("temp", "humidity", "gas")

#: Physically possible ranges. Anything outside these is a fault, not a value.
TEMP_RANGE_C = (-20.0, 60.0)
HUMIDITY_RANGE = (0.0, 100.0)
GAS_RAW_RANGE = (-32768, 32767)

#: Shown whenever a required sensor is unavailable. Never "Fresh".
FAULT_STATUS = "Sensor Fault/Data Unavailable"


def utc_now_iso() -> str:
    """Return the current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_plausible(name: str, value: float | int | None) -> bool:
    """True when ``value`` is inside the physically possible range for ``name``."""
    if value is None:
        return False
    ranges = {"temp": TEMP_RANGE_C, "humidity": HUMIDITY_RANGE, "gas": GAS_RAW_RANGE}
    low, high = ranges[name]
    return low <= value <= high


@dataclass(frozen=True, slots=True)
class Reading:
    """One poll of one zone's sensors.

    A value is ``None`` exactly when its sensor is named in ``faults``.
    """

    ts: str = field(default_factory=utc_now_iso)
    temp_c: float | None = None
    humidity: float | None = None
    gas_raw: int | None = None
    door_open: bool = False
    faults: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = set(self.faults) - set(SENSOR_FIELDS)
        if unknown:
            raise ValueError(f"unknown sensor name(s) in faults: {sorted(unknown)}")
        for name, value in (
            ("temp", self.temp_c),
            ("humidity", self.humidity),
            ("gas", self.gas_raw),
        ):
            faulted = name in self.faults
            if faulted and value is not None:
                raise ValueError(f"{name} is marked faulty but carries the value {value!r}")
            if not faulted and value is None:
                raise ValueError(f"{name} has no value but is not marked faulty")
            if value is not None and not is_plausible(name, value):
                raise ValueError(f"{name} value {value!r} is outside the possible range")

    @property
    def temp_ok(self) -> bool:
        """True when the temperature sensor produced a usable value."""
        return "temp" not in self.faults

    @property
    def humidity_ok(self) -> bool:
        """True when the humidity sensor produced a usable value."""
        return "humidity" not in self.faults

    @property
    def gas_ok(self) -> bool:
        """True when the gas sensor produced a usable value."""
        return "gas" not in self.faults

    @property
    def healthy(self) -> bool:
        """True when every sensor reported."""
        return not self.faults

    def to_row(self, zone_id: int) -> dict[str, object]:
        """Return this reading as bound parameters for the ``readings`` table."""
        return {
            "zone_id": zone_id,
            "ts": self.ts,
            "temp_c": self.temp_c,
            "humidity": self.humidity,
            "gas_raw": self.gas_raw,
            "door_open": int(self.door_open),
            "temp_ok": int(self.temp_ok),
            "hum_ok": int(self.humidity_ok),
            "gas_ok": int(self.gas_ok),
        }

    @classmethod
    def faulted(cls, *, ts: str | None = None, door_open: bool = False) -> Reading:
        """Build a reading in which every sensor failed.

        Used when a whole bus goes away, for instance when I2C stops responding.
        """
        return cls(
            ts=ts or utc_now_iso(),
            door_open=door_open,
            faults=frozenset(SENSOR_FIELDS),
        )


class Sensor(ABC):
    """A source of zone readings.

    Implementations must not raise from :meth:`read`: a hardware error is
    reported as a fault so the monitor loop keeps running.
    """

    @abstractmethod
    def read(self) -> Reading:
        """Return the current reading for this zone."""

    def close(self) -> None:  # noqa: B027 - optional hook; the simulator holds nothing
        """Release any hardware handles. Safe to call more than once."""

    def __enter__(self) -> Sensor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def get_sensor(mode: str | None = None, **kwargs: object) -> Sensor:
    """Return the backend named by ``mode`` or by ``FG_SENSOR_MODE``.

    The real backend is imported lazily so that the project runs on a laptop
    where the Raspberry Pi libraries are not installed.
    """
    if mode is None:
        from core.config import get_settings

        mode = get_settings().sensor_mode

    if mode == "sim":
        from core.sensors.sim import SimulatedSensor

        return SimulatedSensor(**kwargs)  # type: ignore[arg-type]
    if mode == "real":
        from core.sensors.real import RealSensor

        return RealSensor(**kwargs)  # type: ignore[arg-type]
    raise ValueError(f"unknown sensor mode {mode!r}; expected 'real' or 'sim'")

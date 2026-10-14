"""Sensor simulator: the backend every test and every evidence run uses.

The simulator is deterministic. Given a scenario and a seed it produces the
same sequence of readings on every machine, so a test matrix row captured on a
laptop means the same thing as one captured on the Pi.

Each scenario is described by a :class:`ScenarioProfile` rather than by code, so
the expected behaviour can be read off the table below and asserted directly in
tests instead of being duplicated as magic numbers.

Scenarios
---------
``normal``               everything inside range, door shut
``high_temp``            temperature well above any refrigeration limit
``high_humidity``        humidity well above any category limit
``gas_spike``            two normal readings, then a sustained gas rise
``door_open``            door open from the first reading and left open
``sensor_disconnected``  the DHT11 never answers; the gas sensor still does
``dht_intermittent``     the DHT11 answers on even polls and fails on odd ones
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.sensors.base import Reading, Sensor, utc_now_iso

logger = logging.getLogger(__name__)

SCENARIOS = (
    "normal",
    "high_temp",
    "high_humidity",
    "gas_spike",
    "door_open",
    "sensor_disconnected",
    "dht_intermittent",
)

#: Readings a scenario emits normally before its abnormal behaviour starts.
#: Chosen so the "three consecutive bad readings" rule is exercised rather than
#: satisfied instantly.
GAS_SPIKE_DELAY_TICKS = 2


@dataclass(frozen=True, slots=True)
class ScenarioProfile:
    """The target values a scenario aims for, before per-tick noise."""

    temp_c: float = 4.0
    humidity: float = 55.0
    gas_raw: int = 1200
    door_open: bool = False
    #: Sensors that never answer in this scenario.
    always_faulty: frozenset[str] = frozenset()
    #: Sensors that answer only on even ticks.
    intermittent: frozenset[str] = frozenset()
    #: Ticks to behave normally before the abnormal values begin.
    onset_tick: int = 0
    #: Values used from ``onset_tick`` onwards, when the scenario ramps.
    onset_gas_raw: int | None = None
    temp_noise: float = 0.3
    humidity_noise: float = 2.0
    gas_noise: int = 40


PROFILES: dict[str, ScenarioProfile] = {
    "normal": ScenarioProfile(),
    "high_temp": ScenarioProfile(temp_c=14.5),
    "high_humidity": ScenarioProfile(humidity=88.0),
    "gas_spike": ScenarioProfile(
        onset_tick=GAS_SPIKE_DELAY_TICKS,
        onset_gas_raw=3200,
    ),
    "door_open": ScenarioProfile(door_open=True),
    "sensor_disconnected": ScenarioProfile(always_faulty=frozenset({"temp", "humidity"})),
    "dht_intermittent": ScenarioProfile(intermittent=frozenset({"temp", "humidity"})),
}


def _profile_for(scenario: str) -> ScenarioProfile:
    try:
        return PROFILES[scenario]
    except KeyError:
        raise ValueError(
            f"unknown scenario {scenario!r}; expected one of {', '.join(SCENARIOS)}"
        ) from None


@dataclass(slots=True)
class SimulatedSensor(Sensor):
    """A scripted :class:`~core.sensors.base.Sensor` with fault injection.

    ``scenario`` may be changed at runtime, either by assigning to the
    attribute or by writing a scenario name into ``control_file``. The control
    file is re-read on every poll, which lets a live demo switch from ``normal``
    to ``high_temp`` without restarting the monitor.
    """

    scenario: str = "normal"
    seed: int = 0
    control_file: Path | None = None
    #: Injected for tests; defaults to real UTC time.
    clock: Callable[[], datetime] | None = None
    tick: int = field(default=0, init=False)
    _random: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _profile_for(self.scenario)
        if self.control_file is not None:
            self.control_file = Path(self.control_file)
        self._random = random.Random(self.seed)  # noqa: S311 - simulation, not security

    # -- scenario selection ------------------------------------------------
    def _active_scenario(self) -> str:
        """Return the scenario, honouring the control file when it holds one."""
        if self.control_file is None:
            return self.scenario
        try:
            requested = self.control_file.read_text(encoding="utf-8").strip()
        except OSError:
            return self.scenario
        if requested in PROFILES:
            if requested != self.scenario:
                logger.info("simulator scenario switched to %s by control file", requested)
                self.scenario = requested
            return requested
        if requested:
            logger.warning("control file holds unknown scenario %r; ignoring", requested)
        return self.scenario

    def _timestamp(self) -> str:
        if self.clock is None:
            return utc_now_iso()
        return self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # -- reading generation ------------------------------------------------
    def _jitter(self, centre: float, spread: float) -> float:
        return round(centre + self._random.uniform(-spread, spread), 2)

    def _faulty_sensors(self, profile: ScenarioProfile) -> frozenset[str]:
        faults = set(profile.always_faulty)
        if profile.intermittent and self.tick % 2 == 1:
            faults |= set(profile.intermittent)
        return frozenset(faults)

    def read(self) -> Reading:
        """Return the next scripted reading and advance the tick counter."""
        profile = _profile_for(self._active_scenario())
        faults = self._faulty_sensors(profile)
        started = self.tick >= profile.onset_tick

        gas_target = profile.gas_raw
        if started and profile.onset_gas_raw is not None:
            gas_target = profile.onset_gas_raw

        reading = Reading(
            ts=self._timestamp(),
            temp_c=None if "temp" in faults else self._jitter(profile.temp_c, profile.temp_noise),
            humidity=(
                None
                if "humidity" in faults
                else self._jitter(profile.humidity, profile.humidity_noise)
            ),
            gas_raw=(
                None if "gas" in faults else int(self._jitter(gas_target, float(profile.gas_noise)))
            ),
            door_open=profile.door_open,
            faults=faults,
        )
        self.tick += 1
        return reading

    def reset(self) -> None:
        """Rewind to the first tick and restore the seeded random sequence."""
        self.tick = 0
        self._random = random.Random(self.seed)  # noqa: S311 - simulation, not security


def stepped_clock(start: datetime | None = None, step_s: int = 1) -> Callable[[], datetime]:
    """Return a clock that advances ``step_s`` seconds each time it is called.

    Gives readings distinct, ordered timestamps in tests without real sleeping.
    """
    current = start or datetime(2026, 9, 24, 10, 0, 0, tzinfo=UTC)
    state = {"now": current - timedelta(seconds=step_s)}

    def _tick() -> datetime:
        state["now"] = state["now"] + timedelta(seconds=step_s)
        return state["now"]

    return _tick

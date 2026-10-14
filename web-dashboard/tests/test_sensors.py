"""Tests for the sensor interface and all seven simulator scenarios.

Every scenario is asserted end to end: the reading it produces, the faults it
injects, and the fact that the resulting row is accepted by the real
``readings`` table. The last part matters because the Python invariant and the
SQL CHECK constraints have to agree.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.sensors.base import Reading, Sensor, get_sensor, utc_now_iso
from core.sensors.sim import PROFILES, SCENARIOS, SimulatedSensor, stepped_clock


def drain(sensor: Sensor, count: int) -> list[Reading]:
    """Return ``count`` consecutive readings."""
    return [sensor.read() for _ in range(count)]


class TestReading:
    def test_a_healthy_reading_reports_every_sensor(self) -> None:
        reading = Reading(temp_c=4.0, humidity=55.0, gas_raw=1200)
        assert reading.healthy is True
        assert (reading.temp_ok, reading.humidity_ok, reading.gas_ok) == (True, True, True)

    def test_a_missing_value_must_be_declared_as_a_fault(self) -> None:
        with pytest.raises(ValueError, match="not marked faulty"):
            Reading(temp_c=None, humidity=55.0, gas_raw=1200)

    def test_a_fault_must_not_carry_a_value(self) -> None:
        with pytest.raises(ValueError, match="carries the value"):
            Reading(temp_c=4.0, humidity=55.0, gas_raw=1200, faults=frozenset({"temp"}))

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"temp_c": 120.0, "humidity": 55.0, "gas_raw": 1200}, "outside the possible range"),
            ({"temp_c": 4.0, "humidity": 140.0, "gas_raw": 1200}, "outside the possible range"),
            ({"temp_c": 4.0, "humidity": 55.0, "gas_raw": 99999}, "outside the possible range"),
        ],
    )
    def test_impossible_values_are_rejected(self, kwargs: dict, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            Reading(**kwargs)

    def test_unknown_fault_names_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown sensor"):
            Reading(temp_c=4.0, humidity=55.0, gas_raw=1200, faults=frozenset({"laser"}))

    def test_faulted_marks_every_sensor(self) -> None:
        reading = Reading.faulted()
        assert reading.healthy is False
        assert (reading.temp_c, reading.humidity, reading.gas_raw) == (None, None, None)

    def test_to_row_matches_the_readings_table(self) -> None:
        row = Reading(temp_c=4.0, humidity=55.0, gas_raw=1200, door_open=True).to_row(zone_id=7)
        assert row["zone_id"] == 7
        assert row["door_open"] == 1
        assert row["temp_ok"] == 1

    def test_timestamps_are_utc_iso8601(self) -> None:
        assert utc_now_iso().endswith("Z")
        datetime.strptime(utc_now_iso(), "%Y-%m-%dT%H:%M:%SZ")


class TestScenarioCoverage:
    def test_every_documented_scenario_has_a_profile(self) -> None:
        assert set(SCENARIOS) == set(PROFILES)

    def test_there_are_exactly_seven(self) -> None:
        assert len(SCENARIOS) == 7

    def test_an_unknown_scenario_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown scenario"):
            SimulatedSensor(scenario="teleport")


class TestScenarios:
    def test_normal_stays_inside_range(self) -> None:
        for reading in drain(SimulatedSensor(scenario="normal"), 10):
            assert reading.healthy
            assert 3.0 <= reading.temp_c <= 5.0
            assert 50.0 <= reading.humidity <= 60.0
            assert reading.door_open is False

    def test_high_temp_is_above_any_refrigeration_limit(self) -> None:
        readings = drain(SimulatedSensor(scenario="high_temp"), 5)
        assert all(r.temp_c > 10.0 for r in readings)
        assert all(r.healthy for r in readings)

    def test_high_humidity_is_above_any_category_limit(self) -> None:
        readings = drain(SimulatedSensor(scenario="high_humidity"), 5)
        assert all(r.humidity > 80.0 for r in readings)

    def test_gas_spike_starts_normal_then_rises(self) -> None:
        """Two quiet readings first, so the three-in-a-row rule is exercised."""
        readings = drain(SimulatedSensor(scenario="gas_spike"), 6)
        assert all(r.gas_raw < 2000 for r in readings[:2])
        assert all(r.gas_raw > 3000 for r in readings[2:])
        assert all(r.healthy for r in readings)

    def test_door_open_stays_open(self) -> None:
        assert all(r.door_open for r in drain(SimulatedSensor(scenario="door_open"), 5))

    def test_sensor_disconnected_never_produces_a_temperature(self) -> None:
        """The case that must never be shown as Fresh."""
        for reading in drain(SimulatedSensor(scenario="sensor_disconnected"), 5):
            assert reading.healthy is False
            assert reading.temp_c is None
            assert reading.humidity is None
            assert reading.temp_ok is False
            # The gas sensor is on a different bus and keeps working.
            assert reading.gas_raw is not None

    def test_dht_intermittent_alternates(self) -> None:
        readings = drain(SimulatedSensor(scenario="dht_intermittent"), 6)
        assert [r.temp_ok for r in readings] == [True, False, True, False, True, False]
        assert all(r.gas_ok for r in readings)


class TestDeterminism:
    def test_the_same_seed_gives_the_same_sequence(self) -> None:
        a = [r.temp_c for r in drain(SimulatedSensor(scenario="normal", seed=7), 5)]
        b = [r.temp_c for r in drain(SimulatedSensor(scenario="normal", seed=7), 5)]
        assert a == b

    def test_different_seeds_diverge(self) -> None:
        a = [r.temp_c for r in drain(SimulatedSensor(scenario="normal", seed=1), 5)]
        b = [r.temp_c for r in drain(SimulatedSensor(scenario="normal", seed=2), 5)]
        assert a != b

    def test_reset_rewinds(self) -> None:
        sensor = SimulatedSensor(scenario="gas_spike", seed=3)
        first = [r.gas_raw for r in drain(sensor, 4)]
        sensor.reset()
        assert sensor.tick == 0
        assert [r.gas_raw for r in drain(sensor, 4)] == first

    def test_the_clock_can_be_injected(self) -> None:
        sensor = SimulatedSensor(scenario="normal", clock=stepped_clock(step_s=1))
        stamps = [r.ts for r in drain(sensor, 3)]
        assert stamps == [
            "2026-09-24T10:00:00Z",
            "2026-09-24T10:00:01Z",
            "2026-09-24T10:00:02Z",
        ]

    def test_a_naive_free_running_clock_still_produces_utc(self) -> None:
        fixed = datetime(2026, 9, 24, 12, 30, 0, tzinfo=UTC)
        sensor = SimulatedSensor(clock=lambda: fixed)
        assert sensor.read().ts == "2026-09-24T12:30:00Z"


class TestControlFile:
    def test_scenario_switches_while_running(self, tmp_path: Path) -> None:
        control = tmp_path / "scenario.txt"
        control.write_text("normal", encoding="utf-8")
        sensor = SimulatedSensor(scenario="normal", control_file=control)
        assert sensor.read().temp_c < 6.0

        control.write_text("high_temp", encoding="utf-8")
        assert sensor.read().temp_c > 10.0

    def test_an_unknown_name_is_ignored(self, tmp_path: Path) -> None:
        control = tmp_path / "scenario.txt"
        control.write_text("nonsense", encoding="utf-8")
        sensor = SimulatedSensor(scenario="normal", control_file=control)
        assert sensor.read().healthy is True
        assert sensor.scenario == "normal"

    def test_a_missing_file_falls_back_to_the_configured_scenario(self, tmp_path: Path) -> None:
        sensor = SimulatedSensor(scenario="door_open", control_file=tmp_path / "gone.txt")
        assert sensor.read().door_open is True


class TestFactory:
    def test_sim_mode_returns_the_simulator(self) -> None:
        assert isinstance(get_sensor("sim"), SimulatedSensor)

    def test_an_unknown_mode_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown sensor mode"):
            get_sensor("carrier-pigeon")

    def test_the_sensor_is_a_context_manager(self) -> None:
        with SimulatedSensor() as sensor:
            assert sensor.read().healthy


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_scenario_produces_storable_rows(
    conn: sqlite3.Connection, zone_id: int, scenario: str
) -> None:
    """The Python invariant and the SQL CHECK constraints must agree."""
    sensor = SimulatedSensor(scenario=scenario, clock=stepped_clock())
    for reading in drain(sensor, 6):
        conn.execute(
            "INSERT INTO readings (zone_id, ts, temp_c, humidity, gas_raw, door_open, "
            "temp_ok, hum_ok, gas_ok) VALUES (:zone_id, :ts, :temp_c, :humidity, :gas_raw, "
            ":door_open, :temp_ok, :hum_ok, :gas_ok)",
            reading.to_row(zone_id),
        )
    assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 6

"""Specification for core/validation.py, written before the logic.

These tests define what the signal layer must do. They fail with
NotImplementedError until the bodies are written; that is the point.

The behaviour they pin down comes from the iot-sensors skill:
faults are never values, a violation needs three consecutive bad readings,
clearing uses hysteresis, and the door alerts once rather than every second.
"""

from __future__ import annotations

import pytest

from core.sensors.base import Reading
from core.sensors.sim import SimulatedSensor
from core.validation import (
    CONFIRMATION_READINGS,
    ConfirmationTracker,
    DoorTracker,
    Limit,
    detect_faults,
    is_flatlined,
    moving_average,
)

# A typical fridge temperature limit, used throughout.
TEMP_LIMIT = Limit(low=0.0, high=5.0)


class TestLimit:
    def test_a_value_inside_the_range_does_not_breach(self) -> None:
        assert TEMP_LIMIT.breaches(4.0) is False

    def test_a_value_above_the_range_breaches(self) -> None:
        assert TEMP_LIMIT.breaches(9.0) is True

    def test_a_value_below_the_range_breaches(self) -> None:
        """Too cold matters too: freezing damages some categories."""
        assert TEMP_LIMIT.breaches(-3.0) is True

    @pytest.mark.parametrize("value", [0.0, 5.0])
    def test_the_boundary_itself_is_acceptable(self, value: float) -> None:
        """The range is inclusive, so a value exactly on the limit is fine."""
        assert TEMP_LIMIT.breaches(value) is False

    def test_clearing_requires_the_hysteresis_margin(self) -> None:
        """Back inside the range is not enough; it must be inside by the margin."""
        assert TEMP_LIMIT.clears(4.9, hysteresis=0.5) is False
        assert TEMP_LIMIT.clears(4.4, hysteresis=0.5) is True

    def test_clearing_applies_at_the_low_end_too(self) -> None:
        assert TEMP_LIMIT.clears(0.1, hysteresis=0.5) is False
        assert TEMP_LIMIT.clears(0.6, hysteresis=0.5) is True

    def test_a_value_still_outside_never_clears(self) -> None:
        assert TEMP_LIMIT.clears(9.0, hysteresis=0.5) is False


class TestDetectFaults:
    def test_a_healthy_reading_has_no_faults(self) -> None:
        reading = Reading(temp_c=4.0, humidity=55.0, gas_raw=1200)
        assert detect_faults(reading) == frozenset()

    def test_a_missing_temperature_is_a_temperature_fault(self) -> None:
        reading = Reading(temp_c=None, humidity=55.0, gas_raw=1200, faults=frozenset({"temp"}))
        assert detect_faults(reading) == frozenset({"temp"})

    def test_a_dead_dht11_faults_both_of_its_channels(self) -> None:
        """The DHT11 reports temperature and humidity together."""
        reading = Reading(gas_raw=1200, faults=frozenset({"temp", "humidity"}))
        assert detect_faults(reading) == frozenset({"temp", "humidity"})

    def test_the_disconnected_sensor_scenario_reports_faults(self) -> None:
        sensor = SimulatedSensor(scenario="sensor_disconnected")
        assert detect_faults(sensor.read()) == frozenset({"temp", "humidity"})

    def test_a_totally_failed_read_faults_everything(self) -> None:
        assert detect_faults(Reading.faulted()) == frozenset({"temp", "humidity", "gas"})


class TestMovingAverage:
    def test_averages_the_values_in_the_window(self) -> None:
        assert moving_average([2.0, 4.0, 6.0], window=3) == pytest.approx(4.0)

    def test_only_the_most_recent_window_counts(self) -> None:
        """Older readings must fall out, or the average never recovers."""
        assert moving_average([100.0, 100.0, 2.0, 4.0, 6.0], window=3) == pytest.approx(4.0)

    def test_a_short_history_uses_what_there_is(self) -> None:
        assert moving_average([4.0], window=5) == pytest.approx(4.0)

    def test_gaps_from_faulty_reads_are_skipped_not_counted_as_zero(self) -> None:
        """Treating a fault as 0 would drag the average toward a false alarm."""
        assert moving_average([4.0, None, 6.0], window=3) == pytest.approx(5.0)

    def test_a_window_with_no_usable_value_has_no_average(self) -> None:
        assert moving_average([None, None], window=2) is None

    def test_an_empty_history_has_no_average(self) -> None:
        assert moving_average([], window=5) is None

    def test_one_spike_does_not_move_the_average_far(self) -> None:
        """Smoothing is what stops a single bad sample changing a status."""
        steady = [4.0, 4.0, 4.0, 4.0]
        assert moving_average([*steady, 40.0], window=5) < 12.0


class TestIsFlatlined:
    def test_identical_readings_beyond_the_limit_are_a_stuck_sensor(self) -> None:
        assert is_flatlined([4.0] * 10, limit=10) is True

    def test_a_short_run_of_identical_readings_is_not_yet_stuck(self) -> None:
        assert is_flatlined([4.0] * 5, limit=10) is False

    def test_a_varying_signal_is_not_stuck(self) -> None:
        assert is_flatlined([4.0, 4.1, 4.0, 4.2] * 3, limit=10) is False

    def test_tiny_drift_still_counts_as_stuck(self) -> None:
        """Real sensors jitter; a signal flat to within a thousandth is stuck."""
        assert is_flatlined([4.0, 4.0001] * 6, limit=10, tolerance=0.001) is True

    def test_only_the_most_recent_readings_matter(self) -> None:
        assert is_flatlined([1.0, 2.0, 3.0, *([4.0] * 10)], limit=10) is True


class TestConfirmationTracker:
    def make(self) -> ConfirmationTracker:
        return ConfirmationTracker(limit=TEMP_LIMIT, required=CONFIRMATION_READINGS)

    def test_a_single_bad_reading_does_not_confirm(self) -> None:
        """One spike must never raise an alert."""
        assert self.make().update(9.0) is False

    def test_two_bad_readings_do_not_confirm(self) -> None:
        tracker = self.make()
        tracker.update(9.0)
        assert tracker.update(9.0) is False

    def test_three_consecutive_bad_readings_confirm(self) -> None:
        tracker = self.make()
        assert [tracker.update(9.0) for _ in range(3)] == [False, False, True]

    def test_the_streak_must_be_consecutive(self) -> None:
        """A good reading in the middle resets the count."""
        tracker = self.make()
        tracker.update(9.0)
        tracker.update(9.0)
        tracker.update(4.0)
        assert tracker.update(9.0) is False

    def test_a_confirmed_breach_stays_confirmed_while_it_persists(self) -> None:
        tracker = self.make()
        for _ in range(3):
            tracker.update(9.0)
        assert tracker.update(9.0) is True

    def test_a_confirmed_breach_clears_once_the_value_recovers(self) -> None:
        tracker = self.make()
        for _ in range(3):
            tracker.update(9.0)
        assert tracker.update(3.0) is False
        assert tracker.confirmed is False

    def test_a_value_on_the_limit_does_not_clear_a_breach(self) -> None:
        """Hysteresis: without it the status would flap on every poll."""
        tracker = ConfirmationTracker(limit=TEMP_LIMIT, required=3, hysteresis=0.5)
        for _ in range(3):
            tracker.update(9.0)
        assert tracker.update(4.9) is True

    def test_a_faulty_reading_breaks_the_streak(self) -> None:
        """An unknown value is not evidence of a breach."""
        tracker = self.make()
        tracker.update(9.0)
        tracker.update(9.0)
        tracker.update(None)
        assert tracker.update(9.0) is False

    def test_a_faulty_reading_does_not_clear_a_confirmed_breach(self) -> None:
        """An unknown value is not evidence the problem went away either."""
        tracker = self.make()
        for _ in range(3):
            tracker.update(9.0)
        assert tracker.update(None) is True

    def test_reset_forgets_everything(self) -> None:
        tracker = self.make()
        for _ in range(3):
            tracker.update(9.0)
        tracker.reset()
        assert tracker.confirmed is False
        assert tracker.update(9.0) is False

    def test_the_required_count_is_configurable(self) -> None:
        tracker = ConfirmationTracker(limit=TEMP_LIMIT, required=2)
        assert [tracker.update(9.0) for _ in range(2)] == [False, True]

    def test_the_high_temp_scenario_confirms_on_the_third_reading(self) -> None:
        """End to end against the simulator, as the test matrix will run it."""
        sensor = SimulatedSensor(scenario="high_temp")
        tracker = self.make()
        results = [tracker.update(sensor.read().temp_c) for _ in range(3)]
        assert results == [False, False, True]

    def test_the_normal_scenario_never_confirms(self) -> None:
        sensor = SimulatedSensor(scenario="normal")
        tracker = self.make()
        assert not any(tracker.update(sensor.read().temp_c) for _ in range(10))


class TestDoorTracker:
    def test_a_closed_door_never_alerts(self) -> None:
        tracker = DoorTracker(timeout_s=30.0)
        assert tracker.update(door_open=False, now=0.0) is False
        assert tracker.update(door_open=False, now=100.0) is False

    def test_a_brief_opening_does_not_alert(self) -> None:
        tracker = DoorTracker(timeout_s=30.0)
        tracker.update(door_open=True, now=0.0)
        assert tracker.update(door_open=True, now=10.0) is False

    def test_it_alerts_once_the_timeout_is_exceeded(self) -> None:
        tracker = DoorTracker(timeout_s=30.0)
        tracker.update(door_open=True, now=0.0)
        assert tracker.update(door_open=True, now=31.0) is True

    def test_it_alerts_only_once_per_opening(self) -> None:
        """Otherwise the buzzer retriggers on every one-second poll."""
        tracker = DoorTracker(timeout_s=30.0)
        tracker.update(door_open=True, now=0.0)
        tracker.update(door_open=True, now=31.0)
        assert tracker.update(door_open=True, now=32.0) is False
        assert tracker.update(door_open=True, now=95.0) is False

    def test_closing_the_door_rearms_it(self) -> None:
        tracker = DoorTracker(timeout_s=30.0)
        tracker.update(door_open=True, now=0.0)
        tracker.update(door_open=True, now=31.0)
        tracker.update(door_open=False, now=40.0)
        tracker.update(door_open=True, now=50.0)
        assert tracker.update(door_open=True, now=81.0) is True

    def test_the_timeout_is_measured_from_the_opening(self) -> None:
        tracker = DoorTracker(timeout_s=30.0)
        tracker.update(door_open=True, now=1000.0)
        assert tracker.update(door_open=True, now=1020.0) is False
        assert tracker.update(door_open=True, now=1031.0) is True

    def test_the_door_open_scenario_eventually_alerts(self) -> None:
        sensor = SimulatedSensor(scenario="door_open")
        tracker = DoorTracker(timeout_s=30.0)
        results = [tracker.update(sensor.read().door_open, now=float(t)) for t in range(0, 60, 10)]
        assert results.count(True) == 1

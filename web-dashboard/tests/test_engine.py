"""Specification for core/engine.py, written before the logic.

These tests encode the six-rule precedence from the SRS
scenarios. They fail with NotImplementedError until the bodies are written.

The rules, first match wins:

1. expired or storage limit exceeded         -> Check Food
2. required zone sensor faulty               -> Sensor Fault/Data Unavailable
3. temp/humidity out of range (confirmed)    -> Check Food
4. gas above abnormal (confirmed)            -> Check Food, message names the zone
5. within use_soon_days                      -> Use Soon
6. otherwise                                 -> Fresh/Normal

Precedence is tested directly, not just each rule in isolation, because the
ordering is where this gets subtly wrong.
"""

from __future__ import annotations

from datetime import date

import pytest

from core.engine import (
    CONDITIONS_RECOMMENDATION,
    DISPOSAL_RECOMMENDATION,
    DOOR_RECOMMENDATION,
    RECOMMENDATIONS,
    Assessment,
    CategoryThresholds,
    Item,
    Status,
    ZoneState,
    assess,
    days_remaining,
    effective_deadline,
    worst_status,
)

TODAY = date(2026, 9, 24)

DAIRY = CategoryThresholds(
    category="dairy",
    temp_min=0.0,
    temp_max=5.0,
    hum_min=30.0,
    hum_max=70.0,
    storage_limit_days=7,
    use_soon_days=2,
)


def make_item(**overrides: object) -> Item:
    """An item stored four days ago, expiring in six days."""
    defaults: dict[str, object] = {
        "id": 1,
        "qr_id": "FG-0001",
        "name": "Milk",
        "category": "dairy",
        "zone_id": 1,
        "storage_date": date(2026, 9, 20),
        "expiry_date": date(2026, 9, 30),
        "storage_limit_days": None,
    }
    return Item(**(defaults | overrides))  # type: ignore[arg-type]


def make_zone(**overrides: object) -> ZoneState:
    """A healthy zone with nothing breached."""
    defaults: dict[str, object] = {"zone_id": 1, "name": "Main shelf"}
    return ZoneState(**(defaults | overrides))  # type: ignore[arg-type]


class TestEffectiveDeadline:
    def test_an_explicit_expiry_date_is_used(self) -> None:
        assert effective_deadline(make_item(), DAIRY) == date(2026, 9, 30)

    def test_without_an_expiry_the_item_storage_limit_applies(self) -> None:
        item = make_item(expiry_date=None, storage_limit_days=3)
        assert effective_deadline(item, DAIRY) == date(2026, 9, 23)

    def test_without_either_the_category_limit_applies(self) -> None:
        item = make_item(expiry_date=None, storage_limit_days=None)
        assert effective_deadline(item, DAIRY) == date(2026, 9, 27)

    def test_the_earlier_of_expiry_and_storage_limit_wins(self) -> None:
        """A long printed shelf life does not survive a short storage limit."""
        item = make_item(expiry_date=date(2026, 12, 1), storage_limit_days=2)
        assert effective_deadline(item, DAIRY) == date(2026, 9, 22)


class TestDaysRemaining:
    def test_counts_whole_days_to_the_deadline(self) -> None:
        assert days_remaining(make_item(), DAIRY, TODAY) == 6

    def test_is_zero_on_the_deadline(self) -> None:
        assert days_remaining(make_item(expiry_date=TODAY), DAIRY, TODAY) == 0

    def test_goes_negative_once_passed(self) -> None:
        assert days_remaining(make_item(expiry_date=date(2026, 9, 21)), DAIRY, TODAY) == -3


class TestRule1Expiry:
    def test_an_expired_item_is_check_food(self) -> None:
        result = assess(make_item(expiry_date=date(2026, 9, 23)), make_zone(), DAIRY, TODAY)
        assert result.status is Status.CHECK
        assert result.rule == 1

    def test_an_item_on_its_deadline_is_not_yet_expired(self) -> None:
        """Expiry means past the date, not on it."""
        result = assess(make_item(expiry_date=TODAY), make_zone(), DAIRY, TODAY)
        assert result.status is not Status.CHECK

    def test_an_exceeded_storage_limit_is_check_food(self) -> None:
        item = make_item(expiry_date=None, storage_limit_days=2)
        result = assess(item, make_zone(), DAIRY, TODAY)
        assert result.status is Status.CHECK
        assert result.rule == 1

    def test_the_message_names_the_item(self) -> None:
        result = assess(make_item(expiry_date=date(2026, 9, 23)), make_zone(), DAIRY, TODAY)
        assert "Milk" in result.message
        assert result.zone_level is False


class TestRule2SensorFault:
    def test_a_faulty_temperature_sensor_gives_the_fault_status(self) -> None:
        zone = make_zone(faults=frozenset({"temp"}))
        result = assess(make_item(), zone, DAIRY, TODAY)
        assert result.status is Status.FAULT
        assert result.rule == 2

    def test_a_faulty_humidity_sensor_gives_the_fault_status(self) -> None:
        zone = make_zone(faults=frozenset({"humidity"}))
        assert assess(make_item(), zone, DAIRY, TODAY).status is Status.FAULT

    def test_a_fault_is_never_reported_as_fresh(self) -> None:
        """SRS rule 1: never show Fresh when a required sensor is faulty."""
        zone = make_zone(faults=frozenset({"temp", "humidity"}))
        result = assess(make_item(), zone, DAIRY, TODAY)
        assert result.status is not Status.FRESH

    def test_a_faulty_gas_sensor_alone_does_not_make_an_item_unknown(self) -> None:
        """Gas is zone-level; it does not determine an item's status."""
        zone = make_zone(faults=frozenset({"gas"}))
        assert assess(make_item(), zone, DAIRY, TODAY).status is Status.FRESH

    def test_the_fault_message_says_data_is_unavailable(self) -> None:
        zone = make_zone(faults=frozenset({"temp"}))
        assert "unavailable" in assess(make_item(), zone, DAIRY, TODAY).message.lower()

    def test_the_disconnected_sensor_scenario_is_never_fresh(self) -> None:
        """The scenario the test matrix requires: disconnected -> Sensor Fault."""
        zone = make_zone(faults=frozenset({"temp", "humidity"}))
        for item in (make_item(), make_item(expiry_date=date(2026, 9, 26))):
            assert assess(item, zone, DAIRY, TODAY).status is not Status.FRESH


class TestRule3TemperatureAndHumidity:
    def test_a_confirmed_temperature_breach_is_check_food(self) -> None:
        result = assess(make_item(), make_zone(temp_breach=True), DAIRY, TODAY)
        assert result.status is Status.CHECK
        assert result.rule == 3

    def test_a_confirmed_humidity_breach_is_check_food(self) -> None:
        result = assess(make_item(), make_zone(humidity_breach=True), DAIRY, TODAY)
        assert result.status is Status.CHECK
        assert result.rule == 3

    def test_an_unconfirmed_breach_changes_nothing(self) -> None:
        """Only confirmed breaches reach the engine; a clean zone stays Fresh."""
        assert assess(make_item(), make_zone(), DAIRY, TODAY).status is Status.FRESH


class TestRule4Gas:
    def test_a_confirmed_gas_breach_is_check_food(self) -> None:
        result = assess(make_item(), make_zone(gas_breach=True), DAIRY, TODAY)
        assert result.status is Status.CHECK
        assert result.rule == 4

    def test_the_gas_message_names_the_zone_not_the_item(self) -> None:
        """SRS rule 3: a gas reading is never attributed to a specific item."""
        result = assess(make_item(), make_zone(gas_breach=True), DAIRY, TODAY)
        assert "Main shelf" in result.message
        assert "Milk" not in result.message
        assert result.zone_level is True

    def test_the_gas_message_never_reports_ppm(self) -> None:
        """Gas is shown as a relative reading unless the sensor is calibrated."""
        result = assess(make_item(), make_zone(gas_breach=True), DAIRY, TODAY)
        assert "ppm" not in result.message.lower()

    def test_a_temperature_breach_takes_precedence_over_gas(self) -> None:
        zone = make_zone(temp_breach=True, gas_breach=True)
        assert assess(make_item(), zone, DAIRY, TODAY).rule == 3


class TestRule5UseSoon:
    def test_an_item_inside_the_use_soon_window_is_use_soon(self) -> None:
        result = assess(make_item(expiry_date=date(2026, 9, 25)), make_zone(), DAIRY, TODAY)
        assert result.status is Status.USE_SOON
        assert result.rule == 5

    def test_the_edge_of_the_window_counts(self) -> None:
        """use_soon_days = 2, so two days out is already Use Soon."""
        result = assess(make_item(expiry_date=date(2026, 9, 26)), make_zone(), DAIRY, TODAY)
        assert result.status is Status.USE_SOON

    def test_just_outside_the_window_is_still_fresh(self) -> None:
        result = assess(make_item(expiry_date=date(2026, 9, 27)), make_zone(), DAIRY, TODAY)
        assert result.status is Status.FRESH

    def test_the_use_soon_recommendation_is_the_srs_wording(self) -> None:
        result = assess(make_item(expiry_date=date(2026, 9, 25)), make_zone(), DAIRY, TODAY)
        assert result.recommendation == "Use the food soon"


class TestRule6Fresh:
    def test_a_healthy_item_in_a_healthy_zone_is_fresh(self) -> None:
        result = assess(make_item(), make_zone(), DAIRY, TODAY)
        assert result.status is Status.FRESH
        assert result.rule == 6

    def test_no_assessment_ever_claims_the_food_is_safe(self) -> None:
        """SRS rule 2: never claim food is safe or unsafe."""
        cases = [
            (make_item(), make_zone()),
            (make_item(expiry_date=date(2026, 9, 23)), make_zone()),
            (make_item(), make_zone(faults=frozenset({"temp"}))),
            (make_item(), make_zone(gas_breach=True)),
        ]
        for item, zone in cases:
            text = assess(item, zone, DAIRY, TODAY).message.lower()
            assert "safe" not in text
            assert "unsafe" not in text
            assert "spoiled" not in text


class TestRulePrecedence:
    def test_expiry_beats_a_sensor_fault(self) -> None:
        """Rule 1 precedes rule 2: a known problem outranks an unknown one."""
        zone = make_zone(faults=frozenset({"temp", "humidity"}))
        result = assess(make_item(expiry_date=date(2026, 9, 23)), zone, DAIRY, TODAY)
        assert result.status is Status.CHECK
        assert result.rule == 1

    def test_a_sensor_fault_beats_a_temperature_breach(self) -> None:
        """Rule 2 precedes rule 3: with a dead sensor the breach is not trustworthy."""
        zone = make_zone(faults=frozenset({"temp"}), temp_breach=True)
        assert assess(make_item(), zone, DAIRY, TODAY).rule == 2

    def test_a_sensor_fault_beats_use_soon(self) -> None:
        zone = make_zone(faults=frozenset({"temp"}))
        result = assess(make_item(expiry_date=date(2026, 9, 25)), zone, DAIRY, TODAY)
        assert result.rule == 2

    def test_a_temperature_breach_beats_use_soon(self) -> None:
        result = assess(
            make_item(expiry_date=date(2026, 9, 25)), make_zone(temp_breach=True), DAIRY, TODAY
        )
        assert result.rule == 3

    def test_gas_beats_use_soon(self) -> None:
        result = assess(
            make_item(expiry_date=date(2026, 9, 25)), make_zone(gas_breach=True), DAIRY, TODAY
        )
        assert result.rule == 4

    @pytest.mark.parametrize(
        ("zone_kwargs", "expected_rule"),
        [
            ({}, 6),
            ({"gas_breach": True}, 4),
            ({"humidity_breach": True}, 3),
            ({"temp_breach": True, "gas_breach": True}, 3),
            ({"faults": frozenset({"temp"}), "temp_breach": True}, 2),
        ],
    )
    def test_the_first_matching_rule_wins(self, zone_kwargs: dict, expected_rule: int) -> None:
        assert assess(make_item(), make_zone(**zone_kwargs), DAIRY, TODAY).rule == expected_rule


class TestZoneState:
    def test_a_missing_required_sensor_is_a_required_fault(self) -> None:
        assert make_zone(faults=frozenset({"temp"})).has_required_fault is True

    def test_a_missing_gas_sensor_is_not_a_required_fault(self) -> None:
        assert make_zone(faults=frozenset({"gas"})).has_required_fault is False

    def test_a_healthy_zone_has_no_required_fault(self) -> None:
        assert make_zone().has_required_fault is False


class TestWorstStatus:
    def test_check_food_outranks_everything(self) -> None:
        statuses = [
            Assessment(Status.FRESH, 6, "a"),
            Assessment(Status.CHECK, 3, "b"),
            Assessment(Status.FAULT, 2, "c"),
        ]
        assert worst_status(statuses) is Status.CHECK

    def test_a_fault_outranks_use_soon(self) -> None:
        statuses = [Assessment(Status.USE_SOON, 5, "a"), Assessment(Status.FAULT, 2, "b")]
        assert worst_status(statuses) is Status.FAULT

    def test_use_soon_outranks_fresh(self) -> None:
        statuses = [Assessment(Status.FRESH, 6, "a"), Assessment(Status.USE_SOON, 5, "b")]
        assert worst_status(statuses) is Status.USE_SOON

    def test_an_empty_list_is_fresh(self) -> None:
        assert worst_status([]) is Status.FRESH


class TestStatusLabels:
    @pytest.mark.parametrize(
        ("status", "label"),
        [
            (Status.FRESH, "Fresh/Normal"),
            (Status.USE_SOON, "Use Soon"),
            (Status.CHECK, "Check Food"),
            (Status.FAULT, "Sensor Fault/Data Unavailable"),
        ],
    )
    def test_the_labels_match_the_srs_wording(self, status: Status, label: str) -> None:
        assert str(status) == label

    def test_there_are_exactly_four_statuses(self) -> None:
        assert len(list(Status)) == 4


class TestSrsRecommendations:
    """The five recommendations the SRS names, and where each belongs."""

    def test_a_conditions_breach_recommends_checking_the_temperature(self) -> None:
        """Rule 3 is about the storage conditions, so it gets that wording."""
        verdict = assess(
            make_item(expiry_date=date(2099, 1, 1)),
            make_zone(temp_breach=True),
            DAIRY,
            TODAY,
        )
        assert verdict.recommendation == CONDITIONS_RECOMMENDATION
        assert verdict.recommendation == "Check the storage temperature"

    def test_a_sensor_fault_does_not_recommend_checking_the_temperature(self) -> None:
        """A dead sensor says nothing about the temperature, so it must not."""
        verdict = assess(
            make_item(expiry_date=date(2099, 1, 1)),
            make_zone(faults=frozenset({"temp"})),
            DAIRY,
            TODAY,
        )
        assert verdict.recommendation != CONDITIONS_RECOMMENDATION

    def test_check_food_recommends_inspection(self) -> None:
        verdict = assess(make_item(expiry_date=date(2020, 1, 1)), make_zone(), DAIRY, TODAY)
        assert verdict.recommendation == "Inspect the food before use"

    def test_the_disposal_wording_is_conditional_on_a_person_looking(self) -> None:
        """SRS: disposal follows human inspection. It is never our conclusion."""
        assert DISPOSAL_RECOMMENDATION.startswith("If inspection finds")
        assert "disposal procedure" in DISPOSAL_RECOMMENDATION

    def test_no_recommendation_declares_food_safe_or_unsafe(self) -> None:
        wordings = [
            *RECOMMENDATIONS.values(),
            CONDITIONS_RECOMMENDATION,
            DOOR_RECOMMENDATION,
            DISPOSAL_RECOMMENDATION,
        ]
        for wording in wordings:
            assert "is safe" not in wording.lower()
            assert "unsafe" not in wording.lower()

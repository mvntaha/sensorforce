"""The Raspberry Pi drivers, as far as they can be checked off the hardware.

These tests cannot prove the wiring works. What they can prove, and what
matters most, is the failure behaviour: a driver that throws must produce a
fault rather than a value or a crash, because a plausible-looking number from
a dead sensor is the one outcome the SRS forbids.

The real devices are replaced with stubs. The hardware-only checks live in
docs/pin_map.md as a manual procedure.
"""

from __future__ import annotations

import pytest

from core.outputs import BuzzerPattern, Led
from core.sensors.base import Reading
from core.sensors.real import MQ135_WARMUP_S, RealSensor


class StubDht:
    """Stands in for the DHT22."""

    def __init__(self, temperature=4.0, humidity=55.0, raises=False) -> None:
        self._temperature = temperature
        self._humidity = humidity
        self.raises = raises
        self.exited = False

    @property
    def temperature(self):
        if self.raises:
            raise RuntimeError("checksum error")
        return self._temperature

    @property
    def humidity(self):
        if self.raises:
            raise RuntimeError("checksum error")
        return self._humidity

    def exit(self) -> None:
        self.exited = True


class StubAdc:
    """Stands in for the ADS1115 channel."""

    def __init__(self, value=1200, raises=False) -> None:
        self._value = value
        self.raises = raises

    @property
    def value(self):
        if self.raises:
            raise OSError("I2C bus error")
        return self._value


class StubGpio:
    """Stands in for RPi.GPIO."""

    def __init__(self, level=False, raises=False) -> None:
        self.level = level
        self.raises = raises
        self.cleaned: list[int] = []

    def input(self, _pin: int) -> bool:
        if self.raises:
            raise OSError("GPIO read failed")
        return self.level

    def cleanup(self, pin: int) -> None:
        self.cleaned.append(pin)


def wired(
    *,
    dht: StubDht | None = None,
    adc: StubAdc | None = None,
    gpio: StubGpio | None = None,
    warmed: bool = True,
) -> RealSensor:
    """A RealSensor with its devices replaced and connect() short-circuited."""
    sensor = RealSensor()
    sensor._dht = dht if dht is not None else StubDht()
    sensor._adc = adc if adc is not None else StubAdc()
    sensor._gpio = gpio if gpio is not None else StubGpio()
    sensor._connected = True
    if warmed:
        sensor._started_at -= MQ135_WARMUP_S + 1
    return sensor


class TestHealthyReads:
    def test_a_good_read_produces_a_complete_reading(self) -> None:
        reading = wired().read()
        assert reading.healthy
        assert reading.temp_c == 4.0
        assert reading.humidity == 55.0
        assert reading.gas_raw == 1200

    def test_the_reading_is_storable(self, conn, zone_id: int) -> None:
        """The driver's output must satisfy the readings table constraints."""
        reading = wired().read()
        conn.execute(
            "INSERT INTO readings (zone_id, ts, temp_c, humidity, gas_raw, door_open, "
            "temp_ok, hum_ok, gas_ok) VALUES (:zone_id, :ts, :temp_c, :humidity, :gas_raw, "
            ":door_open, :temp_ok, :hum_ok, :gas_ok)",
            reading.to_row(zone_id),
        )
        assert conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0] == 1


class TestFailureBecomesFault:
    """The rule that matters: a failed read is a fault, never a value."""

    def test_a_failing_dht_faults_both_of_its_channels(self) -> None:
        reading = wired(dht=StubDht(raises=True)).read()
        assert reading.temp_c is None
        assert reading.humidity is None
        assert reading.temp_ok is False
        assert "temp" in reading.faults

    def test_a_failing_dht_never_reports_zero(self) -> None:
        """Zero degrees is a plausible fridge temperature; it must not appear."""
        reading = wired(dht=StubDht(raises=True)).read()
        assert reading.temp_c != 0
        assert reading.temp_c is None

    def test_a_failing_adc_faults_the_gas_channel_only(self) -> None:
        reading = wired(adc=StubAdc(raises=True)).read()
        assert reading.gas_raw is None
        assert reading.gas_ok is False
        assert reading.temp_ok is True, "the DHT22 is on a different bus"

    def test_a_none_from_the_dht_is_a_fault(self) -> None:
        reading = wired(dht=StubDht(temperature=None, humidity=None)).read()
        assert reading.temp_c is None
        assert "temp" in reading.faults

    def test_an_out_of_range_gas_value_is_a_fault(self) -> None:
        reading = wired(adc=StubAdc(value=999999)).read()
        assert reading.gas_raw is None
        assert "gas" in reading.faults

    def test_an_implausible_temperature_faults_the_whole_read(self) -> None:
        """A value the Reading type refuses must not crash the loop."""
        reading = wired(dht=StubDht(temperature=500.0)).read()
        assert reading.healthy is False
        assert reading.temp_c is None

    def test_a_failing_gpio_does_not_raise(self) -> None:
        reading = wired(gpio=StubGpio(raises=True)).read()
        assert isinstance(reading, Reading)

    def test_read_never_raises_even_with_everything_broken(self) -> None:
        sensor = wired(
            dht=StubDht(raises=True), adc=StubAdc(raises=True), gpio=StubGpio(raises=True)
        )
        reading = sensor.read()
        assert reading.healthy is False


class TestWarmUp:
    def test_gas_is_faulted_until_the_heater_has_settled(self) -> None:
        """A cold MQ-135 reports nothing rather than a misleading number."""
        reading = wired(warmed=False).read()
        assert reading.gas_raw is None
        assert "gas" in reading.faults

    def test_temperature_still_reports_during_warm_up(self) -> None:
        reading = wired(warmed=False).read()
        assert reading.temp_c == 4.0

    def test_gas_reports_once_warmed(self) -> None:
        assert wired(warmed=True).read().gas_raw == 1200


class TestDhtRateLimit:
    def test_the_previous_value_is_reused_inside_the_window(self) -> None:
        """The DHT22 cannot be sampled faster than about once every two seconds."""
        dht = StubDht(temperature=4.0)
        sensor = wired(dht=dht)
        assert sensor.read().temp_c == 4.0
        dht._temperature = 9.0
        assert sensor.read().temp_c == 4.0, "the sensor was not re-read this soon"


class TestDoorDebounce:
    def test_a_closed_door_reads_closed(self) -> None:
        assert wired(gpio=StubGpio(level=False)).read().door_open is False

    def test_a_change_is_not_accepted_immediately(self) -> None:
        """Contact bounce must not produce a burst of open and closed events."""
        gpio = StubGpio(level=False)
        sensor = wired(gpio=gpio)
        sensor.read()
        gpio.level = True
        assert sensor.read().door_open is False, "the change has not held long enough yet"

    def test_a_held_change_is_accepted(self) -> None:
        gpio = StubGpio(level=False)
        sensor = wired(gpio=gpio)
        sensor.read()
        gpio.level = True
        sensor._door_since -= 1.0
        assert sensor.read().door_open is True


class TestShutdown:
    def test_close_releases_the_devices(self) -> None:
        dht, gpio = StubDht(), StubGpio()
        sensor = wired(dht=dht, gpio=gpio)
        sensor.close()
        assert dht.exited is True
        assert gpio.cleaned == [sensor.reed_pin]

    def test_close_is_safe_twice(self) -> None:
        sensor = wired()
        sensor.close()
        sensor.close()


class TestLazyImports:
    """The Pi libraries must not be needed to import the project."""

    def test_the_sensor_module_imports_without_pi_libraries(self) -> None:
        import core.sensors.real as module

        assert module.RealSensor is not None

    def test_the_outputs_module_imports_without_pi_libraries(self) -> None:
        import core.outputs_real as module

        assert module.real_outputs is not None

    def test_connecting_without_hardware_is_a_fault_not_a_crash(self) -> None:
        """On a laptop the import inside connect() fails; that is a total fault."""
        assert RealSensor().read().healthy is False


class TestPinMapMatchesTheCode:
    """The document and the constants must not drift apart."""

    def test_every_documented_pin_appears_in_the_drivers(self) -> None:
        from pathlib import Path

        from core.outputs_real import BUZZER_PIN, LED_PINS
        from core.sensors.real import DHT_PIN, REED_PIN

        document = (Path(__file__).resolve().parent.parent / "docs" / "pin_map.md").read_text(
            encoding="utf-8"
        )
        for pin in (DHT_PIN, REED_PIN, BUZZER_PIN, *LED_PINS.values()):
            assert f"GPIO{pin}" in document, f"GPIO{pin} is used but not documented"

    def test_the_buzzer_patterns_differ(self) -> None:
        """A door alert and a threshold alert must be distinguishable by ear."""
        from core.outputs_real import DOOR_PATTERN, THRESHOLD_PATTERN

        assert DOOR_PATTERN != THRESHOLD_PATTERN
        assert len(THRESHOLD_PATTERN) > len(DOOR_PATTERN)

    def test_every_status_maps_to_a_lamp(self) -> None:
        from core.outputs import STATUS_LEDS

        assert set(STATUS_LEDS.values()) <= {Led.GREEN, Led.YELLOW, Led.RED}
        assert STATUS_LEDS["Sensor Fault/Data Unavailable"] is Led.RED

    @pytest.mark.parametrize("pattern", list(BuzzerPattern))
    def test_each_buzzer_pattern_has_real_steps(self, pattern: BuzzerPattern) -> None:
        from core.outputs_real import DOOR_PATTERN, THRESHOLD_PATTERN

        steps = DOOR_PATTERN if pattern is BuzzerPattern.DOOR else THRESHOLD_PATTERN
        assert steps
        assert all(on > 0 for on, _off in steps)


class StubPwm:
    """Stands in for RPi.GPIO.PWM on the buzzer pin."""

    def __init__(self, pin: int, frequency: float) -> None:
        self.pin = pin
        self.frequency = frequency
        self.starts: list[float] = []
        self.stops = 0

    def start(self, duty: float) -> None:
        self.starts.append(duty)

    def stop(self) -> None:
        self.stops += 1


class StubBuzzerGpio:
    """Stands in for RPi.GPIO where a PWM channel is needed."""

    LOW = 0
    HIGH = 1
    OUT = "out"

    def __init__(self) -> None:
        self.pwms: list[StubPwm] = []
        self.cleaned: list[int] = []

    def setup(self, _pin, _mode, **_kwargs) -> None:
        return None

    def output(self, _pin, _value) -> None:
        return None

    def PWM(self, pin: int, frequency: float) -> StubPwm:
        pwm = StubPwm(pin, frequency)
        self.pwms.append(pwm)
        return pwm

    def cleanup(self, pin: int) -> None:
        self.cleaned.append(pin)


class TestPassiveBuzzer:
    """The fitted buzzer is passive, so it must be driven with a tone.

    A steady HIGH makes a passive buzzer click once and fall silent, which
    would leave a confirmed alert inaudible. These check that the driver
    generates and stops a tone rather than holding a level.
    """

    def _buzzer(self):
        from core.outputs_real import PiBuzzer

        buzzer = PiBuzzer()
        gpio = StubBuzzerGpio()
        buzzer._gpio_module = gpio
        buzzer._pwm = gpio.PWM(buzzer.pin, buzzer.frequency_hz)
        return buzzer, gpio

    def test_it_sounds_at_the_wired_frequency(self) -> None:
        from core.outputs_real import BUZZER_FREQ_HZ, BUZZER_PIN

        buzzer, gpio = self._buzzer()
        assert gpio.pwms[0].pin == BUZZER_PIN
        assert gpio.pwms[0].frequency == BUZZER_FREQ_HZ

    def test_a_pattern_starts_and_stops_the_tone(self) -> None:
        buzzer, gpio = self._buzzer()
        buzzer._play(((0.0, 0.0), (0.0, 0.0)))

        pwm = gpio.pwms[0]
        assert len(pwm.starts) == 2, "each step must sound"
        assert pwm.stops == 2, "each step must go quiet again"
        assert all(0 < duty < 100 for duty in pwm.starts)

    def test_silencing_stops_the_tone(self) -> None:
        buzzer, gpio = self._buzzer()
        buzzer.silence()
        assert gpio.pwms[0].stops == 1

    def test_a_silenced_buzzer_plays_nothing_further(self) -> None:
        buzzer, gpio = self._buzzer()
        buzzer.silence()
        buzzer._play(((0.0, 0.0), (0.0, 0.0)))
        assert gpio.pwms[0].starts == []

    def test_closing_releases_the_pin(self) -> None:
        from core.outputs_real import BUZZER_PIN

        buzzer, gpio = self._buzzer()
        buzzer.close()
        assert BUZZER_PIN in gpio.cleaned

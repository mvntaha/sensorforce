"""Raspberry Pi implementations of the LEDs, LCD and buzzer.

**Not yet verified on hardware.** Written against the frozen interfaces in
``outputs.py`` and the device datasheets; nothing here has driven a real pin.
The manual procedure in ``docs/pin_map.md`` is what verifies it.

The pin numbers and the buzzer type now come from the signed wiring sheet
rather than from the datasheets, so they match the built board. That makes them
correct on paper; it does not make them tested.

Every method swallows driver errors. A failed lamp must not stop the loop that
would otherwise sound the buzzer.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core.outputs import Buzzer, BuzzerPattern, Display, Led, LedArray, OutputSet, fit

logger = logging.getLogger(__name__)

#: Board pins, taken from the signed wiring sheet and recorded in
#: docs/pin_map.md. Header pins 13, 15, 16 and 18 respectively.
LED_PINS: dict[Led, int] = {Led.GREEN: 27, Led.YELLOW: 22, Led.RED: 23}
BUZZER_PIN = 24

#: I2C address of the 16x2 display.
LCD_ADDRESS = 0x27

#: The fitted buzzer is passive: it contains no oscillator, so a steady HIGH
#: produces one click and then silence. It has to be driven with a square wave,
#: and this is the frequency the device firmware already sounds audibly on this
#: hardware.
BUZZER_FREQ_HZ = 2700

#: Duty cycle while sounding. 50% is the loudest point for a square wave and is
#: what the firmware uses.
BUZZER_DUTY = 50

#: Threshold alerts are three short bursts; a door alert is one long tone, so
#: the two can be told apart without looking at the screen.
THRESHOLD_PATTERN = ((0.12, 0.09), (0.12, 0.09), (0.12, 0.0))
DOOR_PATTERN = ((0.7, 0.0),)


def _gpio() -> Any:
    """Import RPi.GPIO lazily so this module loads on a laptop."""
    from RPi import GPIO

    GPIO.setmode(GPIO.BCM)
    return GPIO


@dataclass
class PiLedArray(LedArray):
    """Three status lamps on GPIO pins, one lit at a time."""

    pins: dict[Led, int] = field(default_factory=lambda: dict(LED_PINS))
    _gpio_module: Any = field(default=None, init=False, repr=False)

    def _setup(self) -> Any:
        if self._gpio_module is None:
            gpio = _gpio()
            for pin in self.pins.values():
                gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
            self._gpio_module = gpio
        return self._gpio_module

    def show(self, led: Led | None) -> None:
        """Light one lamp and extinguish the others."""
        try:
            gpio = self._setup()
            for name, pin in self.pins.items():
                gpio.output(pin, gpio.HIGH if name is led else gpio.LOW)
        except Exception as exc:
            logger.warning("could not drive the lamps: %s", exc)

    def close(self) -> None:
        """Turn everything off and release the pins."""
        if self._gpio_module is None:
            return
        try:
            for pin in self.pins.values():
                self._gpio_module.output(pin, self._gpio_module.LOW)
            self._gpio_module.cleanup(list(self.pins.values()))
        except Exception as exc:
            logger.debug("ignored while releasing the lamps: %s", exc)
        self._gpio_module = None


@dataclass
class PiDisplay(Display):
    """16x2 character LCD on the I2C bus."""

    address: int = LCD_ADDRESS
    _lcd: Any = field(default=None, init=False, repr=False)

    def _setup(self) -> Any:
        if self._lcd is None:
            from RPLCD.i2c import CharLCD

            self._lcd = CharLCD("PCF8574", self.address, cols=16, rows=2)
        return self._lcd

    def write(self, line_one: str, line_two: str = "") -> None:
        """Replace both lines, truncated and padded to the panel width."""
        try:
            lcd = self._setup()
            lcd.cursor_pos = (0, 0)
            lcd.write_string(fit(line_one))
            lcd.cursor_pos = (1, 0)
            lcd.write_string(fit(line_two))
        except Exception as exc:
            logger.warning("could not write to the display: %s", exc)

    def close(self) -> None:
        """Clear and release the panel."""
        if self._lcd is None:
            return
        try:
            self._lcd.clear()
            self._lcd.close(clear=True)
        except Exception as exc:
            logger.debug("ignored while releasing the display: %s", exc)
        self._lcd = None


@dataclass
class PiBuzzer(Buzzer):
    """Passive buzzer on a GPIO pin, driven by PWM.

    A passive buzzer has no oscillator of its own, so holding the pin HIGH
    makes it click once and then fall silent. Sounding it means generating the
    tone, which is what the PWM channel is for: start it to sound, stop it to
    go quiet.

    The pattern is played on a short-lived thread so the sensor loop is not
    blocked for the length of the tone.
    """

    pin: int = BUZZER_PIN
    frequency_hz: int = BUZZER_FREQ_HZ
    duty: int = BUZZER_DUTY
    _gpio_module: Any = field(default=None, init=False, repr=False)
    _pwm: Any = field(default=None, init=False, repr=False)
    _silence: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def _setup(self) -> Any:
        if self._gpio_module is None:
            gpio = _gpio()
            gpio.setup(self.pin, gpio.OUT, initial=gpio.LOW)
            # Created once and reused. Starting a second PWM on the same pin
            # leaves the first running, and the buzzer never goes quiet again.
            self._pwm = gpio.PWM(self.pin, self.frequency_hz)
            self._gpio_module = gpio
        return self._gpio_module

    def _tone_on(self) -> None:
        if self._pwm is not None:
            self._pwm.start(self.duty)

    def _tone_off(self) -> None:
        if self._pwm is not None:
            self._pwm.stop()

    def _play(self, pattern: tuple[tuple[float, float], ...]) -> None:
        for on_s, off_s in pattern:
            if self._silence.is_set():
                break
            self._tone_on()
            self._silence.wait(on_s)
            self._tone_off()
            if off_s:
                self._silence.wait(off_s)

    def sound(self, pattern: BuzzerPattern) -> None:
        """Play the pattern for this alert kind, without blocking the caller."""
        try:
            self._setup()
        except Exception as exc:
            logger.warning("could not open the buzzer: %s", exc)
            return

        self._silence.clear()
        steps = DOOR_PATTERN if pattern is BuzzerPattern.DOOR else THRESHOLD_PATTERN
        threading.Thread(target=self._play, args=(steps,), daemon=True).start()

    def silence(self) -> None:
        """Stop immediately. Used by the acknowledge and mute paths."""
        self._silence.set()
        try:
            self._tone_off()
        except Exception as exc:
            logger.debug("ignored while silencing: %s", exc)

    def close(self) -> None:
        """Silence and release the pin."""
        self.silence()
        if self._gpio_module is None:
            return
        self._pwm = None
        try:
            self._gpio_module.cleanup(self.pin)
        except Exception as exc:
            logger.debug("ignored while releasing the buzzer: %s", exc)
        self._gpio_module = None


def real_outputs() -> OutputSet:
    """Return the three devices wired to the Pi."""
    return OutputSet(leds=PiLedArray(), display=PiDisplay(), buzzer=PiBuzzer())


def self_test(hold_s: float = 1.0) -> None:  # pragma: no cover - hardware only
    """Light each lamp, write to the panel and sound both patterns.

    Run this on the Pi as the first check that the wiring matches the pin map:
        python -c "from core.outputs_real import self_test; self_test()"
    """
    outputs = real_outputs()
    try:
        for led in (Led.GREEN, Led.YELLOW, Led.RED):
            outputs.leds.show(led)
            outputs.display.write("FreshGuard test", f"LED: {led.value}")
            time.sleep(hold_s)
        outputs.leds.show(None)

        outputs.display.write("Buzzer: threshold", "3 short bursts")
        outputs.buzzer.sound(BuzzerPattern.THRESHOLD)
        time.sleep(2)

        outputs.display.write("Buzzer: door", "1 long tone")
        outputs.buzzer.sound(BuzzerPattern.DOOR)
        time.sleep(2)

        outputs.display.write("Self test done", "")
    finally:
        outputs.close()

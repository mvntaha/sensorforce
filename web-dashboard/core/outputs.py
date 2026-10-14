"""Local alert outputs: LEDs, the LCD and the buzzer.

Each device is wrapped in a small class with a ``Fake`` twin that records what
it was asked to do instead of touching hardware. The alert path is therefore
fully testable on a laptop, which is what lets the timing requirement be
measured without a Pi.

Hardware libraries are imported lazily inside the real implementations so this
module imports cleanly anywhere.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)

#: 16x2 character display.
LCD_COLUMNS = 16
LCD_ROWS = 2

#: Shown whenever a required sensor is unavailable. Never "Fresh".
FAULT_TEXT = "Sensor Fault/Data Unavailable"


class Led(StrEnum):
    """The three status lamps."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class BuzzerPattern(StrEnum):
    """Buzzer patterns are distinct so the two alert kinds are distinguishable."""

    #: A threshold or gas violation: three short bursts.
    THRESHOLD = "threshold"
    #: The door left open: one long tone.
    DOOR = "door"


#: Maps a freshness status label to the lamp that should be lit. A faulty
#: sensor lights the red lamp, never the green one.
STATUS_LEDS: dict[str, Led] = {
    "Fresh/Normal": Led.GREEN,
    "Use Soon": Led.YELLOW,
    "Check Food": Led.RED,
    FAULT_TEXT: Led.RED,
}


class LedArray(ABC):
    """Exactly one lamp is lit at a time."""

    @abstractmethod
    def show(self, led: Led | None) -> None:
        """Light ``led`` and extinguish the others. ``None`` turns all off."""

    def show_status(self, status: str) -> None:
        """Light the lamp for a freshness status.

        An unknown status lights red rather than green, so a bug can never
        present as everything being fine.
        """
        self.show(STATUS_LEDS.get(status, Led.RED))

    def close(self) -> None:  # noqa: B027 - optional hook; the fakes hold nothing
        """Release the pins. Safe to call more than once."""


class Display(ABC):
    """A two-line character display."""

    @abstractmethod
    def write(self, line_one: str, line_two: str = "") -> None:
        """Replace the display contents. Lines are truncated to fit."""

    def close(self) -> None:  # noqa: B027 - optional hook; the fakes hold nothing
        """Release the device. Safe to call more than once."""


class Buzzer(ABC):
    """An audible alert."""

    @abstractmethod
    def sound(self, pattern: BuzzerPattern) -> None:
        """Play ``pattern`` once."""

    @abstractmethod
    def silence(self) -> None:
        """Stop immediately. Used by the acknowledge and mute paths."""

    def close(self) -> None:  # noqa: B027 - optional hook; the fakes hold nothing
        """Release the pin. Safe to call more than once."""


def fit(text: str, width: int = LCD_COLUMNS) -> str:
    """Truncate or pad ``text`` to exactly ``width`` characters."""
    return text[:width].ljust(width)


def format_reading_line(temp_c: float | None, humidity: float | None, gas_raw: int | None) -> str:
    """Return the measurements line for the display.

    A missing value shows as ``--`` rather than a zero, so a fault is never
    displayed as a plausible reading. Gas is shown as a relative figure and
    never as ppm.
    """
    temperature = f"{temp_c:.1f}C" if temp_c is not None else "--C"
    moisture = f"{humidity:.0f}%" if humidity is not None else "--%"
    gas = f"G{gas_raw}" if gas_raw is not None else "G--"
    return fit(f"{temperature} {moisture} {gas}")


@dataclass
class FakeLedArray(LedArray):
    """Records which lamp was lit. Used by tests and by sim mode."""

    current: Led | None = None
    history: list[Led | None] = field(default_factory=list)

    def show(self, led: Led | None) -> None:
        """Record the lamp instead of driving a pin."""
        self.current = led
        self.history.append(led)
        logger.debug("LED -> %s", led)


@dataclass
class FakeDisplay(Display):
    """Records what would have been shown on the LCD."""

    lines: tuple[str, str] = ("", "")
    history: list[tuple[str, str]] = field(default_factory=list)

    def write(self, line_one: str, line_two: str = "") -> None:
        """Record the two lines, truncated exactly as the hardware would."""
        self.lines = (fit(line_one), fit(line_two))
        self.history.append(self.lines)
        logger.debug("LCD -> %r | %r", *self.lines)

    @property
    def text(self) -> str:
        """Both lines joined, for convenient assertions."""
        return " ".join(line.strip() for line in self.lines).strip()


@dataclass
class FakeBuzzer(Buzzer):
    """Records buzzer activity without making a sound."""

    patterns: list[BuzzerPattern] = field(default_factory=list)
    silenced: bool = False

    def sound(self, pattern: BuzzerPattern) -> None:
        """Record the pattern that would have been played."""
        self.patterns.append(pattern)
        self.silenced = False
        logger.debug("buzzer -> %s", pattern)

    def silence(self) -> None:
        """Record that the buzzer was silenced."""
        self.silenced = True


@dataclass(frozen=True, slots=True)
class OutputSet:
    """The three local outputs, passed around together."""

    leds: LedArray
    display: Display
    buzzer: Buzzer

    def close(self) -> None:
        """Release every device."""
        self.leds.close()
        self.display.close()
        self.buzzer.close()


def fake_outputs() -> OutputSet:
    """Return a set of recording outputs for tests and sim mode."""
    return OutputSet(leds=FakeLedArray(), display=FakeDisplay(), buzzer=FakeBuzzer())


def get_outputs(mode: str = "sim") -> OutputSet:
    """Return the outputs for ``mode``.

    The real implementations live in ``core.outputs_real`` and are imported
    lazily so this module works on a machine with no GPIO.
    """
    if mode == "sim":
        return fake_outputs()
    if mode == "real":
        from core.outputs_real import real_outputs

        return real_outputs()
    raise ValueError(f"unknown output mode {mode!r}; expected 'real' or 'sim'")

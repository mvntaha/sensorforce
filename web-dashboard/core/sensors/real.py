"""Raspberry Pi sensor drivers.

**Not yet verified on hardware.** Every line here is written against the
datasheets and the interface frozen in ``base.py``, but nothing in this file
has been run against a real DHT22, ADS1115 or reed switch. The manual test
procedure in ``docs/pin_map.md`` is what turns it from plausible into proven.

Two rules shape the whole module:

* **A failed read is a fault, never a value.** Returning 0 °C because the DHT22
  did not answer would put a plausible number on the dashboard and could show
  Fresh while the sensor is dead. Every failure path produces a fault instead.
* **Nothing raises.** ``read()`` is called once a second by a loop that must
  not die. Any driver error becomes a fault on that sensor.

Hardware libraries are imported lazily inside ``connect()`` so the project,
and its tests, run on a laptop with none of them installed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from core.sensors.base import GAS_RAW_RANGE, Reading, Sensor, utc_now_iso

logger = logging.getLogger(__name__)

#: Board pins. Confirm against the real wiring and record in docs/pin_map.md.
DHT_PIN = 4
REED_PIN = 17

#: The ADS1115 channel the MQ-135 analog output is wired to.
GAS_CHANNEL = 0

#: The DHT22 answers about once every two seconds; asking more often returns
#: stale data or an error. The loop still polls faster than this, and the cached
#: value is reused in between, so the door and gas channels stay responsive.
DHT_MIN_INTERVAL_S = 2.0

#: The reed switch must hold a state this long before it counts as changed.
REED_DEBOUNCE_S = 0.05

#: The MQ-135 heater needs time before its readings mean anything. Until this
#: has elapsed the gas channel reports a fault rather than a misleading number.
MQ135_WARMUP_S = 180.0


@dataclass
class RealSensor(Sensor):
    """Reads one zone's DHT22, MQ-135, and reed switch on a Raspberry Pi.

    Status: written from datasheets, never run on hardware. See
    docs/pin_map.md for the procedure that verifies it.
    """

    dht_pin: int = DHT_PIN
    reed_pin: int = REED_PIN
    gas_channel: int = GAS_CHANNEL
    warmup_s: float = MQ135_WARMUP_S
    debounce_s: float = REED_DEBOUNCE_S

    _dht: Any = field(default=None, init=False, repr=False)
    _adc: Any = field(default=None, init=False, repr=False)
    _gpio: Any = field(default=None, init=False, repr=False)
    _started_at: float = field(default_factory=time.monotonic, init=False)
    _last_dht_at: float = field(default=0.0, init=False)
    _last_temp: float | None = field(default=None, init=False)
    _last_humidity: float | None = field(default=None, init=False)
    _door_state: bool = field(default=False, init=False)
    _door_since: float = field(default_factory=time.monotonic, init=False)
    _connected: bool = field(default=False, init=False)

    # -- wiring ------------------------------------------------------------
    def connect(self) -> None:
        """Open the devices. Imports the Pi libraries only when called."""
        if self._connected:
            return

        import adafruit_dht
        import board
        import busio
        from adafruit_ads1x15.ads1115 import ADS1115
        from adafruit_ads1x15.analog_in import AnalogIn
        from RPi import GPIO

        self._dht = adafruit_dht.DHT22(getattr(board, f"D{self.dht_pin}"))

        i2c = busio.I2C(board.SCL, board.SDA)
        self._adc = AnalogIn(ADS1115(i2c), self.gas_channel)

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.reed_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        self._gpio = GPIO

        self._connected = True
        logger.info(
            "sensors connected: DHT22 on GPIO%s, reed on GPIO%s", self.dht_pin, self.reed_pin
        )

    @property
    def warmed_up(self) -> bool:
        """True once the MQ-135 heater has had long enough to settle."""
        return (time.monotonic() - self._started_at) >= self.warmup_s

    # -- individual sensors ------------------------------------------------
    def _read_dht(self) -> tuple[float | None, float | None]:
        """Return temperature and humidity, or None for whichever failed.

        The DHT22 answers roughly once every two seconds and fails often even
        when healthy, so the previous good value is reused inside that window.
        A genuine read failure returns None, which becomes a fault.
        """
        now = time.monotonic()
        if now - self._last_dht_at < DHT_MIN_INTERVAL_S:
            return self._last_temp, self._last_humidity

        self._last_dht_at = now
        try:
            temperature = self._dht.temperature
            humidity = self._dht.humidity
        except Exception as exc:
            logger.debug("DHT22 read failed: %s", exc)
            self._last_temp = self._last_humidity = None
            return None, None

        self._last_temp = float(temperature) if temperature is not None else None
        self._last_humidity = float(humidity) if humidity is not None else None
        return self._last_temp, self._last_humidity

    def _read_gas(self) -> int | None:
        """Return the raw ADC count, or None while warming up or on failure.

        The value is raw counts, never ppm: converting to a concentration would
        need a calibration this prototype does not have.
        """
        if not self.warmed_up:
            return None
        try:
            raw = int(self._adc.value)
        except Exception as exc:
            logger.debug("ADS1115 read failed: %s", exc)
            return None

        low, high = GAS_RAW_RANGE
        if not low <= raw <= high:
            logger.warning("gas reading %s outside the ADC range", raw)
            return None
        return raw

    def _read_door(self) -> bool:
        """Return the debounced door state.

        A change is only accepted once it has held for the debounce window, so
        contact bounce cannot produce a burst of open/closed events.
        """
        try:
            # Pull-up: the switch pulls the pin low when the magnet is present,
            # so a HIGH reading means the door is open.
            raw_open = bool(self._gpio.input(self.reed_pin))
        except Exception as exc:
            logger.debug("reed switch read failed: %s", exc)
            return self._door_state

        now = time.monotonic()
        if raw_open != self._door_state:
            if now - self._door_since >= self.debounce_s:
                self._door_state = raw_open
                self._door_since = now
        else:
            self._door_since = now
        return self._door_state

    # -- interface ---------------------------------------------------------
    def read(self) -> Reading:
        """Return one reading. Never raises; failures become faults."""
        try:
            self.connect()
        except Exception as exc:
            logger.exception("could not open the sensors: %s", exc)
            return Reading.faulted()

        temperature, humidity = self._read_dht()
        gas = self._read_gas()
        door_open = self._read_door()

        faults = set()
        if temperature is None:
            faults.add("temp")
        if humidity is None:
            faults.add("humidity")
        if gas is None:
            faults.add("gas")

        try:
            return Reading(
                ts=utc_now_iso(),
                temp_c=temperature,
                humidity=humidity,
                gas_raw=gas,
                door_open=door_open,
                faults=frozenset(faults),
            )
        except ValueError:
            # A value outside the physically possible range. Report the whole
            # read as faulty rather than storing something impossible.
            logger.warning(
                "implausible reading discarded: %s C, %s %%, %s", temperature, humidity, gas
            )
            return Reading.faulted(door_open=door_open)

    def close(self) -> None:
        """Release the pins and the DHT22. Safe to call more than once."""
        if self._dht is not None:
            with _ignoring_errors():
                self._dht.exit()
            self._dht = None
        if self._gpio is not None:
            with _ignoring_errors():
                self._gpio.cleanup(self.reed_pin)
            self._gpio = None
        self._connected = False


class _ignoring_errors:
    """Swallow errors during shutdown, where there is nothing left to do."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        if exc is not None:
            logger.debug("ignored during shutdown: %s", exc)
        return True

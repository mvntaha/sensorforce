"""Measure the MQ-135 clean-air baseline and propose a zone gas threshold.

The values currently seeded on the zone came from the simulator, not from any
physical sensor: 1817 simulated readings on 2026-09-24 averaged 1200.1, and the
seeded baseline is 1200. The real sensor idles several times higher. This tool
produces the numbers that would replace them.

It **prints only**. Nothing here writes to the database, because a calibration
figure should be read by a person, sanity-checked against the room it was taken
in, and recorded in docs/calibration.md before it changes how the fridge
behaves.

Method, per docs/calibration.md:

1. Honour the configured warm-up. A cold MQ-135 reports a drifting number that
   means nothing, so sampling before it settles produces a baseline that is
   simply wrong.
2. Collect N samples at a fixed interval in clean air.
3. Abort, loudly, if any sample is zero or the sensor reports a fault. A run
   taken through a failing sensor is worse than no run at all: it looks like
   data.
4. baseline = mean, spread = standard deviation.
5. abnormal = max(baseline + 5 * spread, baseline * 1.25)
   The 5-sigma term stops ordinary noise tripping the alert. The 25% term stops
   a very quiet sensor producing a threshold so close to the baseline that any
   drift trips it. Whichever is larger wins.

Run this in a ventilated room, away from cooking, solvents, alcohol and breath.

Usage:
    python scripts/calibrate_gas.py                  # 100 samples at 1 Hz
    python scripts/calibrate_gas.py --samples 300
    python scripts/calibrate_gas.py --skip-warmup    # sensor already hot
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - convenience for direct runs
    sys.path.insert(0, str(REPO_ROOT))

from core.sensors.base import GAS_RAW_RANGE  # noqa: E402
from core.sensors.real import MQ135_WARMUP_S, RealSensor  # noqa: E402

logger = logging.getLogger(__name__)

#: Samples taken by default. Two minutes at 1 Hz is enough to see the spread
#: without asking anyone to stand still for very long.
DEFAULT_SAMPLES = 100

#: Seconds between samples.
DEFAULT_INTERVAL_S = 1.0

#: Multiples of the standard deviation the abnormal threshold must clear.
SIGMA_MARGIN = 5

#: Minimum proportional headroom, for a sensor whose noise is very low.
PROPORTIONAL_MARGIN = 1.25

#: A run with fewer than this many samples is not worth reporting.
MIN_USABLE_SAMPLES = 30


class CalibrationAborted(RuntimeError):
    """Raised when the run cannot produce a trustworthy figure."""


@dataclass(frozen=True, slots=True)
class Summary:
    """What one run measured, and what it proposes."""

    samples: list[int]

    @property
    def count(self) -> int:
        return len(self.samples)

    @property
    def mean(self) -> float:
        return statistics.fmean(self.samples)

    @property
    def median(self) -> float:
        return statistics.median(self.samples)

    @property
    def spread(self) -> float:
        """Population standard deviation; every sample of the run is included."""
        return statistics.pstdev(self.samples) if self.count > 1 else 0.0

    @property
    def baseline(self) -> int:
        return round(self.mean)

    @property
    def abnormal(self) -> int:
        """The larger of the two margins, so neither failure mode is possible."""
        sigma = self.mean + SIGMA_MARGIN * self.spread
        proportional = self.mean * PROPORTIONAL_MARGIN
        return round(max(sigma, proportional))

    @property
    def margin_used(self) -> str:
        """Which rule decided the threshold. Worth printing: it explains the gap."""
        sigma = self.mean + SIGMA_MARGIN * self.spread
        return f"{SIGMA_MARGIN}-sigma" if sigma >= self.mean * PROPORTIONAL_MARGIN else "25%"


def warm_up(sensor: RealSensor, *, skip: bool) -> None:
    """Wait for the heater, reporting progress so the wait is not mistaken for a hang."""
    if skip:
        logger.info("skipping warm-up at your request; the baseline is only as good as that")
        return

    logger.info("warming up for %.0f s before sampling", MQ135_WARMUP_S)
    deadline = time.monotonic() + MQ135_WARMUP_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if int(remaining) % 30 == 0:
            logger.info("  %3.0f s remaining", remaining)
        time.sleep(min(1.0, remaining))
    logger.info("warm-up complete")


def collect(sensor: RealSensor, samples: int, interval_s: float) -> list[int]:
    """Take the samples, refusing to continue through a fault."""
    low, high = GAS_RAW_RANGE
    collected: list[int] = []

    for index in range(1, samples + 1):
        reading = sensor.read()
        value = reading.gas_raw

        if not reading.gas_ok or value is None:
            raise CalibrationAborted(
                f"sample {index}: the gas sensor reported a fault. "
                "Check the I2C wiring and the MQ-135 connection, then run again."
            )
        if value == 0:
            raise CalibrationAborted(
                f"sample {index}: raw value 0. That is the signature of a "
                "disconnected sensor, not clean air."
            )
        if not low <= value <= high:
            raise CalibrationAborted(f"sample {index}: raw value {value} is outside the ADC range.")

        collected.append(value)
        if index % 10 == 0:
            logger.info("  %d/%d samples", index, samples)
        if index < samples:
            time.sleep(interval_s)

    return collected


def report(summary: Summary) -> None:
    """Print the measurements, the proposal and a row ready for the docs."""
    logger.info("")
    logger.info("Measured")
    logger.info("  samples   %d", summary.count)
    logger.info("  mean      %.1f", summary.mean)
    logger.info("  median    %.1f", summary.median)
    logger.info("  stdev     %.1f", summary.spread)
    logger.info("  min       %d", min(summary.samples))
    logger.info("  max       %d", max(summary.samples))

    logger.info("")
    logger.info("Proposed (raw ADC counts, never ppm)")
    logger.info("  gas_baseline  %d", summary.baseline)
    logger.info("  gas_abnormal  %d   (set by the %s rule)", summary.abnormal, summary.margin_used)

    logger.info("")
    logger.info("Paste into docs/calibration.md:")
    logger.info("")
    logger.info("| Item | Value | How it was chosen |")
    logger.info("|---|---|---|")
    logger.info(
        "| `gas_baseline` | %d | mean of %d clean-air samples |", summary.baseline, summary.count
    )
    logger.info(
        "| `gas_abnormal` | %d | %s above the baseline (stdev %.1f) |",
        summary.abnormal,
        summary.margin_used,
        summary.spread,
    )
    logger.info("| Source note | prototype/demo assumption | |")

    logger.info("")
    logger.info("Before using these numbers, validate both directions:")
    logger.info("  1. hold a marker pen or alcohol wipe near the sensor")
    logger.info("     -> the value must climb above %d within a few seconds", summary.abnormal)
    logger.info("  2. leave it in clean air for 10 minutes")
    logger.info("     -> it must never reach %d", summary.abnormal)
    logger.info("")
    logger.info("Nothing has been written to the database. Set the values on the")
    logger.info("zone from the admin page once you are satisfied with them.")


def main(argv: list[str] | None = None) -> int:
    """Take a calibration run and print the proposal. Returns an exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S)
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="the sensor has already been powered for longer than the warm-up",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.samples < MIN_USABLE_SAMPLES:
        logger.error(
            "%d samples is too few to measure a spread; use at least %d",
            args.samples,
            MIN_USABLE_SAMPLES,
        )
        return 2

    sensor = RealSensor()
    try:
        sensor.connect()
    except Exception as exc:
        logger.error("could not open the sensors: %s", exc)
        logger.info("-> this tool only runs on the Pi, with the MQ-135 and ADS1115 wired")
        return 1

    try:
        warm_up(sensor, skip=args.skip_warmup)
        logger.info("sampling %d times at %.1f s intervals", args.samples, args.interval)
        collected = collect(sensor, args.samples, args.interval)
    except CalibrationAborted as exc:
        logger.error("")
        logger.error("ABORTED: %s", exc)
        logger.info("")
        logger.info("No figures are printed from a partial run: a baseline taken")
        logger.info("through a failing sensor would look like data and be wrong.")
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive stop
        logger.info("\nstopped before the run finished; nothing to report")
        return 1
    finally:
        sensor.close()

    report(Summary(samples=collected))
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())

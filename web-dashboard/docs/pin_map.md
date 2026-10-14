# Pin Mapping and Hardware Verification

Owner: SF-SensorForce  ·  Wiring confirmed by: SF-SensorForce  ·  Last reviewed: 2026-09-29

**Status: transcribed from the signed wiring sheet "FreshGuard – Raspberry Pi 4B
Pin Cuttings".** The board is built and this document now records what is
actually wired, not what was proposed. Where the software disagrees with the
board, the board is correct and the software is listed as outstanding work.

Controller: **Raspberry Pi 4B**, Raspberry Pi OS. The Pi is the only controller
in the delivered system. An ESP32 Dev Module was used as a bench rig to prove
individual components before they were moved to the Pi; it is not part of the
build (see "Component bench tests" below).

## Safety

Raspberry Pi GPIO pins are **3.3 V only**. Connecting a 5 V signal to one can
destroy the pin or the board.

**Open risk — MQ-135 has no voltage divider fitted.** The MQ-135 is powered from
5 V and its analog output is wired **directly** to ADS1115 A0. The ADS1115 runs
from 3.3 V, so its absolute maximum input is about VDD + 0.3 V = 3.6 V. A
5 V-powered MQ-135 can in principle drive its output above that and damage the
ADC. In practice the observed output has stayed well below the limit — the
bench rig measured 1.99 V in clean air and about 2.5 V during a detection event
— so the part is not currently being stressed, but there is no protection if the
sensor output ever swings higher. Fitting a divider, or moving the ADS1115 to a
5 V supply, would remove the risk.

## Pin assignments as built

Physical header pins are as written on the wiring sheet; the BCM numbers are the
ones the software uses.

| Part | Connection | Header pin | BCM | Notes |
|---|---|---|---|---|
| DHT22 | data | 7 | GPIO4 | temperature and humidity |
| DHT22 | power | 3.3 V, GND | — | |
| Reed switch | wire 1 | 11 | GPIO17 | wire 2 to GND |
| LED green | anode | 13 | **GPIO27** | 220 Ω / 330 Ω series resistor, cathode to GND |
| LED yellow | anode | 15 | **GPIO22** | 220 Ω / 330 Ω series resistor, cathode to GND |
| LED red | anode | 16 | **GPIO23** | 220 Ω / 330 Ω series resistor, cathode to GND |
| Buzzer | signal | 18 | **GPIO24** | passive buzzer, driven by PWM |
| Buzzer | ground | GND | — | |
| Fridge light | relay control | 29 | **GPIO5** | relay module switching a lamp |
| Fridge light | ground / return | GND | — | |
| ADS1115 | SDA / SCL | 3 / 5 | GPIO2 / GPIO3 | I²C address 0x48 |
| ADS1115 | power | 3.3 V, GND | — | |
| MQ-135 | analog out | — | — | **direct to ADS1115 A0, no divider** |
| MQ-135 | heater power | 5 V, GND | — | draws significant current; never from a GPIO |
| LCD 16×2 | SDA / SCL | 3 / 5 | GPIO2 / GPIO3 | I²C address 0x27 |
| LCD 16×2 | power | 5 V, GND | — | |
| DS3231 RTC | SDA / SCL | 3 / 5 | GPIO2 / GPIO3 | I²C address 0x68 |
| DS3231 RTC | power | 3.3 V, GND | — | |
| RC522 RFID | SDA / SS | 24 | GPIO8 (CE0) | SPI0 |
| RC522 RFID | SCK | 23 | GPIO11 (SCLK) | SPI0 |
| RC522 RFID | MOSI | 19 | GPIO10 | SPI0 |
| RC522 RFID | MISO | 21 | GPIO9 | SPI0 |
| RC522 RFID | RST | 22 | GPIO25 | |
| RC522 RFID | power | 3.3 V (pin 1), GND | — | 3.3 V only |
| Pi Camera | CSI ribbon | CSI connector | — | not a GPIO device |
| Fan | 5 V / GND | 4 / 6 | — | **reserved**, cooling only |

Three buses share the header and do not conflict: I²C on GPIO2/3 carries the
ADS1115, LCD and DS3231 at three different addresses; SPI0 on GPIO8/9/10/11
carries the RC522 alone; everything else is a plain digital pin.

## Software that does not match the board

The drivers were written from datasheets before the board was wired. The lamp
and buzzer pins are **rotated** relative to the built hardware, so running the
current code on this board would light the wrong lamp and drive the buzzer pin
as an LED.

| Signal | Board (correct) | `core/outputs_real.py` (wrong) |
|---|---|---|
| LED green | GPIO27 | GPIO22 |
| LED yellow | GPIO22 | GPIO23 |
| LED red | GPIO23 | GPIO24 |
| Buzzer | GPIO24 | GPIO27 |

| Signal | Board (correct) | `core/sensors/real.py` |
|---|---|---|
| DHT data | GPIO4, **DHT22** | GPIO4, **DHT11** — pin right, model wrong |
| Reed switch | GPIO17 | GPIO17 — correct |
| Gas channel | ADS1115 A0 | A0 — correct |

Buzzer type also differs: the board has a **passive** buzzer, which needs a PWM
tone. `core/outputs_real.py` drives a steady HIGH, which is what an *active*
buzzer needs and which leaves a passive one silent.

Outstanding software changes, none of them applied yet:

| Constant | File | Change needed |
|---|---|---|
| `LED_PINS` | `core/outputs_real.py` | green 27, yellow 22, red 23 |
| `BUZZER_PIN` | `core/outputs_real.py` | 24, and drive it with PWM |
| `DHT_PIN` model | `core/sensors/real.py` | `adafruit_dht.DHT22`, not `DHT11` |
| — | — | no constant exists for the fridge-light relay (GPIO5) |

The device firmware currently running on the Pi,
`reference/hardware/pi_monitor.py`, already uses GPIO23 for the red LED and
GPIO24 for the buzzer with PWM, so **it matches the board**. It is a standalone
script and shares no code with the application.

## Enabling the buses

`deploy/README.md` currently enables I²C only. SPI must also be enabled for the
RC522:

    sudo raspi-config nointeractive do_i2c 0
    sudo raspi-config nointeractive do_spi 0
    echo "dtoverlay=i2c-rtc,ds3231" | sudo tee -a /boot/firmware/config.txt
    sudo reboot

## Component bench tests

Two components were proven on an ESP32 Dev Module (Arduino IDE, COM3) before
being wired to the Pi. These captures are evidence that the **parts** work; they
are not evidence about the Pi build.

| Component | Rig | Result |
|---|---|---|
| RC522 RFID | ESP32 Dev Module, COM3 | tag UID `09 FE 01 07` read repeatedly |
| MQ-135 via ADS1115 | ESP32 Dev Module, `Adafruit_ADS1X15.h` | clean-air baseline 1.9933 V; detection at ≈2.2–2.5 V |

Confirmed working on the Pi itself, per the build team:

| Component | Evidence held |
|---|---|
| DHT22 + LCD | photograph, LCD showing `Temp:31.1C  Humidity:71%` |
| DS3231 + LCD | photograph, LCD showing `27/09/2026 15:33:52` |
| RC522 RFID | reported working on the Pi; **no serial or log capture on file** |
| Pi Camera | reported wired and working on the Pi; **no capture on file** |

**Gap to close before submission:** the only RC522 capture on file is from the
ESP32 rig. A capture taken on the Pi would make the claim self-evidencing. The
same applies to the camera.

## Software support status

Wired and driven by FreshGuard:

| Part | Status |
|---|---|
| DHT22 | publishing to Firebase from `reference/hardware/pi_monitor.py` |
| MQ-135 / ADS1115 | read by the same script; reporting `I2C_DISCONNECTED` intermittently |
| LCD | reported healthy by the same script |

Wired but with **no software in this repository**:

| Part | Status |
|---|---|
| RC522 RFID | no driver, no library dependency, not in `requirements-pi.txt` |
| Pi Camera | no driver, no imaging library |
| Fridge light relay (GPIO5) | no constant, no control code, not in the SRS scope |
| DS3231 | no driver; used only via the kernel RTC overlay, and shown as a label on the sensors page |
| Reed switch | driver exists in `core/sensors/real.py` but the running firmware sends no door state |

## Verification procedure

Run these on the Pi. Each is a row in the IoT Test Matrix; record the result and
the evidence file. **None of these has been run against the built board yet.**

| # | Step | Expected | Actual | Evidence |
|---|---|---|---|---|
| 1 | `i2cdetect -y 1` | 0x27, 0x48 and 0x68 all present | | |
| 2 | `ls /dev/spidev*` | `spidev0.0` present | | |
| 3 | Output self-test (see below) | each lamp lights in turn, panel shows text, buzzer sounds | | |
| 4 | Start the monitor in real mode | readings appear in the dashboard within a few seconds | | |
| 5 | Breathe on the DHT22 | humidity rises on the dashboard | | |
| 6 | Warm the zone above the category limit | Check Food after three consecutive readings, red lamp, buzzer | | |
| 7 | Unplug the DHT22 | Sensor Fault/Data Unavailable, **never** Fresh | | |
| 8 | Reconnect the DHT22 | readings resume, fault clears | | |
| 9 | Open the door past the timeout | door alert once, not repeatedly | | |
| 10 | Close and reopen past the timeout | alerts again | | |
| 11 | Hold a marker pen near the MQ-135 | zone gas alert naming the **zone**, no item named | | |
| 12 | Present an RFID tag | tag UID read | | |
| 13 | Disconnect the network, generate alerts | local alerts still work, pending count rises | | |
| 14 | Reconnect the network | pending drains to zero, no duplicate events | | |
| 15 | Power-cycle the Pi | services restart, time still correct from the DS3231 | | |

The output self-test, which needs no sensors wired:

    python -c "from core.outputs_real import self_test; self_test()"

Note that until the pin constants are corrected, step 3 will light the wrong
lamps on this board.

## Corrections observed on the board

| Observation | Fix applied |
|---|---|
| | |

import os
import time
import urllib.parse
import urllib.request
import board
import busio
import RPi.GPIO as GPIO
import adafruit_dht
from adafruit_ads1x15.ads1115 import ADS1115
from adafruit_ads1x15.analog_in import AnalogIn

# ----------------- Configurations -----------------
ADS1115_ADDRESS = 0x48      # Standard ADS1115 address
BUZZER_PIN = 24             # GPIO 24 (Physical Pin 18)
RED_LED_PIN = 23            # GPIO 23 (Physical Pin 16)
CHANGE_DELTA = 300          # Reading difference to trigger alert
BUZZER_FREQ = 2700          # Passive buzzer tone frequency (Hz)

# ----------------- ThingSpeak Settings -----------------
THINGSPEAK_WRITE_API_KEY = "R4GNHWDCQE3B3G47"
THINGSPEAK_URL = "https://api.thingspeak.com/update.json"
THINGSPEAK_INTERVAL = 15    # Free tier minimum 15 seconds

# ----------------- Firebase Settings -----------------
# The service-account key is looked for NEXT TO THIS SCRIPT, not in the
# working directory, so it is found the same way whether the script is run
# from Thonny, from a terminal or from a systemd unit.
# Put firebase-key.json in the same folder as this file.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FIREBASE_CRED_PATH = os.path.join(SCRIPT_DIR, "firebase-key.json")
FIREBASE_DB_URL = "https://fresh-guard-aaaf8-default-rtdb.firebaseio.com"
FIREBASE_DEVICE_ID = "pi-01"
FIREBASE_INTERVAL = 15      # Seconds between cloud writes

# 1. GPIO / Buzzer / LED Setup
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

GPIO.setup(RED_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(BUZZER_PIN, GPIO.OUT)

buzzer_pwm = GPIO.PWM(BUZZER_PIN, BUZZER_FREQ)
alert_active = False

def start_alert():
    """Turn on Buzzer and Red LED continuously"""
    global alert_active
    if not alert_active:
        GPIO.output(RED_LED_PIN, GPIO.HIGH)
        buzzer_pwm.start(50)  # 50% duty cycle
        alert_active = True

def stop_alert():
    """Turn off Buzzer and Red LED"""
    global alert_active
    if alert_active:
        GPIO.output(RED_LED_PIN, GPIO.LOW)
        buzzer_pwm.stop()
        alert_active = False

# 2. DHT22 Setup (GPIO 4)
dht_device = adafruit_dht.DHT22(board.D4, use_pulseio=False)

# 3. ADS1115 Setup (MQ-135 on Pin A0)
i2c = busio.I2C(board.SCL, board.SDA)
ads = ADS1115(i2c, address=ADS1115_ADDRESS)
mq135_channel = AnalogIn(ads, 0)

# 4. Function to upload to ThingSpeak
def send_to_thingspeak(temperature, humidity, gas_value):
    if not THINGSPEAK_WRITE_API_KEY:
        return "API Key Missing"

    payload = {
        "api_key": THINGSPEAK_WRITE_API_KEY,
        "field1": "" if temperature is None else f"{temperature:.1f}",
        "field2": "" if humidity is None else f"{humidity:.1f}",
        "field3": "" if gas_value is None else int(gas_value)
    }

    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(THINGSPEAK_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            res = response.read().decode("utf-8", errors="ignore").strip()
            if res and res != "0":
                return f"Uploaded (Entry: {res})"
            return "Rejected by Cloud"
    except Exception as e:
        return f"Upload Failed: {e}"

# 5. Firebase Setup (connected on first use, so a missing library or a bad
#    key never stops the sensors, the buzzer or the LED from working)
firebase_ref = None
firebase_error = None

def connect_firebase():
    """Open the Realtime Database connection once and reuse it."""
    global firebase_ref, firebase_error

    if firebase_ref is not None or firebase_error is not None:
        return firebase_ref

    if not FIREBASE_DB_URL:
        firebase_error = "Not Configured"
        return None

    # Said plainly, because a missing key file is the usual reason the cloud
    # column never changes while the sensors look perfectly healthy.
    if not os.path.isfile(FIREBASE_CRED_PATH):
        firebase_error = f"Key Not Found: {FIREBASE_CRED_PATH}"
        return None

    try:
        import firebase_admin
        from firebase_admin import credentials, db as firebase_db

        try:
            app = firebase_admin.get_app("pi-monitor")
        except ValueError:
            app = firebase_admin.initialize_app(
                credentials.Certificate(FIREBASE_CRED_PATH),
                {"databaseURL": FIREBASE_DB_URL},
                name="pi-monitor",
            )
        firebase_ref = firebase_db.reference("/", app=app)
    except Exception as e:
        firebase_error = f"Connect Failed: {e}"
        return None

    return firebase_ref

# 6. Function to upload to Firebase
def send_to_firebase(temperature, humidity, gas_value, alert_on):
    ref = connect_firebase()
    if ref is None:
        return firebase_error

    payload = {
        "device_id": FIREBASE_DEVICE_ID,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # None is kept as None rather than 0: a missing reading must never
        # look like a real measurement of zero.
        "temp_c": None if temperature is None else round(float(temperature), 1),
        "humidity": None if humidity is None else round(float(humidity), 1),
        "gas_raw": None if gas_value is None else int(gas_value),
        "alert": bool(alert_on),
    }

    try:
        # Two writes: 'live' always holds the latest snapshot and is cheap to
        # read, while 'readings' keeps the history under a generated key.
        ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}").set(payload)
        entry = ref.child(f"pi_monitor/readings/{FIREBASE_DEVICE_ID}").push(payload)
        return f"Uploaded ({entry.key})"
    except Exception as e:
        return f"Upload Failed: {e}"

print("Starting DHT22 + MQ-135 + Buzzer + ThingSpeak + Firebase Monitor...")
print("Press Ctrl+C to stop.\n")
print(f"{'Temperature':<13} | {'Humidity':<10} | {'MQ-135 Raw':<10} | {'Alert':<7} | {'ThingSpeak Status':<28} | {'Firebase Status'}")
print("-" * 110)

baseline_gas_raw = None
last_cloud_upload = 0
last_firebase_upload = 0

try:
    while True:
        temp = None
        hum = None

        # Read DHT22
        try:
            temp = dht_device.temperature
            hum = dht_device.humidity
        except RuntimeError:
            pass  # Normal DHT22 retry

        # Read MQ-135
        gas_raw = mq135_channel.value

        # Baseline set karein
        if baseline_gas_raw is None:
            baseline_gas_raw = gas_raw

        # Alert logic
        diff = abs(gas_raw - baseline_gas_raw)
        if diff >= CHANGE_DELTA:
            start_alert()
            alert_str = "ON"
        else:
            stop_alert()
            alert_str = "OFF"

        # ThingSpeak upload check (har 15 seconds baad)
        cloud_status = "Waiting..."
        now = time.monotonic()
        if now - last_cloud_upload >= THINGSPEAK_INTERVAL:
            cloud_status = send_to_thingspeak(temp, hum, gas_raw)
            last_cloud_upload = now

        # Firebase upload check (apna alag interval, ThingSpeak se independent)
        firebase_status = "Waiting..."
        if now - last_firebase_upload >= FIREBASE_INTERVAL:
            firebase_status = send_to_firebase(temp, hum, gas_raw, alert_str == "ON")
            last_firebase_upload = now

        # Terminal Shell Output
        t_str = f"{temp:.1f} °C" if temp is not None else "Reading..."
        h_str = f"{hum:.1f} %" if hum is not None else "Reading..."
        print(f"{t_str:<13} | {h_str:<10} | {gas_raw:<10} | {alert_str:<7} | {cloud_status:<28} | {firebase_status}")

        time.sleep(2)

except KeyboardInterrupt:
    print("\nStopping system...")
    stop_alert()
    GPIO.cleanup()
    dht_device.exit()

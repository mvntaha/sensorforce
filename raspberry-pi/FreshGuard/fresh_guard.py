import os
import json
import time
import base64
import threading
import urllib.parse
import urllib.request
import board
import busio
import RPi.GPIO as GPIO
import adafruit_dht
from RPLCD.i2c import CharLCD
from adafruit_ads1x15.ads1115 import ADS1115
from adafruit_ads1x15.analog_in import AnalogIn
import firebase_admin
from firebase_admin import credentials, db as firebase_db
from mfrc522 import SimpleMFRC522
import cv2
from picamera2 import Picamera2

# ==================== PINS CONFIGURATION (AS PER CHART) ====================
GREEN_LED_PIN = 27    # Pin 13 (GPIO 27) - Healthy System
YELLOW_LED_PIN = 22   # Pin 15 (GPIO 22) - Use Soon Alert
RED_LED_PIN = 23      # Pin 16 (GPIO 23) - Sensor Fault / Alert
BUZZER_PIN = 24       # Pin 18 (GPIO 24)
REED_PIN = 17         # Pin 11 (GPIO 17)
DHT_PIN = board.D4    # Pin 7 (GPIO 4)

# ==================== ROOM TEMPERATURE THRESHOLDS ====================
TEMP_MIN_SAFE = 18.0   # °C
TEMP_MAX_SAFE = 27.0   # °C
TEMP_MAX_WARN = 30.0   # °C

HUM_MIN_SAFE = 40.0    # %
HUM_MAX_SAFE = 65.0    # %
HUM_MAX_WARN = 75.0    # %

GAS_ALERT_DELTA = 350
DOOR_TIMEOUT_SEC = 10
OFFLINE_BUFFER_FILE = "unsent_buffer.json"

# ==================== CLOUD CONFIGURATION ====================
THINGSPEAK_WRITE_API_KEY = "R4GNHWDCQE3B3G47"
THINGSPEAK_URL = "https://api.thingspeak.com/update.json"
THINGSPEAK_INTERVAL = 15

FIREBASE_DB_URL = "https://fresh-guard-aaaf8-default-rtdb.firebaseio.com"
FIREBASE_DEVICE_ID = "pi-01"
SYNC_INTERVAL = 15

# The service-account key is read from firebase-key.json beside this script,
# never pasted into the code. When the key is rotated, replace that file only.
FIREBASE_CRED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firebase-key.json")

# A Firebase call that cannot finish gives up after this many seconds instead of
# freezing the loop (and with it the buzzer, LEDs and RFID).
FIREBASE_TIMEOUT_SEC = 10

# Dashboard snapshots: each photo is pushed here, one entry per photo, so the
# dashboard keeps a history and two quick requests cannot overwrite each other.
CAPTURES_PATH = f"pi_monitor/captures/{FIREBASE_DEVICE_ID}"

# ==================== HARDWARE SETUP ====================
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

GPIO.setup(GREEN_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(YELLOW_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(RED_LED_PIN, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(BUZZER_PIN, GPIO.OUT, initial=GPIO.LOW)
GPIO.setup(REED_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

rfid_reader = None
try:
    rfid_reader = SimpleMFRC522()
    print("[INIT] RFID RC522 Ready.")
except Exception as e:
    print(f"[INIT ERROR] RFID: {e}")

FOOD_DATABASE = {}
inside_fridge_items = {}
last_rfid_scan_time = 0
RFID_SCAN_COOLDOWN = 1.5
last_scanned_tag_id = None

lcd = None
try:
    lcd = CharLCD(i2c_expander='PCF8574', address=0x27, port=1, cols=16, rows=2, dotsize=8)
    lcd.clear()
    lcd.write_string("FreshGuard Boot")
except Exception:
    pass

dht_device = adafruit_dht.DHT22(DHT_PIN, use_pulseio=False)

i2c = busio.I2C(board.SCL, board.SDA)
ads = None
mq135_channel = None
try:
    ads = ADS1115(i2c, address=0x48)
    mq135_channel = AnalogIn(ads, 0)
    print("[INIT] ADS1115 / MQ-135 Ready.")
except Exception as e:
    print(f"[INIT ERROR] ADS1115: {e}")

db_ref = None
try:
    cred = credentials.Certificate(FIREBASE_CRED_PATH)
    firebase_app = firebase_admin.initialize_app(
        cred,
        {"databaseURL": FIREBASE_DB_URL, "httpTimeout": FIREBASE_TIMEOUT_SEC},
        name="freshguard-system",
    )
    db_ref = firebase_db.reference("/", app=firebase_app)
    # Ensure trigger_snapshot button field exists on boot
    db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}/trigger_snapshot").set(False)
    print("[INIT] Firebase Connected & trigger_snapshot ready.")
except Exception as e:
    print(f"[INIT ERROR] Firebase: {e}")

# ==================== CAMERA CAPTURE FUNCTION ====================
def capture_base64_snapshot():
    picam2 = None
    try:
        picam2 = Picamera2()
        config = picam2.create_still_configuration(main={"size": (640, 480)})
        picam2.configure(config)
        picam2.start()
        time.sleep(0.8)

        frame_rgb = picam2.capture_array()
        picam2.stop()
        picam2.close()
        picam2 = None

        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        success, buffer = cv2.imencode('.jpg', frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 65])
        if not success:
            return None

        b64_str = base64.b64encode(buffer).decode('utf-8')
        return f"data:image/jpeg;base64,{b64_str}"
    except Exception as e:
        print(f"❌ Camera error: {e}")
        if picam2 is not None:
            try:
                picam2.stop()
                picam2.close()
            except Exception:
                pass
        return None

# ==================== DASHBOARD SNAPSHOT (BACKGROUND THREAD) ====================
snapshot_thread = None

def take_dashboard_snapshot():
    """Runs in a background thread when the dashboard asks for a photo.

    Photo background thread mein liya jata hai, taake camera ya network atak
    jaye to bhi buzzer, LEDs, RFID aur readings chalti rahein.

    Order matters: the photo is pushed to the captures list FIRST, and only
    then is trigger_snapshot set back to False. The dashboard treats "flag is
    False but no photo arrived" as a failure, so lowering the flag before the
    photo is saved would look like one.
    """
    ts_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print("\n📸 [DASHBOARD TRIGGER] Snapshot button pressed! Capturing fresh image...")
    snap = capture_base64_snapshot()
    live_ref = db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}")
    if not snap:
        print("❌ Snapshot capture failed. Resetting trigger_snapshot.\n")
        try:
            live_ref.update({"trigger_snapshot": False, "last_snapshot_error": ts_now})
        except Exception:
            pass
        return
    try:
        db_ref.child(CAPTURES_PATH).push({
            "device_id": FIREBASE_DEVICE_ID,
            "ts": ts_now,
            "source": "dashboard",
            "image_base64": snap,
        })
        live_ref.update({
            "image_base64": snap,
            "trigger_snapshot": False,
            "last_snapshot_ts": ts_now,
        })
        print("✅ Snapshot pushed to DB & trigger_snapshot reset to False.\n")
    except Exception as e:
        print(f"❌ Failed to sync dashboard snapshot: {e}")
        try:
            live_ref.update({"trigger_snapshot": False, "last_snapshot_error": ts_now})
        except Exception:
            pass

# ==================== LED INDICATOR ====================
def update_system_leds(freshness_status, sensor_has_fault, door_alert, gas_spike, use_soon_val):
    GPIO.output(GREEN_LED_PIN, GPIO.LOW)
    GPIO.output(YELLOW_LED_PIN, GPIO.LOW)
    GPIO.output(RED_LED_PIN, GPIO.LOW)

    if sensor_has_fault or door_alert or gas_spike or freshness_status in ["Check Food", "Sensor Fault"]:
        GPIO.output(RED_LED_PIN, GPIO.HIGH)
    elif freshness_status == "Use Soon" or use_soon_val > 0:
        GPIO.output(YELLOW_LED_PIN, GPIO.HIGH)
    else:
        GPIO.output(GREEN_LED_PIN, GPIO.HIGH)

# ==================== RFID SCAN & INSTANT DB SYNC ====================
def scan_rfid_inventory():
    global inside_fridge_items, last_rfid_scan_time, last_scanned_tag_id
    if rfid_reader is None:
        return
    now = time.monotonic()
    if now - last_rfid_scan_time < RFID_SCAN_COOLDOWN:
        return

    try:
        tag_id, _ = rfid_reader.read_no_block()
        if tag_id:
            tag_id_str = str(tag_id).strip()
            last_scanned_tag_id = tag_id_str
            last_rfid_scan_time = now

            # RFID Beep
            GPIO.output(BUZZER_PIN, GPIO.HIGH)
            time.sleep(0.08)
            GPIO.output(BUZZER_PIN, GPIO.LOW)

            item_name = FOOD_DATABASE.get(tag_id_str, f"Item-{tag_id_str[-4:]}")
            if tag_id_str in inside_fridge_items:
                del inside_fridge_items[tag_id_str]
                action = "REMOVED"
            else:
                inside_fridge_items[tag_id_str] = item_name
                action = "ADDED"

            current_count = len(inside_fridge_items)
            current_list = list(inside_fridge_items.values())
            timestamp_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            print(f"\n🏷️ [RFID TAP DETECTED] UID: {tag_id_str} | {item_name} {action}! (Count: {current_count})")

            # Direct Instant Write to Firebase
            if db_ref:
                try:
                    # 1. Update live node with tag ID
                    db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}").update({
                        "last_scanned_tag_id": tag_id_str,
                        "last_tag_action": action,
                        "last_tag_timestamp": timestamp_now,
                        "item_count": current_count,
                        "items_inside": current_list
                    })
                    # 2. Push to history log
                    db_ref.child(f"pi_monitor/rfid_history/{FIREBASE_DEVICE_ID}").push({
                        "tag_id": tag_id_str,
                        "item_name": item_name,
                        "action": action,
                        "item_count": current_count,
                        "ts": timestamp_now
                    })
                    print(f"✅ Firebase Updated -> last_scanned_tag_id: {tag_id_str}\n")
                except Exception as err:
                    print(f"❌ Firebase Write Error for RFID: {err}\n")
            else:
                print("⚠️ Firebase offline! RFID tag stored locally in memory only.\n")

    except Exception as e:
        print(f"❌ RFID Reader Exception: {e}")

def evaluate_freshness(temp, hum, gas, gas_baseline, components):
    if any(s != "OK" for s in components.values()) or temp is None or hum is None:
        return "Sensor Fault", ["Sensor Fault / Data Missing"]
    reasons = []
    status = "Fresh/Normal"
    if temp > TEMP_MAX_WARN or temp < (TEMP_MIN_SAFE - 3):
        status = "Check Food"
        reasons.append("Abnormal Temp")
    elif temp > TEMP_MAX_SAFE or temp < TEMP_MIN_SAFE:
        status = "Use Soon"
        reasons.append("Temp Out of Range")

    if hum > HUM_MAX_WARN:
        status = "Check Food"
        reasons.append("High Humidity")
    elif hum > HUM_MAX_SAFE or hum < HUM_MIN_SAFE:
        if status != "Check Food":
            status = "Use Soon"
        reasons.append("Humidity Sub-optimal")

    if gas_baseline is not None and gas is not None:
        if (gas - gas_baseline) >= GAS_ALERT_DELTA:
            status = "Check Food"
            reasons.append("Gas Spike")
    return status, reasons

def send_to_thingspeak(temperature, humidity, gas_value, door_is_open, freshness_status, item_count=0):
    if not THINGSPEAK_WRITE_API_KEY:
        return "No API Key"
    stat_code = 1 if freshness_status == "Fresh/Normal" else (2 if freshness_status == "Use Soon" else 3)
    payload = {
        "api_key": THINGSPEAK_WRITE_API_KEY,
        "field1": "" if temperature is None else f"{temperature:.1f}",
        "field2": "" if humidity is None else f"{humidity:.1f}",
        "field3": "" if gas_value is None else int(gas_value),
        "field4": 1 if door_is_open else 0,
        "field5": stat_code,
        "field6": item_count
    }
    try:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        req = urllib.request.Request(THINGSPEAK_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=5) as response:
            res = response.read().decode("utf-8", errors="ignore").strip()
            return f"Uploaded ({res})"
    except Exception as e:
        return f"TS Err: {e}"

# ==================== MAIN MONITORING LOOP ====================
print("\n[STABILIZE] MQ-135 Baseline set ho raha hai...")
time.sleep(2)

gas_baseline = None
try:
    if mq135_channel:
        gas_baseline = mq135_channel.value
except Exception:
    pass

last_cloud_upload = 0
door_open_start_time = None
use_soon_val = 0

print("=========================================================================================")
print(f"{'TIME':<10} | {'TEMP':<7} | {'HUM':<7} | {'GAS':<6} | {'DOOR':<7} | {'ITEMS':<5} | {'LED':<5} | {'STATUS':<12} | {'FIREBASE'}")
print("=========================================================================================")

try:
    while True:
        components = {"dht22": "OK", "mq135": "OK", "lcd": "OK"}
        temp, hum, gas_raw = None, None, None

        # Check RFID Scan
        scan_rfid_inventory()
        item_count = len(inside_fridge_items)
        items_list = list(inside_fridge_items.values())

        # 1. DHT22 Read
        try:
            temp = dht_device.temperature
            hum = dht_device.humidity
        except (RuntimeError, Exception):
            temp, hum = None, None

        if temp is None or hum is None:
            components["dht22"] = "DISCONNECTED"
        else:
            components["dht22"] = "OK"

        time.sleep(0.04)

        # 2. MQ-135 Read
        if mq135_channel:
            try:
                gas_raw = mq135_channel.value
                if gas_raw == 0 or gas_raw is None:
                    components["mq135"] = "DISCONNECTED"
                else:
                    components["mq135"] = "OK"
            except OSError:
                components["mq135"] = "DISCONNECTED"
                gas_raw = None
        else:
            components["mq135"] = "DISCONNECTED"

        time.sleep(0.04)

        if gas_baseline is None and gas_raw is not None and gas_raw > 0:
            gas_baseline = gas_raw

        # 3. Door Read
        door_open = (GPIO.input(REED_PIN) == GPIO.LOW)
        door_alert = False
        if door_open:
            if door_open_start_time is None:
                door_open_start_time = time.monotonic()
            elif time.monotonic() - door_open_start_time >= DOOR_TIMEOUT_SEC:
                door_alert = True
        else:
            door_open_start_time = None

        # 4. Sensor Fault & Alert Evaluation
        sensor_has_fault = (temp is None) or (hum is None) or (gas_raw is None) or any(s != "OK" for s in components.values())

        # Check values from Firebase (use_soon_count + trigger_snapshot button)
        trigger_requested = False
        if db_ref:
            try:
                val = db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}/use_soon_count").get()
                use_soon_val = int(val) if val is not None else 0

                btn_state = db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}/trigger_snapshot").get()
                trigger_requested = bool(btn_state)
            except Exception:
                pass

        # === ON-DEMAND DASHBOARD SNAPSHOT TRIGGER ===
        # Started in the background; while one photo is still being taken the
        # flag stays True, and the busy thread stops a second one starting.
        if trigger_requested and db_ref:
            if snapshot_thread is None or not snapshot_thread.is_alive():
                snapshot_thread = threading.Thread(target=take_dashboard_snapshot, daemon=True)
                snapshot_thread.start()

        freshness_status, alerts = evaluate_freshness(temp, hum, gas_raw, gas_baseline, components)
        if door_alert:
            alerts.append("Door Left Open!")
            if freshness_status != "Sensor Fault":
                freshness_status = "Check Food"

        gas_spike = (gas_baseline is not None and gas_raw is not None and (gas_raw - gas_baseline) >= GAS_ALERT_DELTA)

        # Buzzer Control
        if door_alert or gas_spike:
            GPIO.output(BUZZER_PIN, GPIO.HIGH)
        else:
            GPIO.output(BUZZER_PIN, GPIO.LOW)

        # LED Indicator
        update_system_leds(freshness_status, sensor_has_fault, door_alert, gas_spike, use_soon_val)

        # 5. LCD Update
        if lcd:
            try:
                lcd.cursor_pos = (0, 0)
                if temp is not None and hum is not None and gas_raw is not None:
                    lcd.write_string(f"T:{temp:.0f}C H:{hum:.0f}% G:{gas_raw}".ljust(16))
                else:
                    lcd.write_string("SENSOR FAULT".ljust(16))

                lcd.cursor_pos = (1, 0)
                if door_alert:
                    lcd.write_string("CLOSE THE DOOR!".ljust(16))
                elif sensor_has_fault:
                    lcd.write_string("DATA UNAVAIL".ljust(16))
                else:
                    stat_short = "Normal" if freshness_status == "Fresh/Normal" else freshness_status
                    lcd.write_string(f"Items:{item_count} {stat_short[:7]}".ljust(16))
            except OSError:
                components["lcd"] = "DISCONNECTED"

        # 6. Har 15 Sec Cloud Update
        now = time.monotonic()
        fb_status = "Idle"

        if now - last_cloud_upload >= SYNC_INTERVAL:
            # Sensor values only. The photo is NOT sent here: it used to ride on
            # every reading (about 46 KB each, roughly 260 MB a day). Photos now
            # go up only when the dashboard asks for one.
            payload = {
                "device_id": FIREBASE_DEVICE_ID,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "item_count": item_count,
                "items_inside": items_list,
                "last_scanned_tag_id": last_scanned_tag_id,
                "freshness_status": freshness_status,
                "door_status": "OPEN" if door_open else "CLOSED",
                "temp_c": None if temp is None else round(float(temp), 1),
                "humidity": None if hum is None else round(float(hum), 1),
                "gas_raw": None if gas_raw is None else int(gas_raw),
                "gas_baseline": None if gas_baseline is None else int(gas_baseline),
                "components": components,
                "active_alerts": alerts
            }

            if db_ref:
                try:
                    db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}").update(payload)
                    db_ref.child(f"pi_monitor/readings/{FIREBASE_DEVICE_ID}").push(payload)
                    fb_status = "Synced"
                except Exception as err:
                    fb_status = f"Err: {err}"
            else:
                fb_status = "Offline"

            send_to_thingspeak(temp, hum, gas_raw, door_open, freshness_status, item_count)
            last_cloud_upload = now

        # --- 7. Real-Time Terminal Shell Readout ---
        t_str = f"{temp:.1f}C" if temp is not None else "--"
        h_str = f"{hum:.1f}%" if hum is not None else "--"
        g_str = f"{gas_raw}" if gas_raw is not None else "--"
        d_str = "OPEN" if door_open else "CLOSED"

        if sensor_has_fault or door_alert or gas_spike or freshness_status in ["Check Food", "Sensor Fault"]:
            led_str = "RED"
        elif freshness_status == "Use Soon" or use_soon_val > 0:
            led_str = "YELLOW"
        else:
            led_str = "GREEN"

        print(f"[{time.strftime('%H:%M:%S')}] | {t_str:<7} | {h_str:<7} | {g_str:<6} | {d_str:<7} | {item_count:<5} | {led_str:<5} | {freshness_status:<12} | {fb_status}")

        time.sleep(1)

except KeyboardInterrupt:
    print("\n[STOP] Shutting down...")
    if lcd:
        try:
            lcd.clear()
        except Exception:
            pass
    GPIO.output(BUZZER_PIN, GPIO.LOW)
    GPIO.output(GREEN_LED_PIN, GPIO.LOW)
    GPIO.output(YELLOW_LED_PIN, GPIO.LOW)
    GPIO.output(RED_LED_PIN, GPIO.LOW)
    GPIO.cleanup()
    dht_device.exit()

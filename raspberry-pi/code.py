import os
import json
import time
import base64
import sqlite3
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

# ==================== PINS CONFIGURATION ====================
GREEN_LED_PIN = 27    # Pin 13 (GPIO 27) - Healthy System (use_soon == 0)
YELLOW_LED_PIN = 22   # Pin 15 (GPIO 22) - Warning (use_soon > 0)
RED_LED_PIN = 23      # Pin 16 (GPIO 23) - Critical Alert / Fault
BUZZER_PIN = 24       # Pin 18 (GPIO 24)
REED_PIN = 17         # Pin 11 (GPIO 17)
DHT_PIN = board.D4    # Pin 7 (GPIO 4)

# ==================== ROOM TEMPERATURE THRESHOLDS ====================
TEMP_MIN_SAFE = 18.0   # °C
TEMP_MAX_SAFE = 27.0   # °C
TEMP_MAX_WARN = 32.0   # °C

HUM_MIN_SAFE = 35.0    # %
HUM_MAX_SAFE = 70.0    # %
HUM_MAX_WARN = 80.0    # %

GAS_ALERT_DELTA = 350
DOOR_TIMEOUT_SEC = 10
LOCAL_STATE_FILE = "local_inventory_state.json"
SQLITE_DB_FILE = "freshguard_offline.db"

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

# ==================== SQLITE OFFLINE BUFFER SETUP ====================
def init_sqlite_db():
    try:
        conn = sqlite3.connect(SQLITE_DB_FILE)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS offline_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payload_json TEXT,
                created_at TEXT
            )
        ''')
        conn.commit()
        conn.close()
        print("[INIT] SQLite Offline Buffer Ready.")
    except Exception as e:
        print(f"[INIT ERROR] SQLite: {e}")

init_sqlite_db()

def buffer_reading_to_sqlite(payload, timestamp_str):
    try:
        conn = sqlite3.connect(SQLITE_DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO offline_readings (payload_json, created_at) VALUES (?, ?)',
            (json.dumps(payload), timestamp_str)
        )
        conn.commit()
        conn.close()
        print("💾 Reading buffered to local SQLite.")
    except Exception as e:
        print(f"❌ SQLite Buffer Error: {e}")

def flush_sqlite_buffer_to_firebase():
    if not db_ref:
        return
    try:
        conn = sqlite3.connect(SQLITE_DB_FILE)
        cursor = conn.cursor()
        cursor.execute('SELECT id, payload_json FROM offline_readings ORDER BY id ASC LIMIT 50')
        rows = cursor.fetchall()
        if not rows:
            conn.close()
            return

        print(f"🔄 Syncing {len(rows)} offline readings from SQLite to Firebase...")
        synced_ids = []
        for row_id, p_str in rows:
            try:
                p_dict = json.loads(p_str)
                db_ref.child(f"pi_monitor/readings/{FIREBASE_DEVICE_ID}").push(p_dict)
                synced_ids.append(row_id)
            except Exception:
                break

        if synced_ids:
            cursor.execute(f"DELETE FROM offline_readings WHERE id IN ({','.join(['?']*len(synced_ids))})", synced_ids)
            conn.commit()
            print(f"✅ Synced and cleared {len(synced_ids)} readings from SQLite.")
        conn.close()
    except Exception as e:
        print(f"⚠️ Flush Buffer Warning: {e}")

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
RFID_SCAN_COOLDOWN = 0.8
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

# ==================== DS3231 RTC SETUP ====================
rtc_device = None
try:
    import adafruit_ds3231
    rtc_device = adafruit_ds3231.DS3231(i2c)
    print("[INIT] DS3231 RTC Module Ready (0x68).")
except Exception as e:
    print(f"[INIT NOTE] DS3231 module not initialized ({e}), using system clock.")

def get_accurate_time_iso():
    """DS3231 RTC se exact offline time uthata hai, warna system clock fallback karta hai"""
    if rtc_device:
        try:
            t = rtc_device.datetime
            return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}T{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}Z"
        except Exception:
            pass
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

db_ref = None
try:
    cred = credentials.Certificate(FIREBASE_CRED_PATH)
    firebase_app = firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL}, name="freshguard-system")
    db_ref = firebase_db.reference("/", app=firebase_app)
    db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}/trigger_snapshot").set(False)
    print("[INIT] Firebase Connected.")
except Exception as e:
    print(f"[INIT ERROR] Firebase: {e}")

# ==================== STATE PERSISTENCE ====================
def save_local_state():
    try:
        with open(LOCAL_STATE_FILE, "w") as f:
            json.dump(inside_fridge_items, f)
    except Exception:
        pass

def load_initial_state():
    global inside_fridge_items, last_scanned_tag_id
    loaded = False
    if db_ref:
        try:
            live_data = db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}").get()
            if live_data and isinstance(live_data, dict):
                saved_tags = live_data.get("items_inside_tags")
                if saved_tags and isinstance(saved_tags, dict):
                    inside_fridge_items = saved_tags
                    loaded = True
                elif "items_inside" in live_data and isinstance(live_data["items_inside"], list):
                    inside_fridge_items = {f"restored_{idx}": item for idx, item in enumerate(live_data["items_inside"])}
                    loaded = True
                last_scanned_tag_id = live_data.get("last_scanned_tag_id")
                if loaded:
                    print(f"📦 [RESTORED] {len(inside_fridge_items)} items recovered.")
        except Exception:
            pass

    if not loaded and os.path.exists(LOCAL_STATE_FILE):
        try:
            with open(LOCAL_STATE_FILE, "r") as f:
                inside_fridge_items = json.load(f)
        except Exception:
            inside_fridge_items = {}

load_initial_state()

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

        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        success, buffer = cv2.imencode('.jpg', frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 65])
        if not success:
            return None
        return f"data:image/jpeg;base64,{base64.b64encode(buffer).decode('utf-8')}"
    except Exception:
        if picam2 is not None:
            try:
                picam2.stop()
                picam2.close()
            except Exception:
                pass
        return None

# ==================== RFID SCAN & INSTANT SYNC ====================
def scan_rfid_inventory():
    global inside_fridge_items, last_rfid_scan_time, last_scanned_tag_id
    if rfid_reader is None:
        return
    now = time.monotonic()
    if now - last_rfid_scan_time < RFID_SCAN_COOLDOWN:
        return

    tag_id = None
    try:
        tag_id, _ = rfid_reader.read_no_block()
    except Exception:
        return

    if tag_id:
        tag_id_str = str(tag_id).strip()
        last_scanned_tag_id = tag_id_str
        last_rfid_scan_time = now

        # Beep confirmation
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

        save_local_state()
        current_count = len(inside_fridge_items)
        current_list = list(inside_fridge_items.values())
        timestamp_now = get_accurate_time_iso()

        print(f"\n🏷️ [RFID TAP DETECTED] UID: {tag_id_str} | {item_name} {action}! (Count: {current_count})")

        if db_ref:
            try:
                db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}").update({
                    "last_scanned_tag_id": tag_id_str,
                    "last_tag_action": action,
                    "last_tag_timestamp": timestamp_now,
                    "item_count": current_count,
                    "items_inside": current_list,
                    "items_inside_tags": inside_fridge_items
                })
                db_ref.child(f"pi_monitor/rfid_history/{FIREBASE_DEVICE_ID}").push({
                    "tag_id": tag_id_str,
                    "item_name": item_name,
                    "action": action,
                    "item_count": current_count,
                    "ts": timestamp_now
                })
                print("🔥 RFID Synced to Firebase!\n")
            except Exception as err:
                print(f"❌ RFID Sync Error: {err}\n")

def send_to_thingspeak(temperature, humidity, gas_value, door_is_open, freshness_status, item_count=0):
    if not THINGSPEAK_WRITE_API_KEY:
        return "No API Key"
    stat_code = 1 if freshness_status == "Fresh/Normal" or freshness_status == "Normal" else (2 if "Soon" in freshness_status else 3)
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
print("\n[STABILIZE] MQ-135 Baseline setting...")
time.sleep(2)

gas_baseline = None
try:
    if mq135_channel:
        gas_baseline = mq135_channel.value
except Exception:
    pass

last_cloud_upload = 0
door_open_start_time = None
latest_img_b64 = None
use_soon_val = 0

print("=========================================================================================")
print(f"{'TIME':<10} | {'TEMP':<7} | {'HUM':<7} | {'GAS':<6} | {'DOOR':<7} | {'ITEMS':<5} | {'LED':<6} | {'STATUS':<16} | {'FIREBASE'}")
print("=========================================================================================")

try:
    while True:
        components = {"dht22": "OK", "mq135": "OK", "lcd": "OK"}
        temp, hum, gas_raw = None, None, None

        # RFID check call
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

        scan_rfid_inventory()

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

        if gas_baseline is None and gas_raw is not None and gas_raw > 0:
            gas_baseline = gas_raw

        # 3. Door Read (Testing: HIGH = Open)
        door_open = (GPIO.input(REED_PIN) == GPIO.HIGH)
        door_alert = False
        if door_open:
            if door_open_start_time is None:
                door_open_start_time = time.monotonic()
            elif time.monotonic() - door_open_start_time >= DOOR_TIMEOUT_SEC:
                door_alert = True
        else:
            door_open_start_time = None

        # 4. Critical Alert Checks
        sensor_has_fault = (temp is None) or (hum is None) or (gas_raw is None) or any(s != "OK" for s in components.values())
        gas_spike = (gas_baseline is not None and gas_raw is not None and (gas_raw - gas_baseline) >= GAS_ALERT_DELTA)
        temp_critical = (temp is not None) and (temp > TEMP_MAX_WARN or temp < (TEMP_MIN_SAFE - 3))

        # Check values from Firebase
        trigger_requested = False
        if db_ref:
            try:
                val = db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}/use_soon_count").get()
                if val is not None:
                    use_soon_val = int(val)
                else:
                    use_soon_val = 0

                btn_state = db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}/trigger_snapshot").get()
                trigger_requested = bool(btn_state)
            except Exception:
                pass

        # === LIVE DASHBOARD ON-DEMAND CAMERA CAPTURE ===
        current_time_iso = get_accurate_time_iso()

        if trigger_requested:
            print("\n📸 [TRIGGER] Capturing live snapshot...")
            snap = capture_base64_snapshot()
            if snap:
                latest_img_b64 = snap
                try:
                    db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}").update({
                        "image_base64": latest_img_b64,
                        "trigger_snapshot": False,
                        "last_snapshot_ts": current_time_iso
                    })
                    print("✅ Snapshot synced.\n")
                except Exception:
                    pass
            else:
                try:
                    db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}/trigger_snapshot").set(False)
                except Exception:
                    pass

        # Buzzer Control
        if door_alert or gas_spike:
            GPIO.output(BUZZER_PIN, GPIO.HIGH)
        else:
            GPIO.output(BUZZER_PIN, GPIO.LOW)

        # ==================== STRICT PRIORITY STATUS & LED LOGIC ====================
        GPIO.output(GREEN_LED_PIN, GPIO.LOW)
        GPIO.output(YELLOW_LED_PIN, GPIO.LOW)
        GPIO.output(RED_LED_PIN, GPIO.LOW)

        # Priority 1: Sensor Hardware Fault
        if sensor_has_fault:
            GPIO.output(RED_LED_PIN, GPIO.HIGH)
            led_str = "RED"
            status_text = "Sensor Fault"
            lcd_line2 = "DATA UNAVAIL"

        # Priority 2: Gas Spike Alert
        elif gas_spike:
            GPIO.output(RED_LED_PIN, GPIO.HIGH)
            led_str = "RED"
            status_text = "Gas Alert"
            lcd_line2 = "GAS ALERT!"

        # Priority 3: Door Left Open
        elif door_alert:
            GPIO.output(RED_LED_PIN, GPIO.HIGH)
            led_str = "RED"
            status_text = "Door Left Open"
            lcd_line2 = "CLOSE THE DOOR!"

        # Priority 4: Abnormal Temperature
        elif temp_critical:
            GPIO.output(RED_LED_PIN, GPIO.HIGH)
            led_str = "RED"
            status_text = "Check Food"
            lcd_line2 = "TEMP ALERT!"

        # Priority 5: Yellow Warning (use_soon_val > 0)
        elif use_soon_val > 0:
            GPIO.output(YELLOW_LED_PIN, GPIO.HIGH)
            led_str = "YELLOW"
            status_text = f"Use Soon ({use_soon_val})"
            lcd_line2 = f"Soon:{use_soon_val}"

        # Priority 6: Healthy / Normal (use_soon_val == 0 aur koi alert nahi)
        else:
            GPIO.output(GREEN_LED_PIN, GPIO.HIGH)
            led_str = "GREEN"
            status_text = "Normal"
            lcd_line2 = "Normal"

        # 5. LCD Update
        if lcd:
            try:
                lcd.cursor_pos = (0, 0)
                if temp is not None and hum is not None and gas_raw is not None:
                    lcd.write_string(f"T:{temp:.0f}C H:{hum:.0f}% G:{gas_raw}".ljust(16))
                else:
                    lcd.write_string("SENSOR FAULT".ljust(16))

                lcd.cursor_pos = (1, 0)
                lcd.write_string(f"Itm:{item_count} {lcd_line2}".ljust(16))
            except OSError:
                components["lcd"] = "DISCONNECTED"

        # 6. Har 15 Sec Cloud Update + SQLite Fallback
        now = time.monotonic()
        fb_status = "Idle"

        if now - last_cloud_upload >= SYNC_INTERVAL:
            if latest_img_b64 is None:
                snap = capture_base64_snapshot()
                if snap:
                    latest_img_b64 = snap

            payload = {
                "device_id": FIREBASE_DEVICE_ID,
                "ts": current_time_iso,
                "item_count": item_count,
                "items_inside": items_list,
                "items_inside_tags": inside_fridge_items,
                "last_scanned_tag_id": last_scanned_tag_id,
                "freshness_status": status_text,
                "door_status": "OPEN" if door_open else "CLOSED",
                "temp_c": None if temp is None else round(float(temp), 1),
                "humidity": None if hum is None else round(float(hum), 1),
                "gas_raw": None if gas_raw is None else int(gas_raw),
                "gas_baseline": None if gas_baseline is None else int(gas_baseline),
                "components": components,
                "image_base64": latest_img_b64
            }

            uploaded = False
            if db_ref:
                try:
                    db_ref.child(f"pi_monitor/live/{FIREBASE_DEVICE_ID}").update(payload)
                    db_ref.child(f"pi_monitor/readings/{FIREBASE_DEVICE_ID}").push(payload)
                    fb_status = "Synced"
                    uploaded = True
                    # Purani offline buffered readings flush karein
                    flush_sqlite_buffer_to_firebase()
                except Exception as err:
                    fb_status = f"Err: {err}"

            # Agar Firebase upload fail hua ya internet offline ho
            if not uploaded:
                fb_status = "Buffered (SQL)"
                offline_p = payload.copy()
                offline_p["image_base64"] = None  # DB size optimize rakhne ke liye image buffer nahi karte
                buffer_reading_to_sqlite(offline_p, current_time_iso)

            send_to_thingspeak(temp, hum, gas_raw, door_open, status_text, item_count)
            last_cloud_upload = now

        # --- 7. Real-Time Terminal Shell Readout ---
        t_str = f"{temp:.1f}C" if temp is not None else "--"
        h_str = f"{hum:.1f}%" if hum is not None else "--"
        g_str = f"{gas_raw}" if gas_raw is not None else "--"
        d_str = "OPEN" if door_open else "CLOSED"

        print(f"[{time.strftime('%H:%M:%S')}] | {t_str:<7} | {h_str:<7} | {g_str:<6} | {d_str:<7} | {item_count:<5} | {led_str:<6} | {status_text:<16} | {fb_status}")

        for _ in range(5):
            scan_rfid_inventory()
            time.sleep(0.1)

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
"""
detect.py  –  Sentinel AI standalone detector (no server needed)
Exit:  press Q or ESC in the OpenCV window, or Ctrl+C in terminal.
"""

import cv2
import time
import os
import threading
import signal
import sys
from datetime import datetime
from ultralytics import YOLO

try:
    import winsound
    HAS_SOUND = True
except ImportError:
    HAS_SOUND = False

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────
CONF_THRESHOLD  = 0.45
IMGSZ           = 320
SAVE_INTERVAL   = 2      # seconds between saved screenshots
BEEP_FREQ       = 1800
BEEP_DURATION   = 400

# ─────────────────────────────────────────────
#  Load model
# ─────────────────────────────────────────────
print("[SENTINEL AI] Loading model …")
model = YOLO("best.pt")
print(f"[SENTINEL AI] Classes: {model.names}")

# ─────────────────────────────────────────────
#  Camera
# ─────────────────────────────────────────────
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)

if not cap.isOpened():
    print("[ERROR] Camera not detected")
    sys.exit(1)

cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)

# ─────────────────────────────────────────────
#  Storage
# ─────────────────────────────────────────────
os.makedirs("alerts", exist_ok=True)
log_path = os.path.join(os.getcwd(), "weapon_log.txt")
open(log_path, "a").close()

# ─────────────────────────────────────────────
#  Globals
# ─────────────────────────────────────────────
running        = True
prev_time      = 0
alert_count    = 0
last_save_time = 0

# ─────────────────────────────────────────────
#  Clean shutdown helpers
# ─────────────────────────────────────────────
def cleanup():
    print("\n[SENTINEL AI] Shutting down …")
    try: cap.release()
    except: pass
    try: cv2.destroyAllWindows()
    except: pass
    for _ in range(4):          # drain the OpenCV message queue
        try: cv2.waitKey(1)
        except: pass
    print("[SENTINEL AI] Closed ✓")

def sig_handler(sig, frame):
    global running
    running = False

signal.signal(signal.SIGINT,  sig_handler)
signal.signal(signal.SIGTERM, sig_handler)

# ─────────────────────────────────────────────
#  Visual helpers
# ─────────────────────────────────────────────
def get_threat(conf):
    if conf >= 0.85: return "CRITICAL", (0, 0, 255)
    if conf >= 0.70: return "HIGH",     (0, 60, 255)
    return "MEDIUM", (0, 140, 255)

def draw_glow(frame, x1, y1, x2, y2):
    x1 = max(0, x1 - 8);  y1 = max(0, y1 - 8)
    x2 = min(frame.shape[1], x2 + 8)
    y2 = min(frame.shape[0], y2 + 8)
    ov = frame.copy()
    cv2.rectangle(ov, (x1, y1), (x2, y2), (0, 0, 255), -1)
    return cv2.addWeighted(ov, 0.18, frame, 0.82, 0)

def beep_async():
    if HAS_SOUND:
        threading.Thread(
            target=lambda: winsound.Beep(BEEP_FREQ, BEEP_DURATION),
            daemon=True
        ).start()

# ─────────────────────────────────────────────
#  Main loop
# ─────────────────────────────────────────────
print("[SENTINEL AI] Running – press Q or ESC to quit")

try:
    frame_count = 0
    while running:
        ret, frame = cap.read()
        if not ret:
            print("[WARN] Frame dropped")
            time.sleep(0.02)
            continue

        frame_count += 1
        weapon_detected = False

        # ── YOLO detection ─────────────────────────────────────
        results = model(frame, conf=CONF_THRESHOLD,
                        imgsz=IMGSZ, verbose=False)[0]

        for box in results.boxes:
            cls  = int(box.cls[0])
            conf = float(box.conf[0])

            if cls not in [0, 1]:
                continue

            weapon_detected = True
            label           = model.names[cls].upper()
            threat, color   = get_threat(conf)

            x1, y1, x2, y2 = map(int, box.xyxy[0])

            frame = draw_glow(frame, x1, y1, x2, y2)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)

            text = f"{label} {conf:.0%} [{threat}]"
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, 0.7, 2)
            ty = max(34, y1 - 8)
            cv2.rectangle(frame, (x1, ty - th - 6),
                          (x1 + tw + 8, ty + 4), color, -1)
            cv2.putText(frame, text, (x1 + 4, ty),
                        cv2.FONT_HERSHEY_DUPLEX, 0.7, (255, 255, 255), 2)

            # ── Save alert ────────────────────────────────────
            if time.time() - last_save_time > SAVE_INTERVAL:
                last_save_time = time.time()
                alert_count   += 1

                ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
                path = f"alerts/weapon_{ts}.jpg"
                cv2.imwrite(path, frame)

                log_line = (
                    f"{ts} | ALERT #{alert_count} | "
                    f"{label} | CONF {conf:.2f} | THREAT {threat}\n"
                )
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write(log_line)

                beep_async()
                print(f"[ALERT #{alert_count}] {label} {conf:.0%} [{threat}]")

            break   # one detection per frame is enough

        # ── Big banner ─────────────────────────────────────────
        if weapon_detected:
            h, w, _ = frame.shape
            ov = frame.copy()
            cv2.rectangle(ov, (0, 0), (w, 50), (0, 0, 180), -1)
            frame = cv2.addWeighted(ov, 0.55, frame, 0.45, 0)
            cv2.putText(frame, "  *** WEAPON DETECTED ***",
                        (10, 36), cv2.FONT_HERSHEY_DUPLEX,
                        1.1, (255, 255, 255), 2)

        # ── HUD ────────────────────────────────────────────────
        curr = time.time()
        fps  = round(1 / (curr - prev_time)) if prev_time else 0
        prev_time = curr

        h, w, _ = frame.shape
        cv2.putText(frame, f"FPS {fps}",
                    (10, h - 40), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 220, 0), 2)
        cv2.putText(frame, f"ALERTS {alert_count}",
                    (10, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (80, 255, 80), 2)
        cv2.putText(frame, datetime.now().strftime("%Y-%m-%d  %H:%M:%S"),
                    (w - 230, h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1)

        # ── Show ───────────────────────────────────────────────
        cv2.imshow("Sentinel AI – Weapon Detection", frame)

        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), ord("Q"), 27):   # Q or ESC
            running = False

except Exception as e:
    print(f"[ERROR] {e}")

finally:
    cleanup()

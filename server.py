import cv2
import sqlite3
import os
import sys
import threading
import time
import smtplib
import numpy as np
import signal
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from ultralytics import YOLO
import torch

# ─────────────────────────────────────────────
# Cross-platform beep (Windows only, silent on Linux/cloud)
# ─────────────────────────────────────────────
def _beep():
    try:
        if sys.platform == "win32":
            import winsound
            winsound.Beep(1800, 350)
    except Exception:
        pass

# ─────────────────────────────────────────────
# Globals
# ─────────────────────────────────────────────
frame_lock          = threading.Lock()
db_lock             = threading.Lock()
latest_frame        = None
processed_frame     = None
last_boxes          = []
last_detection_time = 0.0
last_save_time      = 0.0
alert_count         = 0
prev_time           = 0.0
heatmap             = None
running             = True
start_time          = time.time()
model               = None
cap                 = None

# ─────────────────────────────────────────────
# Ctrl+C handler
# ─────────────────────────────────────────────
def _sigint_handler(sig, frame):
    global running
    print("\n[CTRL+C] Shutting down …")
    running = False
    try:
        if cap and cap.isOpened():
            cap.release()
    except Exception:
        pass
    def _kill():
        time.sleep(0.5)
        os._exit(0)
    threading.Thread(target=_kill, daemon=True).start()

try:
    signal.signal(signal.SIGINT, _sigint_handler)
except Exception:
    pass

# ─────────────────────────────────────────────
# Email
# ─────────────────────────────────────────────
EMAIL_USER     = os.getenv("EMAIL_USER", "").strip()
EMAIL_PASS     = os.getenv("EMAIL_PASS", "").strip()
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", "").strip()

def send_email(image_path: str = "", conf: float = 0.0):
    if not EMAIL_USER or not EMAIL_PASS or not EMAIL_RECEIVER:
        print("[EMAIL] credentials not set – skipping"); return
    if EMAIL_PASS == "your_16char_app_password":
        print("[EMAIL] ERROR: set real Gmail App Password in .env"); return
    try:
        msg = MIMEMultipart()
        msg["Subject"] = "⚠ Sentinel AI – WEAPON DETECTED"
        msg["From"]    = EMAIL_USER
        msg["To"]      = EMAIL_RECEIVER
        msg.attach(MIMEText(
            f"⚠ WEAPON DETECTED\n\n"
            f"Time       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Confidence : {conf*100:.0f}%\n\n-- Sentinel AI", "plain"))
        if image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                            f"attachment; filename={os.path.basename(image_path)}")
            msg.attach(part)
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(EMAIL_USER, EMAIL_PASS)
            s.send_message(msg)
        print("[EMAIL] Sent ✓")
    except Exception as e:
        print(f"[EMAIL] Error: {e}")

# ─────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────
os.makedirs("alerts", exist_ok=True)
DB_PATH  = "detections.db"
log_path = os.path.join(os.getcwd(), "weapon_log.txt")

def init_db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS detections")
    cur.execute("""CREATE TABLE detections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        weapon_type TEXT NOT NULL DEFAULT 'WEAPON',
        confidence REAL NOT NULL DEFAULT 0.0,
        image TEXT NOT NULL)""")
    c.commit(); cur.close()
    print("[DB] Ready ✓")
    return c

conn = init_db()

def get_cursor():
    return conn.cursor()

def clear_old_data():
    for f in os.listdir("alerts"):
        try: os.remove(os.path.join("alerts", f))
        except: pass
    open(log_path, "w").close()
    print("[STARTUP] Old data cleared ✓")

# ─────────────────────────────────────────────
# NMS helper — removes overlapping boxes
# ─────────────────────────────────────────────
def apply_nms(boxes, iou_threshold=0.40):
    if not boxes:
        return []
    boxes_sorted = sorted(boxes, key=lambda b: b[4], reverse=True)
    kept = []
    while boxes_sorted:
        best = boxes_sorted.pop(0)
        kept.append(best)
        remaining = []
        for b in boxes_sorted:
            ix1 = max(best[0], b[0]); iy1 = max(best[1], b[1])
            ix2 = min(best[2], b[2]); iy2 = min(best[3], b[3])
            inter = max(0, ix2-ix1) * max(0, iy2-iy1)
            area_best = (best[2]-best[0]) * (best[3]-best[1])
            area_b    = (b[2]-b[0]) * (b[3]-b[1])
            union = area_best + area_b - inter
            iou = inter / union if union > 0 else 0
            if iou < iou_threshold:
                remaining.append(b)
        boxes_sorted = remaining
    return kept

# ─────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, cap
    clear_old_data()
    print("[STARTUP] Loading YOLO model …")
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model  = YOLO("best.pt").to(device)
    dummy  = np.zeros((480, 640, 3), dtype=np.uint8)
    model(dummy, imgsz=416, verbose=False)
    model(dummy, imgsz=416, verbose=False)
    print(f"[STARTUP] Model ready on {device.upper()} ✓")
    # Use CAP_DSHOW on Windows, default on Linux
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_V4L2
    cap = cv2.VideoCapture(0, backend)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS,          30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)
    threading.Thread(target=camera_capture, daemon=True).start()
    threading.Thread(target=detection_loop,  daemon=True).start()
    print("[STARTUP] Live ✓")
    yield
    global running
    running = False
    time.sleep(0.2)
    try:
        if cap and cap.isOpened(): cap.release()
    except: pass
    try: conn.close()
    except: pass
    print("[SHUTDOWN] Done ✓")

# ─────────────────────────────────────────────
# App
# ─────────────────────────────────────────────
app = FastAPI(title="Sentinel AI", version="4.1", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ─────────────────────────────────────────────
# Drawing helpers
# ─────────────────────────────────────────────
def _pulse():
    return (np.sin(time.time() * 3.0) + 1.0) / 2.0


def draw_detection(frame, x1, y1, x2, y2, conf):
    p     = _pulse()
    RED   = (0, 0, 255)
    WHITE = (255, 255, 255)
    BLACK = (0, 0, 0)

    cv2.rectangle(frame, (x1, y1), (x2, y2), RED, 2)

    arm, t = 20, 3
    for (cx, cy, sx, sy) in [(x1,y1,1,1),(x2,y1,-1,1),(x1,y2,1,-1),(x2,y2,-1,-1)]:
        cv2.line(frame, (cx, cy), (cx + sx*arm, cy),        WHITE, t)
        cv2.line(frame, (cx, cy), (cx,           cy+sy*arm), WHITE, t)

    cx_w = int((x1 + x2) / 2)
    cy_w = int((y1 + y2) / 2)
    box_short = min(x2-x1, y2-y1)
    base_r    = max(12, int(box_short * 0.18))
    pulse_r   = base_r + int(4 * p)
    cv2.circle(frame, (cx_w, cy_w), pulse_r, RED, 2)
    cv2.circle(frame, (cx_w, cy_w), 4, RED, -1)
    cv2.circle(frame, (cx_w, cy_w), 4, WHITE, 1)

    label  = f"WEAPON DETECTED  {conf*100:.0f}%"
    font   = cv2.FONT_HERSHEY_DUPLEX
    fscale = 0.70
    thick  = 2
    (tw, th), bl = cv2.getTextSize(label, font, fscale, thick)
    ty = max(th + 12, y1 - 8)
    cv2.rectangle(frame, (x1, ty - th - 8), (x1 + tw + 10, ty + bl + 2), BLACK, -1)
    cv2.rectangle(frame, (x1, ty - th - 8), (x1 + tw + 10, ty + bl + 2), RED,   1)
    cv2.putText(frame, label, (x1 + 5, ty), font, fscale, RED, thick)
    return frame


def draw_banner(frame):
    p = _pulse()
    if p < 0.5:
        return frame
    h, w = frame.shape[:2]
    ov = frame.copy()
    cv2.rectangle(ov, (0, 0), (w, 50), (0, 0, 180), -1)
    frame = cv2.addWeighted(ov, 0.65, frame, 0.35, 0)
    cv2.putText(frame, "  \u26a0  WEAPON DETECTED  \u26a0",
                (8, 36), cv2.FONT_HERSHEY_DUPLEX, 0.95, (0, 0, 255), 2)
    return frame


def update_heatmap(frame, boxes):
    global heatmap
    h, w = frame.shape[:2]
    if heatmap is None or heatmap.shape[:2] != (h, w):
        heatmap = np.zeros((h, w), dtype=np.float32)
    for (x1, y1, x2, y2) in boxes:
        cx = int((x1+x2)/2); cy = int((y1+y2)/2)
        r  = max(18, int(min(x2-x1, y2-y1) * 0.20))
        cv2.circle(heatmap, (cx, cy), r, 0.8, -1)
    heatmap *= 0.94
    if heatmap.max() < 0.05:
        return frame
    norm = heatmap / float(heatmap.max())
    heat = cv2.applyColorMap(np.uint8(255 * norm), cv2.COLORMAP_INFERNO)
    return cv2.addWeighted(frame, 0.85, heat, 0.15, 0)

# ─────────────────────────────────────────────
# Camera thread
# ─────────────────────────────────────────────
def camera_capture():
    global latest_frame
    while running:
        if cap is None: time.sleep(0.01); continue
        ret, frame = cap.read()
        if not ret:     time.sleep(0.01); continue
        with frame_lock:
            latest_frame = frame

# ─────────────────────────────────────────────
# Detection thread
# ─────────────────────────────────────────────
CONF_THRESHOLD = 0.55
MIN_BOX_PX     = 35
VISIBILITY_WIN = 0.45

def detection_loop():
    global latest_frame, processed_frame
    global last_boxes, last_detection_time, last_save_time, alert_count

    while running:
        if latest_frame is None:
            time.sleep(0.01); continue

        with frame_lock:
            frame = latest_frame.copy()

        raw_boxes = []
        try:
            results = model(frame, conf=CONF_THRESHOLD, iou=0.35,
                            imgsz=416, verbose=False)[0]
            for box in results.boxes:
                conf = float(box.conf[0])
                if conf < CONF_THRESHOLD: continue
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                w_box = x2 - x1
                h_box = y2 - y1
                if w_box < MIN_BOX_PX or h_box < MIN_BOX_PX: continue
                fh, fw = frame.shape[:2]
                if w_box > fw * 0.80 or h_box > fh * 0.80: continue
                aspect = w_box / h_box if h_box > 0 else 0
                if aspect < 0.10 or aspect > 12.0: continue
                raw_boxes.append((x1, y1, x2, y2, conf))
        except Exception as e:
            print(f"[YOLO] {e}")

        boxes = apply_nms(raw_boxes, iou_threshold=0.40)

        if boxes:
            last_boxes          = boxes
            last_detection_time = time.time()
        else:
            last_boxes = []

        detection_visible = (
            len(last_boxes) > 0 and
            (time.time() - last_detection_time) < VISIBILITY_WIN
        )

        if detection_visible:
            for (x1, y1, x2, y2, conf) in last_boxes:
                frame = draw_detection(frame, x1, y1, x2, y2, conf)
            frame = draw_banner(frame)
            frame = update_heatmap(frame, [(b[0],b[1],b[2],b[3]) for b in last_boxes])
        else:
            if heatmap is not None:
                heatmap.__imul__(0.94)

        if detection_visible and boxes and (time.time() - last_save_time) > 5:
            last_save_time = time.time()
            alert_count   += 1
            save_conf      = max(b[4] for b in boxes)
            ts             = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename       = f"alerts/weapon_{ts}.jpg"
            cv2.imwrite(filename, frame)
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"{ts} | ALERT #{alert_count} | WEAPON DETECTED | CONF={save_conf:.2f}\n")
            except: pass
            try:
                with db_lock:
                    cur = get_cursor()
                    cur.execute("INSERT INTO detections (timestamp,weapon_type,confidence,image) VALUES (?,?,?,?)",
                                (ts, "WEAPON", round(save_conf, 4), filename))
                    conn.commit(); cur.close()
            except Exception as e:
                print(f"[DB] {e}")
            threading.Thread(target=_beep, daemon=True).start()
            threading.Thread(target=send_email, args=(filename, save_conf), daemon=True).start()
            print(f"[ALERT #{alert_count}] WEAPON | CONF={save_conf:.2f}")

        with frame_lock:
            processed_frame = frame

        time.sleep(0.03)

# ─────────────────────────────────────────────
# MJPEG stream — 25 fps
# ─────────────────────────────────────────────
def frame_generator():
    global prev_time
    interval  = 1.0 / 25.0
    last_sent = 0.0

    while running:
        now  = time.time()
        wait = interval - (now - last_sent)
        if wait > 0:
            time.sleep(wait)

        with frame_lock:
            if processed_frame is None: continue
            frame = processed_frame.copy()
        last_sent = time.time()

        curr      = time.time()
        fps       = round(1/(curr-prev_time)) if prev_time and (curr-prev_time) > 0 else 0
        prev_time = curr

        h, w = frame.shape[:2]
        cv2.putText(frame, f"FPS {fps}",
                    (10, h-40), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (255,220,0), 2)
        cv2.putText(frame, f"ALERTS {alert_count}",
                    (10, h-16), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (80,255,80), 2)
        cv2.putText(frame, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    (w-230, h-12), cv2.FONT_HERSHEY_SIMPLEX, 0.50, (160,160,160), 1)

        ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
        if not ret: continue
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")

# ─────────────────────────────────────────────
# API Routes
# ─────────────────────────────────────────────
@app.get("/")
def home(): return {"message": "Sentinel AI Running", "version": "4.1"}

@app.get("/health")
def health(): return {"status": "ok", "alerts": alert_count}

@app.get("/video")
def video():
    return StreamingResponse(frame_generator(),
                             media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/stats")
def stats():
    try:
        with db_lock:
            cur = get_cursor()
            cur.execute("SELECT COUNT(*) FROM detections")
            total = (cur.fetchone() or (0,))[0]
            cur.execute("SELECT AVG(confidence) FROM detections")
            avg = (cur.fetchone() or (0,))[0] or 0
            cur.close()
        return {"alerts": total, "avg_conf": round(avg*100, 1),
                "uptime": int(time.time()-start_time), "status": "ACTIVE"}
    except:
        return {"alerts": alert_count, "avg_conf": 0, "uptime": 0, "status": "ACTIVE"}

@app.get("/history")
def history():
    try:
        with db_lock:
            cur = get_cursor()
            cur.execute("SELECT id,timestamp,weapon_type,confidence,image "
                        "FROM detections ORDER BY id DESC LIMIT 50")
            rows = cur.fetchall(); cur.close()
        return {"history": [{"id":r[0],"timestamp":r[1],"weapon_type":"WEAPON DETECTED",
                              "confidence":r[3],"image":r[4]} for r in rows]}
    except Exception as e:
        return {"history": [], "error": str(e)}

@app.get("/alerts")
def list_alerts():
    try:
        files = sorted([f for f in os.listdir("alerts") if f.endswith(".jpg")],
                       reverse=True)[:20]
        return {"alerts": files}
    except:
        return {"alerts": []}

@app.get("/alerts/{img}")
def get_alert_img(img: str):
    img  = os.path.basename(img)
    path = os.path.join("alerts", img)
    if not os.path.exists(path):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache",
                                 "Access-Control-Allow-Origin": "*"})

@app.get("/logs")
def get_logs():
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()[-100:]
        return {"logs": [l.strip() for l in lines if l.strip()]}
    except Exception as e:
        return {"logs": [], "error": str(e)}

@app.post("/refresh")
def refresh():
    global heatmap, last_boxes, last_detection_time, last_save_time
    last_boxes = []; last_detection_time = 0.0; last_save_time = 0.0
    if heatmap is not None: heatmap[:] = 0
    return {"refreshed": True, "alerts": alert_count}

@app.delete("/alerts/clear")
def clear_alerts():
    global alert_count, heatmap
    try:
        for f in os.listdir("alerts"):
            try: os.remove(os.path.join("alerts", f))
            except: pass
        with db_lock:
            cur = get_cursor()
            cur.execute("DELETE FROM detections")
            conn.commit(); cur.close()
        alert_count = 0
        if heatmap is not None: heatmap[:] = 0
        return {"cleared": True}
    except Exception as e:
        return {"cleared": False, "error": str(e)}
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
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# ─────────────────────────────────────────────
# CONFIG — tuned for speed + accuracy
# ─────────────────────────────────────────────
CONF_THRESHOLD = 0.40   # 0.40 catches knives immediately
MIN_BOX_PX     = 20     # small weapons still detected
VISIBILITY_WIN = 0.8    # keep box visible 800ms after last hit
ALERT_COOLDOWN = 5.0
STREAM_FPS     = 25
DETECT_IMGSZ   = 416    # 416 = much faster than 640 on CPU, still accurate

EMAIL_USER     = os.getenv("EMAIL_USER", "").strip()
EMAIL_PASS     = os.getenv("EMAIL_PASS", "").strip()
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", "").strip()

IS_CLOUD = (
    not sys.platform.startswith("win") and
    not os.path.exists("/dev/video0") and
    not os.path.exists("/dev/video1")
)

# ─────────────────────────────────────────────
# CROSS-PLATFORM BEEP
# ─────────────────────────────────────────────
def _beep():
    try:
        if sys.platform == "win32":
            import winsound
            winsound.Beep(1800, 350)
    except Exception:
        pass

# ─────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────
frame_lock          = threading.Lock()
db_lock             = threading.Lock()
latest_frame        = None
processed_frame     = None
last_boxes          = []
last_detection_time = 0.0
last_save_time      = 0.0
alert_count         = 0
prev_fps_time       = 0.0
heatmap             = None
running             = True
start_time          = time.time()
model               = None
cap                 = None
# Separate lock just for processed_frame so stream never blocks detection
stream_lock         = threading.Lock()

# ─────────────────────────────────────────────
# SIGNAL HANDLER
# ─────────────────────────────────────────────
def _shutdown(sig, frame_arg):
    global running
    running = False
    def _exit():
        time.sleep(1.0)
        os._exit(0)
    threading.Thread(target=_exit, daemon=True).start()

try:
    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
except Exception:
    pass

# ─────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────
os.makedirs("alerts", exist_ok=True)
DB_PATH  = "detections.db"
LOG_PATH = os.path.join(os.getcwd(), "weapon_log.txt")

def _init_db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.execute("""
        CREATE TABLE IF NOT EXISTS detections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            weapon_type TEXT NOT NULL DEFAULT 'WEAPON',
            confidence  REAL NOT NULL DEFAULT 0.0,
            image       TEXT NOT NULL DEFAULT ''
        )
    """)
    cols = [r[1] for r in c.execute("PRAGMA table_info(detections)")]
    if "image" not in cols:
        c.execute("ALTER TABLE detections ADD COLUMN image TEXT NOT NULL DEFAULT ''")
    c.commit()
    return c

conn = _init_db()

def get_cursor():
    return conn.cursor()

def _clear_old_data():
    if os.path.exists("alerts"):
        for f in os.listdir("alerts"):
            try: os.remove(os.path.join("alerts", f))
            except: pass
    open(LOG_PATH, "w").close()

# ─────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────
def send_email(image_path: str = "", conf: float = 0.0):
    if not EMAIL_USER or not EMAIL_PASS or not EMAIL_RECEIVER:
        print("[EMAIL] credentials not set – skipping"); return
    if EMAIL_PASS in ("your_16char_app_password", ""):
        print("[EMAIL] placeholder password – skipping"); return
    try:
        msg = MIMEMultipart()
        msg["Subject"] = "🚨 Sentinel AI — WEAPON DETECTED"
        msg["From"]    = EMAIL_USER
        msg["To"]      = EMAIL_RECEIVER
        msg.attach(MIMEText(
            f"⚠  WEAPON DETECTED\n\n"
            f"Time       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Confidence : {conf*100:.0f}%\n\n"
            f"— Sentinel AI Surveillance System", "plain"
        ))
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
        print("[EMAIL] Alert sent ✓")
    except Exception as e:
        print(f"[EMAIL] Error: {e}")

# ─────────────────────────────────────────────
# NMS
# ─────────────────────────────────────────────
def apply_nms(boxes, iou_threshold=0.40):
    if not boxes:
        return []
    boxes = sorted(boxes, key=lambda b: b[4], reverse=True)
    kept  = []
    while boxes:
        best = boxes.pop(0)
        kept.append(best)
        rest = []
        for b in boxes:
            ix1   = max(best[0], b[0]); iy1 = max(best[1], b[1])
            ix2   = min(best[2], b[2]); iy2 = min(best[3], b[3])
            inter = max(0, ix2-ix1) * max(0, iy2-iy1)
            union = ((best[2]-best[0])*(best[3]-best[1]) +
                     (b[2]-b[0])*(b[3]-b[1]) - inter)
            if union <= 0 or inter/union < iou_threshold:
                rest.append(b)
        boxes = rest
    return kept

# ─────────────────────────────────────────────
# DRAWING
# ─────────────────────────────────────────────
def _pulse():
    return (np.sin(time.time() * 5.0) + 1.0) / 2.0

def draw_detection(frame, x1, y1, x2, y2, conf):
    p     = _pulse()
    RED   = (0, 0, 255)
    WHITE = (255, 255, 255)
    BLACK = (0, 0, 0)

    # Thick red bounding box
    cv2.rectangle(frame, (x1, y1), (x2, y2), RED, 3)

    # White corner brackets
    arm = 22
    for (cx, cy, sx, sy) in [(x1,y1,1,1),(x2,y1,-1,1),(x1,y2,1,-1),(x2,y2,-1,-1)]:
        cv2.line(frame, (cx, cy), (cx+sx*arm, cy),  WHITE, 3)
        cv2.line(frame, (cx, cy), (cx, cy+sy*arm),  WHITE, 3)

    # Pulsing red target spot on weapon centre
    cx_w    = (x1+x2)//2
    cy_w    = (y1+y2)//2
    base_r  = max(14, int(min(x2-x1, y2-y1) * 0.18))
    pulse_r = base_r + int(8 * p)
    cv2.circle(frame, (cx_w, cy_w), pulse_r, RED,   2)
    cv2.circle(frame, (cx_w, cy_w), 6,        RED,  -1)
    cv2.circle(frame, (cx_w, cy_w), 6,        WHITE, 1)

    # Label: black bg + red border + red text
    label  = f"WEAPON DETECTED  {conf*100:.0f}%"
    font   = cv2.FONT_HERSHEY_DUPLEX
    fs     = 0.70
    thick  = 2
    (tw, th), bl = cv2.getTextSize(label, font, fs, thick)
    ty = max(th + 14, y1 - 8)
    cv2.rectangle(frame, (x1, ty-th-8), (x1+tw+12, ty+bl+2), BLACK, -1)
    cv2.rectangle(frame, (x1, ty-th-8), (x1+tw+12, ty+bl+2), RED,    2)
    cv2.putText(frame, label, (x1+5, ty), font, fs, RED, thick, cv2.LINE_AA)
    return frame

def draw_alert_banner(frame):
    if _pulse() < 0.4:
        return frame
    h, w = frame.shape[:2]
    ov   = frame.copy()
    cv2.rectangle(ov, (0,0), (w,54), (0,0,150), -1)
    frame = cv2.addWeighted(ov, 0.75, frame, 0.25, 0)
    cv2.putText(frame, "  \u26a0  WEAPON DETECTED  \u26a0",
                (10, 40), cv2.FONT_HERSHEY_DUPLEX, 1.0, (0,0,255), 2, cv2.LINE_AA)
    return frame

def draw_hud(frame, fps):
    h, w = frame.shape[:2]
    cv2.putText(frame, f"FPS {fps}",
                (10, h-42), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255,220,0), 2, cv2.LINE_AA)
    cv2.putText(frame, f"ALERTS {alert_count}",
                (10, h-18), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (80,255,80), 2, cv2.LINE_AA)
    ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    cv2.putText(frame, ts, (w-235, h-12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.48, (160,160,160), 1, cv2.LINE_AA)
    col = (0,0,255) if _pulse() > 0.5 else (80,0,0)
    cv2.circle(frame, (w-18, 18), 7, col, -1)
    return frame

def draw_heatmap(frame, boxes):
    global heatmap
    h, w = frame.shape[:2]
    if heatmap is None or heatmap.shape[:2] != (h, w):
        heatmap = np.zeros((h, w), dtype=np.float32)
    for (x1, y1, x2, y2) in boxes:
        cx = (x1+x2)//2; cy = (y1+y2)//2
        r  = max(20, int(min(x2-x1, y2-y1)*0.22))
        cv2.circle(heatmap, (cx,cy), r, 0.9, -1)
    heatmap *= 0.92
    if heatmap.max() < 0.05:
        return frame
    norm = heatmap / float(heatmap.max())
    heat = cv2.applyColorMap(np.uint8(255*norm), cv2.COLORMAP_INFERNO)
    return cv2.addWeighted(frame, 0.80, heat, 0.20, 0)

# ─────────────────────────────────────────────
# CLOUD PLACEHOLDER
# ─────────────────────────────────────────────
def _cloud_frame():
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(0, 640, 40):
        cv2.line(frame, (i,0), (i,480), (15,15,15), 1)
    for i in range(0, 480, 40):
        cv2.line(frame, (0,i), (640,i), (15,15,15), 1)
    cx, cy = 320, 210
    cv2.line(frame, (cx-35,cy), (cx+35,cy), (0,0,180), 1)
    cv2.line(frame, (cx,cy-35), (cx,cy+35), (0,0,180), 1)
    cv2.circle(frame, (cx,cy), 45, (0,0,180), 1)
    cv2.circle(frame, (cx,cy), 12, (0,0,180), 1)
    cv2.putText(frame, "SENTINEL AI", (155,135),
                cv2.FONT_HERSHEY_DUPLEX, 1.4, (0,0,220), 2, cv2.LINE_AA)
    cv2.putText(frame, "Weapon Detection System", (125,178),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (100,100,100), 1, cv2.LINE_AA)
    cv2.putText(frame, "Camera: Run backend locally", (125,275),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (60,60,200), 2, cv2.LINE_AA)
    cv2.putText(frame, "All API endpoints are active", (125,315),
                cv2.FONT_HERSHEY_SIMPLEX, 0.60, (50,160,50), 1, cv2.LINE_AA)
    cv2.putText(frame, f"Alerts stored: {alert_count}", (215,360),
                cv2.FONT_HERSHEY_SIMPLEX, 0.60, (180,180,0), 1, cv2.LINE_AA)
    ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    cv2.putText(frame, ts, (195,452),
                cv2.FONT_HERSHEY_SIMPLEX, 0.50, (80,80,80), 1, cv2.LINE_AA)
    return frame

# ─────────────────────────────────────────────
# CAMERA THREAD — just reads frames, nothing else
# ─────────────────────────────────────────────
def camera_capture():
    global latest_frame
    while running:
        if cap is None:
            time.sleep(0.01); continue
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.01); continue
        with frame_lock:
            latest_frame = frame
        # No sleep — read as fast as camera allows

# ─────────────────────────────────────────────
# DETECTION THREAD — runs YOLO, updates processed_frame
# KEY FIX: uses stream_lock (separate from frame_lock)
# so stream never waits for detection to finish
# ─────────────────────────────────────────────
def detection_loop():
    global latest_frame, processed_frame
    global last_boxes, last_detection_time, last_save_time, alert_count, heatmap

    while running:
        # ── Get latest camera frame ──
        with frame_lock:
            if latest_frame is None:
                time.sleep(0.01); continue
            frame = latest_frame.copy()

        # ── YOLO inference — 416px is ~2-3x faster than 640 on CPU ──
        raw_boxes = []
        try:
            results = model(
                frame,
                conf=CONF_THRESHOLD,
                iou=0.40,
                imgsz=DETECT_IMGSZ,
                verbose=False,
                half=False,   # no half-precision on CPU
                augment=False # no augment = faster
            )[0]
            fh, fw = frame.shape[:2]
            for box in results.boxes:
                conf = float(box.conf[0])
                if conf < CONF_THRESHOLD: continue
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                wb = x2-x1; hb = y2-y1
                # Size filter — ignore tiny noise
                if wb < MIN_BOX_PX or hb < MIN_BOX_PX: continue
                # Skip full-frame false positives
                if wb > fw*0.88 or hb > fh*0.88: continue
                # Aspect ratio — weapons aren't extreme slivers or squares
                aspect = wb/hb if hb > 0 else 0
                if aspect < 0.04 or aspect > 18.0: continue
                raw_boxes.append((x1, y1, x2, y2, conf))
        except Exception as e:
            print(f"[YOLO] {e}")

        boxes = apply_nms(raw_boxes)
        now   = time.time()

        if boxes:
            last_boxes          = boxes
            last_detection_time = now

        detection_visible = (
            bool(last_boxes) and
            (now - last_detection_time) < VISIBILITY_WIN
        )

        # ── Draw on a copy so stream always has a clean frame ──
        display = frame.copy()

        if detection_visible:
            for (x1, y1, x2, y2, conf) in last_boxes:
                display = draw_detection(display, x1, y1, x2, y2, conf)
            display = draw_alert_banner(display)
            display = draw_heatmap(display, [(b[0],b[1],b[2],b[3]) for b in last_boxes])
        else:
            last_boxes = []
            if heatmap is not None:
                heatmap *= 0.92

        # ── Publish to stream immediately ──
        with stream_lock:
            processed_frame = display

        # ── Save alert (throttled) ──
        if detection_visible and boxes and (now - last_save_time) > ALERT_COOLDOWN:
            last_save_time = now
            alert_count   += 1
            save_conf      = max(b[4] for b in boxes)
            ts_str         = datetime.now().strftime("%Y%m%d_%H%M%S")
            img_path       = f"alerts/weapon_{ts_str}.jpg"
            cv2.imwrite(img_path, display)
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as lf:
                    lf.write(f"[{ts_str}] ALERT #{alert_count} | WEAPON DETECTED | CONF={save_conf:.2f}\n")
            except Exception: pass
            try:
                with db_lock:
                    cur = get_cursor()
                    cur.execute(
                        "INSERT INTO detections (timestamp,weapon_type,confidence,image) VALUES (?,?,?,?)",
                        (ts_str, "WEAPON", round(save_conf,4), img_path)
                    )
                    conn.commit(); cur.close()
            except Exception as e:
                print(f"[DB] {e}")
            threading.Thread(target=_beep, daemon=True).start()
            threading.Thread(target=send_email, args=(img_path, save_conf), daemon=True).start()
            print(f"[ALERT #{alert_count}] WEAPON DETECTED | CONF={save_conf:.2f}")

        # No sleep — run as fast as YOLO allows (~15-25fps on CPU)

# ─────────────────────────────────────────────
# MJPEG STREAM — reads from stream_lock, never blocks
# Sends frames even while YOLO is running
# ─────────────────────────────────────────────
def frame_generator():
    global prev_fps_time
    interval  = 1.0 / STREAM_FPS
    last_sent = 0.0
    # Blank startup frame shown before camera initializes
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(blank, "Initializing camera...", (160, 240),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (80,80,80), 2, cv2.LINE_AA)

    while running:
        now  = time.time()
        wait = interval - (now - last_sent)
        if wait > 0:
            time.sleep(wait)
        last_sent = time.time()

        if IS_CLOUD:
            frame = _cloud_frame()
        else:
            with stream_lock:
                frame = processed_frame.copy() if processed_frame is not None else blank.copy()

        curr = time.time()
        fps  = int(1/(curr-prev_fps_time)) if prev_fps_time and (curr-prev_fps_time) > 0 else 0
        prev_fps_time = curr

        frame = draw_hud(frame, fps)

        ret, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ret: continue
        yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
               + buf.tobytes() + b"\r\n")

# ─────────────────────────────────────────────
# LIFESPAN
# ─────────────────────────────────────────────
@asynccontextmanager
async def lifespan(application: FastAPI):
    global model, cap

    print(f"[STARTUP] Mode: {'CLOUD' if IS_CLOUD else 'LOCAL'}")
    _clear_old_data()

    print("[STARTUP] Loading YOLO model …")
    try:
        import torch
        from ultralytics import YOLO
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model  = YOLO("best.pt").to(device)
        # Warm up with 3 dummy runs so first real frame is instant
        dummy = np.zeros((480, 640, 3), dtype=np.uint8)
        for _ in range(3):
            model(dummy, imgsz=DETECT_IMGSZ, verbose=False)
        print(f"[STARTUP] Model ready on {device.upper()} ✓")
    except Exception as e:
        print(f"[STARTUP] Model error: {e}")
        model = None

    if not IS_CLOUD and model is not None:
        try:
            backend = cv2.CAP_DSHOW if sys.platform == "win32" else 0
            cap = cv2.VideoCapture(0, backend)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS,          30)
            cap.set(cv2.CAP_PROP_BUFFERSIZE,   1)  # 1 = always latest frame, no queue
            if cap.isOpened():
                threading.Thread(target=camera_capture, daemon=True).start()
                threading.Thread(target=detection_loop,  daemon=True).start()
                print("[STARTUP] Camera + detection threads started ✓")
            else:
                print("[STARTUP] Camera not found")
        except Exception as e:
            print(f"[STARTUP] Camera error: {e}")

    print("[STARTUP] Sentinel AI is live ✓")
    yield

    global running
    running = False
    time.sleep(0.3)
    try:
        if cap and cap.isOpened(): cap.release()
    except Exception: pass
    try: conn.close()
    except Exception: pass
    print("[SHUTDOWN] Clean ✓")

# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────
app = FastAPI(title="Sentinel AI", version="5.2", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ── ngrok warning bypass ──
class NgrokHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["ngrok-skip-browser-warning"] = "true"
        return response

app.add_middleware(NgrokHeaderMiddleware)

if os.path.exists("alerts"):
    app.mount("/alert_files", StaticFiles(directory="alerts"), name="alert_files")

# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────
@app.get("/")
def root():
    return {"service":"Sentinel AI","version":"5.2",
            "mode":"cloud" if IS_CLOUD else "local",
            "status":"running","uptime":int(time.time()-start_time)}

@app.get("/health")
def health():
    return {"status":"ok","mode":"cloud" if IS_CLOUD else "local",
            "camera":(cap is not None and cap.isOpened()) if not IS_CLOUD else False,
            "model":model is not None,"alerts":alert_count,
            "uptime":int(time.time()-start_time)}

@app.get("/video")
@app.get("/video_feed")
def video_feed():
    return StreamingResponse(
        frame_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store",
            "Pragma": "no-cache",
            "Access-Control-Allow-Origin": "*",
            "ngrok-skip-browser-warning": "true"
        }
    )

@app.get("/stats")
def get_stats():
    try:
        with db_lock:
            cur = get_cursor()
            cur.execute("SELECT COUNT(*) FROM detections")
            total = (cur.fetchone() or (0,))[0]
            cur.execute("SELECT AVG(confidence) FROM detections")
            avg = (cur.fetchone() or (0,))[0] or 0.0
            cur.execute("SELECT COUNT(*) FROM detections WHERE timestamp >= datetime('now','-1 hour')")
            last_hour = (cur.fetchone() or (0,))[0]
            cur.execute("SELECT timestamp FROM detections ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            last_ts = row[0] if row else None
            cur.close()
        return {"alerts":total,"last_hour":last_hour,"avg_conf":round(avg*100,1),
                "last_detection":last_ts,"uptime":int(time.time()-start_time),
                "status":"ACTIVE","mode":"cloud" if IS_CLOUD else "local"}
    except Exception as e:
        return {"alerts":alert_count,"avg_conf":0,"uptime":0,
                "status":"ACTIVE","error":str(e)}

@app.get("/history")
def get_history():
    try:
        with db_lock:
            cur = get_cursor()
            cur.execute("SELECT id,timestamp,weapon_type,confidence,image "
                        "FROM detections ORDER BY id DESC LIMIT 50")
            rows = cur.fetchall(); cur.close()
        return {"history":[{"id":r[0],"timestamp":r[1],"weapon_type":"WEAPON DETECTED",
                             "confidence":r[3],"image":r[4]} for r in rows]}
    except Exception as e:
        return {"history":[],"error":str(e)}

@app.get("/alerts")
def list_alerts():
    try:
        files = sorted([f for f in os.listdir("alerts") if f.endswith(".jpg")],
                       reverse=True)[:30]
        return {"alerts":files}
    except Exception:
        return {"alerts":[]}

@app.get("/alerts/{img}")
def get_alert_img(img: str):
    img  = os.path.basename(img)
    path = os.path.join("alerts", img)
    if not os.path.exists(path):
        return JSONResponse({"error":"not found"}, status_code=404)
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control":"no-cache",
                                 "Access-Control-Allow-Origin":"*"})

@app.get("/logs")
def get_logs():
    try:
        with open(LOG_PATH,"r",encoding="utf-8") as f:
            lines = f.readlines()[-100:]
        return {"logs":[l.strip() for l in lines if l.strip()]}
    except Exception as e:
        return {"logs":[],"error":str(e)}

@app.post("/refresh")
def refresh():
    global heatmap, last_boxes, last_detection_time, last_save_time
    last_boxes=[]; last_detection_time=0.0; last_save_time=0.0
    if heatmap is not None: heatmap[:]=0
    return {"refreshed":True,"alerts":alert_count}

@app.delete("/alerts/clear")
def clear_alerts():
    global alert_count, heatmap
    try:
        for f in os.listdir("alerts"):
            try: os.remove(os.path.join("alerts",f))
            except: pass
        with db_lock:
            cur = get_cursor()
            cur.execute("DELETE FROM detections")
            conn.commit(); cur.close()
        alert_count = 0
        if heatmap is not None: heatmap[:]=0
        open(LOG_PATH,"w").close()
        return {"cleared":True}
    except Exception as e:
        return {"cleared":False,"error":str(e)}

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=False
    )
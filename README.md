# 🛡 Sentinel AI — Weapon Detection Surveillance System

Real-time AI-powered weapon detection using YOLOv8, FastAPI backend, and React dashboard.

---

## 🚀 Quick Start

### 1. Clone & Setup

```powershell
git clone https://github.com/YOUR_USERNAME/SentinelAI-Weapon-Detection.git
cd SentinelAI-Weapon-Detection
```

### 2. Create virtual environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. Install Python dependencies

```powershell
pip install -r requirements.txt
```

### 4. Configure email alerts

Create a `.env` file in the project root:

```
EMAIL_USER=your_gmail@gmail.com
EMAIL_PASS=xxxx xxxx xxxx xxxx
EMAIL_RECEIVER=receiver@gmail.com
```

> Get a Gmail App Password at: https://myaccount.google.com/apppasswords  
> Use the 16-character App Password, NOT your Gmail login password.

### 5. Start the backend

```powershell
python -m uvicorn server:app --reload
```

Backend runs at: http://localhost:8000

### 6. Start the frontend

```powershell
cd sentinel-dashboard
npm install
npm run dev
```

Dashboard at: http://localhost:5173

---

## 📱 Mobile Access (Same WiFi)

Find your PC's local IP:
```powershell
ipconfig
# Look for IPv4 Address e.g. 192.168.1.5
```

Then on your phone: `http://192.168.1.5:5173`

You also need the backend accessible on the network. Set `VITE_API_URL` in `sentinel-dashboard/.env.local`:
```
VITE_API_URL=http://192.168.1.5:8000
```

And run the backend with:
```powershell
python -m uvicorn server:app --host 0.0.0.0 --reload
```

---

## 🗂 Project Structure

```
SentinelAI-Weapon-Detection/
├── server.py              # FastAPI backend + YOLO detection
├── detect.py              # Standalone camera detection script
├── best.pt                # YOLOv8 trained model
├── requirements.txt
├── .env                   # ← create this (never commit)
├── .env.example
├── .gitignore
├── README.md
├── detections.db          # SQLite database (auto-created)
├── alerts/                # Alert screenshots (auto-created)
└── sentinel-dashboard/    # React frontend
    ├── src/
    │   ├── App.jsx
    │   ├── App.css
    │   └── main.jsx
    ├── index.html
    ├── package.json
    └── vite.config.js
```

---

## 🔌 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/video` | GET | MJPEG live stream |
| `/stats` | GET | Total alerts, uptime, status |
| `/history` | GET | Last 50 detections |
| `/alerts` | GET | List of alert images |
| `/alerts/{img}` | GET | Serve alert screenshot |
| `/logs` | GET | Last 100 log lines |
| `/alerts/clear` | DELETE | Clear all alerts + DB |

---

## 🛠 Tech Stack

- **AI Model**: YOLOv8 (Ultralytics)
- **Backend**: FastAPI + Uvicorn
- **Camera**: OpenCV
- **Database**: SQLite
- **Email**: Gmail SMTP
- **Frontend**: React 18 + Vite
- **Styling**: Pure CSS (no UI library)

---

## 🔒 Security Notes

- Never commit `.env` — it's in `.gitignore`
- Use Gmail App Passwords, not account passwords
- The `alerts/` folder is also gitignored

---

## 📧 Email Setup (Step by Step)

1. Go to https://myaccount.google.com/security
2. Enable **2-Step Verification**
3. Go to https://myaccount.google.com/apppasswords
4. Select "Mail" and "Windows Computer"
5. Copy the 16-character password into `.env` as `EMAIL_PASS`

---

*Built for placement project — Sentinel AI Surveillance System*

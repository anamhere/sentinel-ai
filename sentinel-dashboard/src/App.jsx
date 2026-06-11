import { useState, useEffect, useRef, useCallback } from "react";
import "./App.css";

// ── URL constants ────────────────────────────────────────────────────────────
// On Vercel: VITE_API_URL is set → use /api proxy (vercel.json rewrites to ngrok)
// Locally:   VITE_API_URL is not set → use Vite proxy ("") and direct stream
const API_BASE =
  (import.meta.env.VITE_API_URL || "http://localhost:8000")
    .replace(/\/$/, "");

const STREAM_BASE =
  (import.meta.env.VITE_STREAM_URL || "http://localhost:8000")
    .replace(/\/$/, "");

const IS_DEPLOYED =
  window.location.hostname !== "localhost";

console.log("API_BASE =", API_BASE);
console.log("STREAM_BASE =", STREAM_BASE);

// ngrok header needed for direct stream requests only

export default function App() {
  const [stats, setStats]         = useState({ alerts: 0, avg_conf: 0, uptime: 0, status: "CONNECTING" });
  const [history, setHistory]     = useState([]);
  const [logs, setLogs]           = useState([]);
  const [alertImgs, setAlertImgs] = useState([]);
  const [tab, setTab]             = useState("live");
  const [lightbox, setLightbox]   = useState(null);
  const [toast, setToast]         = useState(null);
  const [toastType, setToastType] = useState("ok");
  const [streamError, setStreamError] = useState(false);
  const [refreshing, setRefreshing]   = useState(false);
  const [streamKey, setStreamKey]     = useState(0);

  const prevAlerts = useRef(0);
  const logsRef    = useRef(null);
  const toastTimer = useRef(null);

  const showToast = useCallback((msg, type = "ok", ms = 3000) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setToast(msg); setToastType(type);
    toastTimer.current = setTimeout(() => setToast(null), ms);
  }, []);

  const playBeep = useCallback(() => {
    try {
      const ctx  = new AudioContext();
      const osc  = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.connect(gain); gain.connect(ctx.destination);
      osc.frequency.value = 880; osc.type = "square";
      gain.gain.setValueAtTime(0.3, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
      osc.start(); osc.stop(ctx.currentTime + 0.4);
    } catch (_) {}
  }, []);

  const fetchAll = useCallback(async () => {
    try {
      const [s, h, l, a] = await Promise.all([
        fetch(`${API_BASE}/stats`).then(r => r.json()),
        fetch(`${API_BASE}/history`).then(r => r.json()),
        fetch(`${API_BASE}/logs`).then(r => r.json()),
        fetch(`${API_BASE}/alerts`).then(r => r.json())
      ]);
      setStats(s);
      setHistory(h.history || []);
      setLogs((l.logs || []).reverse());
      // Alert images: use STREAM_BASE (direct ngrok) so <img> can load them
      setAlertImgs(
        (a.alerts || []).map(
        f => `${STREAM_BASE}/alert_files/${f}?t=${Date.now()}`
        )
      );
      if (s.alerts > prevAlerts.current && prevAlerts.current !== 0) {
        showToast("⚠ WEAPON DETECTED — Alert saved!", "warn", 4000);
        playBeep();
      }
      prevAlerts.current = s.alerts;
    } catch (err) {
  console.error("API ERROR:", err);
}
  }, [playBeep, showToast]);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 2000);
    return () => {
      clearInterval(id);
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, [fetchAll]);

  useEffect(() => {
    if (logsRef.current) logsRef.current.scrollTop = 0;
  }, [logs]);

  const reloadStream = useCallback(() => {
    setStreamError(false);
    setStreamKey(k => k + 1);
  }, []);

  // Stream goes DIRECT to ngrok (bypass vercel proxy — it can't handle MJPEG)
  const streamSrc = `${STREAM_BASE}/video`;

  const handleRefresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await fetch(`${API_BASE}/refresh`, {
        method: "POST"
    });
      reloadStream();
      await fetchAll();
      showToast("✓ System refreshed", "ok");
    } catch {
      showToast("✗ Refresh failed — is backend running?", "err");
    } finally {
      setRefreshing(false);
    }
  };

  const clearAlerts = async () => {
    if (!confirm("Clear all alerts and detections?")) return;
    try {
      await fetch(`${API_BASE}/alerts/clear`, {
        method: "DELETE"
      });
      await fetchAll();
      showToast("🗑 All alerts cleared", "ok");
    } catch { showToast("✗ Clear failed", "err"); }
  };

  const fmtUptime = s => {
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(sec).padStart(2,"0")}`;
  };

  const imgFilename = p =>
    p ? p.replace(/^.*[/\\]alerts[/\\]/,"").replace(/^alerts[/\\]/,"").split("?")[0] : null;

  const isLive = stats.status === "ACTIVE";

  return (
    <div className="app">

      {toast && <div className={`toast toast-${toastType}`}>{toast}</div>}

      {lightbox && (
        <div className="lightbox" onClick={() => setLightbox(null)}>
          <img src={lightbox} alt="Evidence" />
          <button className="lb-close" onClick={e => { e.stopPropagation(); setLightbox(null); }}>✕</button>
        </div>
      )}

      <header className="header">
        <div className="header-left">
          <div className={`status-dot${isLive ? " live" : ""}`} />
          <span className="brand">SENTINEL<span className="brand-ai">AI</span></span>
          <span className="brand-sub">Weapon Detection System</span>
        </div>
        <div className="header-right">
          <div className="stat-pill">
            <span className="pill-label">ALERTS</span>
            <span className="pill-value alert-num">{stats.alerts}</span>
          </div>
          <div className="stat-pill hide-xs">
            <span className="pill-label">UPTIME</span>
            <span className="pill-value">{fmtUptime(stats.uptime || 0)}</span>
          </div>
          <div className={`status-badge ${isLive ? "badge-live" : "badge-off"}`}>
            {isLive ? "● LIVE" : "○ OFFLINE"}
          </div>
          <button
            className={`btn-refresh${refreshing ? " refreshing" : ""}`}
            onClick={handleRefresh}
            disabled={refreshing}
          >
            <span className={`refresh-icon${refreshing ? " spin" : ""}`}>↺</span>
            <span className="refresh-label">{refreshing ? "…" : "Refresh"}</span>
          </button>
        </div>
      </header>

      <nav className="nav">
        {[
          ["live",     "📡 Live"],
          ["history",  "📋 Detections"],
          ["evidence", "🖼 Evidence"],
          ["logs",     "📄 Logs"],
        ].map(([k, label]) => (
          <button
            key={k}
            className={`nav-btn${tab === k ? " active" : ""}`}
            onClick={() => setTab(k)}
          >
            {label}
          </button>
        ))}
      </nav>

      <main className="main">

        {tab === "live" && (
          <div className="live-layout">
            <div className="feed-wrap">
              <div className="feed-header">
                <span>LIVE CAMERA FEED</span>
                <span className={`rec-dot${isLive ? " rec-on" : ""}`}>● REC</span>
              </div>

        {streamError ? (
          <div className="feed-error">
            <div className="fe-icon">📷</div>

            <p>Camera feed unavailable</p>

            <p className="fe-sub">
              Stream URL:
              <br />
              <code>{streamSrc}</code>
            </p>

            <button onClick={reloadStream}>↺ Retry</button>
        </div>
      ) : (
        <img
          key={streamKey}
          className="feed-img"
          src={streamSrc}
          alt="Live stream"
          onError={(e) => {
            console.error("STREAM FAILED");
            console.error("URL:", streamSrc);
            console.error(e);
            setStreamError(true);
        }}
          onLoad={() => {
            console.log("STREAM CONNECTED");
            setStreamError(false);
          }}
        />
      )}

              <div className="feed-footer">
                <span>⚙ YOLOv8 WEAPON DETECTION</span>
                <span>🎯 CONF THRESHOLD: 45%</span>
              </div>
            </div>

            <div className="side-panel">
              {[
                { icon: "🚨", val: stats.alerts,               label: "Total Alerts" },
                { icon: "🎯", val: `${stats.avg_conf || 0}%`,  label: "Avg Confidence" },
                { icon: "⏱",  val: fmtUptime(stats.uptime||0), label: "Uptime" },
                { icon: isLive ? "🟢" : "🔴",
                  val: isLive ? "ACTIVE" : "OFFLINE",
                  label: "System Status", small: true },
              ].map(({ icon, val, label, small }) => (
                <div className="stat-card" key={label}>
                  <div className="sc-icon">{icon}</div>
                  <div className="sc-info">
                    <div className="sc-val" style={small ? { fontSize: "1rem" } : {}}>{val}</div>
                    <div className="sc-label">{label}</div>
                  </div>
                </div>
              ))}

              <div className="recent-list">
                <div className="rl-header">Recent Detections</div>
                {history.slice(0, 5).map(h => (
                  <div className="rl-item" key={h.id}>
                    <span className="rl-label">⚠ WEAPON</span>
                    <span className="rl-conf">{(h.confidence * 100).toFixed(0)}%</span>
                    <span className="rl-ts">{h.timestamp?.slice(9, 17)}</span>
                  </div>
                ))}
                {history.length === 0 && <div className="rl-empty">No detections yet</div>}
              </div>
            </div>
          </div>
        )}

        {tab === "history" && (
          <div className="table-wrap">
            <div className="table-toolbar">
              <span className="table-title">Detection History ({history.length})</span>
              <button className="btn-danger" onClick={clearAlerts}>🗑 Clear All</button>
            </div>
            <div className="table-scroll">
              <table className="det-table">
                <thead>
                  <tr>
                    <th>#</th><th>Timestamp</th><th>Detection</th>
                    <th>Confidence</th><th>Evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {history.map((row, i) => (
                    <tr key={row.id}>
                      <td className="td-id">{history.length - i}</td>
                      <td className="td-ts">{row.timestamp}</td>
                      <td><span className="badge-weapon">⚠ WEAPON DETECTED</span></td>
                      <td>
                        <span className={`badge-conf ${
                          row.confidence > 0.8 ? "conf-high" :
                          row.confidence > 0.6 ? "conf-med" : "conf-low"
                        }`}>
                          {(row.confidence * 100).toFixed(1)}%
                        </span>
                      </td>
                      <td>
                        {row.image
                          ? <button className="btn-view" onClick={() =>
                              setLightbox(
                                `${STREAM_BASE}/alert_files/${imgFilename(row.image)}?t=${Date.now()}`
                              )
                            }>View</button>
                          : "—"}
                      </td>
                    </tr>
                  ))}
                  {history.length === 0 && (
                    <tr><td colSpan={5} className="td-empty">No detections recorded yet</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {tab === "evidence" && (
          <div className="evidence-wrap">
            <div className="table-toolbar">
              <span className="table-title">Evidence Gallery ({alertImgs.length})</span>
              <button className="btn-danger" onClick={clearAlerts}>🗑 Clear All</button>
            </div>
            <div className="gallery">
              {alertImgs.map((src, i) => (
                <div className="gallery-item" key={i} onClick={() => setLightbox(src)}>
                  <img
                    src={src}
                    alt={`Alert ${i + 1}`}
                    loading="lazy"
                    onError={e => { e.target.style.opacity = "0.2"; }}
                  />
                  <div className="gallery-overlay">⚠ WEAPON<br />Click to view</div>
                </div>
              ))}
              {alertImgs.length === 0 && (
                <div className="gallery-empty">
                  <p>🖼 No evidence captured yet</p>
                  <p>Screenshots save automatically on weapon detection</p>
                </div>
              )}
            </div>
          </div>
        )}

        {tab === "logs" && (
          <div className="logs-wrap">
            <div className="table-toolbar">
              <span className="table-title">System Logs</span>
              <button className="btn-secondary" onClick={fetchAll}>↻ Refresh</button>
            </div>
            <div className="logs-scroll" ref={logsRef}>
              {logs.map((line, i) => (
                <div
                  key={i}
                  className={`log-line${
                    line.includes("ALERT") ? " log-alert" :
                    line.includes("ERROR") ? " log-error" : ""
                  }`}
                >
                  {line}
                </div>
              ))}
              {logs.length === 0 && <div className="log-empty">No logs yet</div>}
            </div>
          </div>
        )}
      </main>

      <footer className="footer">
        Sentinel AI · YOLOv8 Weapon Detection · {IS_DEPLOYED ? "Vercel+ngrok" : "localhost:8000"}
      </footer>
    </div>
  );
}

// /api/proxy.js  — Vercel serverless function
// Forwards every /api/<path> request to the local backend via ngrok.
// Set NGROK_URL in Vercel Environment Variables, e.g.:
//   NGROK_URL = https://xxxx.ngrok-free.app

export const config = { api: { bodyParser: false } };

export default async function handler(req, res) {
  const ngrok = (process.env.NGROK_URL || "").replace(/\/$/, "");

  if (!ngrok) {
    res.status(503).json({
      error: "NGROK_URL environment variable is not set in Vercel.",
      hint:  "Go to Vercel → Project → Settings → Environment Variables and add NGROK_URL."
    });
    return;
  }

  // Strip the leading /api prefix to get the real backend path
  // e.g. /api/stats → /stats,  /api/alert_files/foo.jpg → /alert_files/foo.jpg
  const backendPath = req.url.replace(/^\/api/, "") || "/";
  const target      = `${ngrok}${backendPath}`;

  try {
    // Read body for POST/DELETE/PUT
    const bodyChunks = [];
    await new Promise((resolve, reject) => {
      req.on("data", chunk => bodyChunks.push(chunk));
      req.on("end",  resolve);
      req.on("error", reject);
    });
    const body = bodyChunks.length ? Buffer.concat(bodyChunks) : undefined;

    const fetchOptions = {
      method:  req.method,
      headers: {
        // Pass through content-type but always add ngrok bypass
        ...(req.headers["content-type"]
          ? { "content-type": req.headers["content-type"] }
          : {}),
        "ngrok-skip-browser-warning": "true",
        "User-Agent": "SentinelAI-Vercel-Proxy/1.0",
      },
      ...(body && body.length > 0 ? { body } : {}),
    };

    const upstream = await fetch(target, fetchOptions);

    // Forward status + content-type
    res.status(upstream.status);
    const ct = upstream.headers.get("content-type");
    if (ct) res.setHeader("content-type", ct);
    res.setHeader("Access-Control-Allow-Origin", "*");

    // Stream the response body back
    const buf = await upstream.arrayBuffer();
    res.end(Buffer.from(buf));
  } catch (err) {
    console.error("[proxy] error:", err.message);
    res.status(502).json({
      error:   "Proxy error — backend unreachable",
      detail:  err.message,
      target,
    });
  }
}

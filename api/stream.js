// /api/stream.js  — Vercel serverless function
// Proxies the MJPEG stream from the local backend via ngrok.
// Vercel serverless functions support streaming responses via the Edge runtime,
// but standard Node functions buffer everything. We use a chunk-by-chunk pipe
// with the Web Streams API (available in Vercel's Node 18+ runtime).
//
// Set NGROK_URL in Vercel Environment Variables.

export const config = {
  api: {
    // Required so Next/Vercel doesn't try to parse the response
    responseLimit: false,
    bodyParser:    false,
  },
  // Use edge runtime for true streaming support
  runtime: "edge",
};

export default async function handler(req) {
  const ngrok = (process.env.NGROK_URL || "").replace(/\/$/, "");

  if (!ngrok) {
    return new Response(
      JSON.stringify({ error: "NGROK_URL not set in Vercel environment variables." }),
      { status: 503, headers: { "content-type": "application/json" } }
    );
  }

  const target = `${ngrok}/video`;

  try {
    const upstream = await fetch(target, {
      headers: {
        "ngrok-skip-browser-warning": "true",
        "User-Agent": "SentinelAI-Vercel-Stream/1.0",
      },
    });

    if (!upstream.ok) {
      return new Response(
        JSON.stringify({ error: `Upstream returned ${upstream.status}` }),
        { status: upstream.status, headers: { "content-type": "application/json" } }
      );
    }

    const ct = upstream.headers.get("content-type") || "multipart/x-mixed-replace; boundary=frame";

    // Pipe the stream directly — Edge runtime supports this natively
    return new Response(upstream.body, {
      status: 200,
      headers: {
        "content-type": ct,
        "Cache-Control": "no-cache, no-store",
        "Access-Control-Allow-Origin": "*",
        "X-Accel-Buffering": "no", // Disable nginx buffering if present
      },
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: "Stream proxy error", detail: err.message }),
      { status: 502, headers: { "content-type": "application/json" } }
    );
  }
}
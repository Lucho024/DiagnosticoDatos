"""
Consume palabras desde Redis y sirve un dashboard en tiempo real via SSE.
"""

import os
import json
import time
import threading
import logging
import queue
import redis
from flask import Flask, Response, render_template_string, request, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [VIZ] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REDIS_HOST    = os.getenv("REDIS_HOST", "redis")
REDIS_PORT    = int(os.getenv("REDIS_PORT", 6379))
REDIS_CHANNEL = "words"
DEFAULT_TOP_N = int(os.getenv("DEFAULT_TOP_N", 20))

app = Flask(__name__)


# ── Conexión Redis ────────────────────────────────────────────────────────────
def get_redis() -> redis.Redis:
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


# ── Broadcaster SSE ───────────────────────────────────────────────────────────
class EventBroadcaster:
    """Escucha Redis pub/sub y retransmite a todos los clientes SSE conectados."""

    def __init__(self):
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while True:
            try:
                r = get_redis()
                ps = r.pubsub()
                ps.subscribe(REDIS_CHANNEL)
                log.info("Suscrito al canal Redis '%s'.", REDIS_CHANNEL)
                for message in ps.listen():
                    if message["type"] == "message":
                        self._broadcast(message["data"])
            except Exception as exc:
                log.warning("Error en listener Redis: %s. Reconectando …", exc)
                time.sleep(3)

    def _broadcast(self, data: str):
        with self._lock:
            dead = []
            for q in self._clients:
                try:
                    q.put_nowait(data)
                except Exception:
                    dead.append(q)
            for q in dead:
                self._clients.remove(q)

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=100)
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)


broadcaster = EventBroadcaster()


# ── Helpers ───────────────────────────────────────────────────────────────────
def snapshot_event(top_n: int) -> str:
    try:
        r = get_redis()
        results = r.zrevrange("word_counts", 0, top_n - 1, withscores=True)
        total_unique = int(r.zcard("word_counts") or 0)
        data = [{"word": w, "count": int(s)} for w, s in results]
        payload = json.dumps({"top": data, "total_unique": total_unique})
        return f"data: {payload}\n\n"
    except Exception:
        return ": error\n\n"


# ── Rutas ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    top_n = request.args.get("top", DEFAULT_TOP_N, type=int)
    return render_template_string(HTML_TEMPLATE, top_n=top_n)


@app.route("/api/top")
def api_top():
    top_n = request.args.get("n", DEFAULT_TOP_N, type=int)
    try:
        r = get_redis()
        results = r.zrevrange("word_counts", 0, top_n - 1, withscores=True)
        return jsonify([{"word": w, "count": int(s)} for w, s in results])
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/stream")
def stream():
    """Endpoint SSE — envía top-N actualizado cada vez que llegan nuevas palabras."""
    top_n = request.args.get("n", DEFAULT_TOP_N, type=int)

    def generate():
        q = broadcaster.subscribe()
        try:
            yield snapshot_event(top_n)          # snapshot inicial
            while True:
                try:
                    q.get(timeout=30)             # espera nuevos datos
                    yield snapshot_event(top_n)
                except Exception:
                    yield ": heartbeat\n\n"        # mantener conexión viva
        finally:
            broadcaster.unsubscribe(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Dashboard HTML ────────────────────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitHub Word Miner — Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
  :root {
    --bg:      #0d1117;
    --surface: #161b22;
    --border:  #30363d;
    --accent:  #58a6ff;
    --green:   #3fb950;
    --text:    #e6edf3;
    --muted:   #8b949e;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'JetBrains Mono', 'Fira Code', monospace; min-height: 100vh; }

  header {
    padding: 1rem 2rem;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    display: flex; align-items: center; gap: 1rem;
  }
  header h1 { font-size: 1.1rem; font-weight: 600; color: var(--accent); letter-spacing: 0.05em; }
  .dot { width: 9px; height: 9px; border-radius: 50%; background: #444; transition: background 0.4s; }
  .dot.live { background: var(--green); box-shadow: 0 0 7px var(--green); }

  .controls {
    padding: 0.75rem 2rem;
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 1.5rem; flex-wrap: wrap;
  }
  .controls label { font-size: 0.78rem; color: var(--muted); }
  .controls input[type=range] { accent-color: var(--accent); cursor: pointer; }
  #topn-val { font-size: 0.85rem; color: var(--accent); min-width: 2ch; }
  .stat { font-size: 0.78rem; color: var(--muted); }
  .stat span { color: var(--text); font-weight: 600; }

  main {
    display: grid;
    grid-template-columns: 1fr 300px;
    gap: 1.25rem;
    padding: 1.25rem 2rem;
    max-width: 1300px;
    margin: 0 auto;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 1.1rem;
  }
  .card h2 { font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 0.85rem; }

  canvas { width: 100% !important; }

  #word-list { list-style: none; overflow-y: auto; max-height: 480px; }
  #word-list li {
    display: flex; align-items: center; gap: 0.6rem;
    padding: 0.4rem 0; border-bottom: 1px solid var(--border);
    font-size: 0.78rem; animation: fadeIn 0.25s ease;
  }
  @keyframes fadeIn { from { opacity:0; transform:translateX(-5px); } to { opacity:1; } }
  .rank  { color: var(--muted); font-size: 0.68rem; min-width: 2ch; text-align: right; }
  .word  { color: var(--accent); flex: 1; }
  .bar-bg { flex: 2; background: var(--border); border-radius: 3px; height: 5px; overflow: hidden; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--green)); border-radius: 3px; transition: width 0.5s ease; }
  .count { color: var(--muted); font-size: 0.7rem; min-width: 5ch; text-align: right; }

  footer { text-align: center; padding: 0.75rem; font-size: 0.72rem; color: var(--muted); border-top: 1px solid var(--border); margin-top: 1rem; }

  @media (max-width: 720px) { main { grid-template-columns: 1fr; } }
</style>
</head>
<body>

<header>
  <div class="dot" id="dot"></div>
  <h1>⛏ GitHub Word Miner</h1>
  <small style="color:var(--muted)">palabras más usadas en nombres de funciones y métodos</small>
</header>

<div class="controls">
  <label>Top-N &nbsp;
    <input type="range" id="slider" min="5" max="50" value="{{ top_n }}" step="5">
  </label>
  <span id="topn-val">{{ top_n }}</span>
  <div class="stat">Palabras únicas: <span id="stat-unique">—</span></div>
  <div class="stat">Última actualización: <span id="stat-time">—</span></div>
</div>

<main>
  <div class="card">
    <h2>Frecuencia de palabras</h2>
    <canvas id="chart"></canvas>
  </div>
  <div class="card">
    <h2>Ranking</h2>
    <ul id="word-list"></ul>
  </div>
</main>

<footer>Miner → Redis (pub/sub + sorted set) → Visualizer → SSE → Browser</footer>

<script>
const slider = document.getElementById('slider');
const topnVal = document.getElementById('topn-val');
const dot = document.getElementById('dot');
let topN = parseInt(slider.value);
let es = null;

const ctx = document.getElementById('chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'bar',
  data: { labels: [], datasets: [{ label: 'ocurrencias', data: [],
    backgroundColor: 'rgba(88,166,255,0.2)',
    borderColor: '#58a6ff', borderWidth: 1.5, borderRadius: 4 }] },
  options: {
    indexAxis: 'y',
    animation: { duration: 350 },
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: '#8b949e', font: { family: 'monospace', size: 10 } }, grid: { color: '#30363d' } },
      y: { ticks: { color: '#e6edf3', font: { family: 'monospace', size: 11 } }, grid: { color: '#30363d' } },
    },
  },
});

function render(data) {
  const items = data.top || [];
  const max = items[0]?.count || 1;

  chart.data.labels = items.map(d => d.word);
  chart.data.datasets[0].data = items.map(d => d.count);
  chart.update();

  document.getElementById('word-list').innerHTML = items.map((d, i) => `
    <li>
      <span class="rank">${i + 1}</span>
      <span class="word">${d.word}</span>
      <span class="bar-bg"><span class="bar-fill" style="width:${(d.count/max*100).toFixed(1)}%"></span></span>
      <span class="count">${d.count.toLocaleString()}</span>
    </li>`).join('');

  document.getElementById('stat-unique').textContent = (data.total_unique || 0).toLocaleString();
  document.getElementById('stat-time').textContent = new Date().toLocaleTimeString('es');
  dot.className = 'dot live';
}

function connect() {
  if (es) es.close();
  es = new EventSource(`/stream?n=${topN}`);
  es.onmessage = e => render(JSON.parse(e.data));
  es.onerror = () => { dot.className = 'dot'; setTimeout(connect, 3000); };
}

slider.addEventListener('input', () => {
  topN = parseInt(slider.value);
  topnVal.textContent = topN;
  connect();
});

connect();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    log.info("Visualizer iniciado en puerto %d …", port)
    app.run(host="0.0.0.0", port=port, threaded=True)

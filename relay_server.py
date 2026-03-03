#!/usr/bin/env python3
"""
Pi Camera Relay Server — WebSocket signaling + SSE fallback.
Deploy on Render. Video flows Pi → Browser directly via WebRTC.
Render only handles the tiny SDP/ICE handshake text.
"""
from flask import Flask, Response, jsonify, request, render_template_string, session, redirect, url_for
from flask_sock import Sock
from functools import wraps
import threading, time, logging, os, json, queue, base64

app  = Flask(__name__)
sock = Sock(app)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey123")
logging.getLogger('werkzeug').setLevel(logging.WARNING)

USERS = {
    "admin":  os.environ.get("ADMIN_PASSWORD",  "admin123"),
    "viewer": os.environ.get("VIEWER_PASSWORD", "viewer123"),
}
PUSH_SECRET = os.environ.get("PUSH_SECRET", "changeme123")

# ── WebSocket connections ─────────────────────────────────────
signal_lock      = threading.Lock()
pi_ws            = None          # one Pi connection at a time
viewer_ws_list   = []            # multiple viewers allowed

# HTTP fallback queues (used when WebSocket is unavailable)
http_offers    = queue.Queue(maxsize=5)
http_answers   = queue.Queue(maxsize=5)
http_pi_ice    = queue.Queue(maxsize=100)
http_br_ice    = queue.Queue(maxsize=100)

# ── Fallback frame / data ─────────────────────────────────────
latest_frame = None
ml_results   = {}
cam_status   = {"0": "Waiting for Pi...", "1": "Waiting for Pi..."}
gps_data     = {"lat": None, "lon": None, "speed": None, "updated": None}

frame_lock  = threading.Lock()
ml_lock     = threading.Lock()
status_lock = threading.Lock()
gps_lock    = threading.Lock()

sse_clients      = []
sse_clients_lock = threading.Lock()

# ── Auth ──────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def check_secret():
    # Accept secret from header OR query string (query string used by WebSocket)
    return (request.headers.get('X-Secret') == PUSH_SECRET or
            request.args.get('secret')       == PUSH_SECRET)

# ── Login ─────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        u = request.form.get('username', '')
        p = request.form.get('password', '')
        if u in USERS and USERS[u] == p:
            session['logged_in'] = True
            session['username']  = u
            return redirect(url_for('index'))
        error = 'Wrong username or password'
    html = """<!DOCTYPE html><html><head><title>Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{box-sizing:border-box;margin:0;padding:0}
body{background:#000;color:#0f0;font-family:monospace;display:flex;
  align-items:center;justify-content:center;min-height:100vh}
.box{border:2px solid #0f0;padding:40px;width:320px;text-align:center}
h1{font-size:20px;margin-bottom:24px}
input{width:100%;background:#111;border:1px solid #0f0;color:#0f0;
  padding:10px;margin:8px 0;font-family:monospace;font-size:14px}
button{width:100%;background:#0f0;color:#000;border:none;padding:12px;
  margin-top:16px;cursor:pointer;font-family:monospace;font-size:15px;font-weight:bold}
.error{color:#f44;margin-top:12px;font-size:13px}
</style></head><body>
<div class="box">
  <div style="font-size:40px;margin-bottom:16px">🔒</div>
  <h1>Pi Camera Access</h1>
  <form method="POST">
    <input type="text" name="username" placeholder="Username" required autofocus>
    <input type="password" name="password" placeholder="Password" required>
    <button type="submit">LOGIN</button>
  </form>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
</div></body></html>"""
    return render_template_string(html, error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ══════════════════════════════════════════════════════════════
#  WebSocket — Pi side
# ══════════════════════════════════════════════════════════════
@sock.route('/ws/pi')
def ws_pi(ws):
    """Pi connects here. Secret passed as ?secret=... in URL."""
    if not check_secret():
        ws.close(1008, "Unauthorized")
        return

    global pi_ws
    with signal_lock:
        pi_ws = ws
    print("[WS-PI] Connected")

    # Tell any waiting viewers that Pi is here
    _broadcast_viewers({"type": "pi_connected"})

    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            data = json.loads(raw)
            msg_type = data.get('type')

            if msg_type == 'offer':
                # Forward offer to all viewers
                print("[WS-PI] Got offer → forwarding to viewers")
                _broadcast_viewers({"type": "offer", "sdp": data['sdp']})
                # Also put in HTTP queue for fallback
                try: http_offers.put_nowait(data['sdp'])
                except queue.Full: pass

            elif msg_type == 'ice':
                # Forward ICE to viewers
                _broadcast_viewers({"type": "ice", "candidate": data['candidate']})
                try: http_pi_ice.put_nowait(data['candidate'])
                except queue.Full: pass

    except Exception as e:
        print(f"[WS-PI] Error: {e}")
    finally:
        with signal_lock:
            if pi_ws is ws:
                pi_ws = None
        _broadcast_viewers({"type": "pi_disconnected"})
        print("[WS-PI] Disconnected")


# ══════════════════════════════════════════════════════════════
#  WebSocket — Viewer side
# ══════════════════════════════════════════════════════════════
@sock.route('/ws/viewer')
def ws_viewer(ws):
    """Browser connects here. Must be logged in (session cookie)."""
    # flask-sock doesn't support @login_required directly, so check manually
    # Session cookie is sent automatically by the browser
    if not session.get('logged_in'):
        ws.close(1008, "Login required")
        return

    with signal_lock:
        viewer_ws_list.append(ws)
    print(f"[WS-VIEWER] Connected (total: {len(viewer_ws_list)})")

    # If Pi is already connected, ask it to send a fresh offer
    with signal_lock:
        pi_alive = pi_ws is not None
    if pi_alive:
        try:
            pi_ws.send(json.dumps({"type": "request_offer"}))
        except Exception:
            pass

    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            data = json.loads(raw)
            msg_type = data.get('type')

            if msg_type == 'answer':
                print("[WS-VIEWER] Got answer → forwarding to Pi")
                _send_to_pi({"type": "answer", "sdp": data['sdp']})
                try: http_answers.put_nowait(data['sdp'])
                except queue.Full: pass

            elif msg_type == 'ice':
                _send_to_pi({"type": "ice", "candidate": data['candidate']})
                try: http_br_ice.put_nowait(data['candidate'])
                except queue.Full: pass

            elif msg_type == 'ping':
                ws.send(json.dumps({"type": "pong"}))

    except Exception as e:
        print(f"[WS-VIEWER] Error: {e}")
    finally:
        with signal_lock:
            if ws in viewer_ws_list:
                viewer_ws_list.remove(ws)
        print(f"[WS-VIEWER] Disconnected (remaining: {len(viewer_ws_list)})")


def _broadcast_viewers(msg):
    """Send a message to all connected viewer WebSockets."""
    raw = json.dumps(msg)
    with signal_lock:
        dead = []
        for v in viewer_ws_list:
            try: v.send(raw)
            except Exception: dead.append(v)
        for v in dead:
            if v in viewer_ws_list:
                viewer_ws_list.remove(v)

def _send_to_pi(msg):
    """Send a message to the Pi WebSocket."""
    with signal_lock:
        ws = pi_ws
    if ws:
        try: ws.send(json.dumps(msg))
        except Exception as e: print(f"[WS] Send to Pi failed: {e}")


# ══════════════════════════════════════════════════════════════
#  HTTP Fallback Signaling (for environments where WS fails)
# ══════════════════════════════════════════════════════════════
@app.route('/push/offer', methods=['POST'])
def http_push_offer():
    if not check_secret(): return 'Unauthorized', 401
    data = request.get_json()
    try: http_offers.put_nowait(data)
    except queue.Full: pass
    _broadcast_viewers({"type": "offer", "sdp": data})
    return 'OK', 200

@app.route('/push/ice', methods=['POST'])
def http_push_ice():
    if not check_secret(): return 'Unauthorized', 401
    data = request.get_json()
    try: http_pi_ice.put_nowait(data)
    except queue.Full: pass
    _broadcast_viewers({"type": "ice", "candidate": data})
    return 'OK', 200

@app.route('/signal/offer')
@login_required
def http_get_offer():
    """Browser long-polls for Pi's SDP offer."""
    try:
        offer = http_offers.get(timeout=10)
        return jsonify(offer)
    except queue.Empty:
        return jsonify(None), 204

@app.route('/signal/answer', methods=['POST'])
@login_required
def http_post_answer():
    data = request.get_json()
    try: http_answers.put_nowait(data)
    except queue.Full: pass
    _send_to_pi({"type": "answer", "sdp": data})
    return 'OK', 200

@app.route('/signal/ice', methods=['POST'])
@login_required
def http_post_ice():
    data = request.get_json()
    try: http_br_ice.put_nowait(data)
    except queue.Full: pass
    _send_to_pi({"type": "ice", "candidate": data})
    return 'OK', 200

@app.route('/signal/ice/pi')
@login_required
def http_get_pi_ice():
    """Browser polls for Pi's ICE candidates."""
    candidates = []
    try:
        while True:
            candidates.append(http_pi_ice.get_nowait())
    except queue.Empty:
        pass
    return jsonify(candidates)

@app.route('/poll/answer')
def http_poll_answer():
    """Pi polls for browser's SDP answer."""
    if not check_secret(): return 'Unauthorized', 401
    try:
        answer = http_answers.get(timeout=20)
        return jsonify(answer)
    except queue.Empty:
        return jsonify(None), 204

@app.route('/poll/ice')
def http_poll_ice():
    """Pi polls for browser's ICE candidates."""
    if not check_secret(): return 'Unauthorized', 401
    candidates = []
    try:
        while True:
            candidates.append(http_br_ice.get_nowait())
    except queue.Empty:
        pass
    return jsonify(candidates)


# ══════════════════════════════════════════════════════════════
#  Pi data push endpoints
# ══════════════════════════════════════════════════════════════
@app.route('/push/frame', methods=['POST'])
def push_frame():
    if not check_secret(): return 'Unauthorized', 401
    global latest_frame
    data = request.get_data()
    if len(data) < 1024: return 'Too small', 400
    with frame_lock:
        latest_frame = data
    b64 = base64.b64encode(data).decode('ascii')
    with sse_clients_lock:
        dead = []
        for q in sse_clients:
            try: q.put_nowait(b64)
            except Exception: dead.append(q)
        for q in dead: sse_clients.remove(q)
    return 'OK', 200

@app.route('/push/ml', methods=['POST'])
def push_ml():
    if not check_secret(): return 'Unauthorized', 401
    global ml_results
    with ml_lock: ml_results = request.get_json() or {}
    return 'OK', 200

@app.route('/push/status', methods=['POST'])
def push_status():
    if not check_secret(): return 'Unauthorized', 401
    global cam_status
    with status_lock: cam_status = request.get_json() or {}
    return 'OK', 200

@app.route('/push/gps', methods=['POST'])
def push_gps():
    if not check_secret(): return 'Unauthorized', 401
    global gps_data
    d = request.get_json() or {}
    with gps_lock:
        gps_data = {"lat": d.get("lat"), "lon": d.get("lon"),
                    "speed": d.get("speed"), "updated": time.strftime("%H:%M:%S")}
    return 'OK', 200


# ══════════════════════════════════════════════════════════════
#  Browser data endpoints
# ══════════════════════════════════════════════════════════════
@app.route('/snapshot.jpg')
@login_required
def snapshot():
    with frame_lock: frame = latest_frame
    if frame is None: return 'No frame yet', 503
    return Response(frame, mimetype='image/jpeg',
                    headers={'Cache-Control': 'no-cache, no-store'})

@app.route('/stream/sse')
@login_required
def stream_sse():
    q = queue.Queue(maxsize=4)
    with sse_clients_lock: sse_clients.append(q)
    def generate():
        try:
            with frame_lock: cur = latest_frame
            if cur:
                yield f"data:{base64.b64encode(cur).decode('ascii')}\n\n"
            while True:
                try:    yield f"data:{q.get(timeout=30)}\n\n"
                except: yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with sse_clients_lock:
                try: sse_clients.remove(q)
                except ValueError: pass
    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

@app.route('/ml_results')
@login_required
def get_ml():
    with ml_lock: return jsonify(ml_results)

@app.route('/status')
@login_required
def get_status():
    with status_lock: return jsonify(cam_status)

@app.route('/gps')
@login_required
def get_gps():
    with gps_lock: return jsonify(gps_data)

@app.route('/ping')
def ping(): return 'pong', 200


# ══════════════════════════════════════════════════════════════
#  Main UI
# ══════════════════════════════════════════════════════════════
@app.route('/')
@login_required
def index():
    html = r"""<!DOCTYPE html>
<html>
<head>
<title>Pi Camera — Live</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--cyan:#00e5ff;--orange:#ff6d00;--green:#00e676;--red:#ff1744;
  --yellow:#ffd600;--bg:#050a0e;--panel:#0a1520;--border:#1a2e40;
  --text:#c8d8e8;--dim:#4a6070}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace}
.header{display:flex;align-items:center;justify-content:space-between;
  padding:10px 20px;background:var(--panel);border-bottom:1px solid var(--border)}
.logo{font-size:20px;font-weight:700;letter-spacing:3px;color:var(--cyan)}
.logo span{color:var(--orange)}
.hright{display:flex;gap:14px;align-items:center;font-size:11px;flex-wrap:wrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);
  display:inline-block;margin-right:4px;transition:background .3s}
.dot.live{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.warn{background:var(--orange)}.dot.err{background:var(--red)}
a.logout{color:var(--red);text-decoration:none;border:1px solid var(--red);padding:3px 10px}

.stream-box{background:#000;display:flex;justify-content:center;
  position:relative;border-bottom:2px solid var(--border);min-height:200px}
#videoEl{display:none;width:100%;max-width:1280px;background:#000}
#fallbackImg{display:block;width:100%;max-width:1280px;object-fit:contain}
.stream-badge{position:absolute;top:10px;left:14px;font-size:11px;
  letter-spacing:2px;padding:3px 10px;font-weight:700;display:none}
.badge-live{background:rgba(0,230,118,.2);color:var(--green);border:1px solid var(--green)}
.badge-sse{background:rgba(255,109,0,.2);color:var(--orange);border:1px solid var(--orange)}
#conn-msg{text-align:center;background:var(--panel);font-size:12px;
  padding:6px;min-height:24px;border-bottom:1px solid var(--border)}

.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;padding:14px 20px}
@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}}
@media(max-width:580px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);padding:14px}
.panel-title{font-size:11px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;
  border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:10px}
.stat-row{display:flex;justify-content:space-between;font-size:11px;
  padding:4px 0;border-bottom:1px solid #0d1e2e}
.sk{color:var(--dim)}.sv{color:var(--text);max-width:60%;text-align:right;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sv.ok{color:var(--green)}.sv.err{color:var(--red)}
.btn{background:transparent;color:var(--text);border:1px solid var(--border);
  padding:9px 14px;cursor:pointer;font-family:monospace;font-size:12px;
  width:100%;margin-bottom:6px;transition:all .2s;text-align:left}
.btn:hover{border-color:var(--cyan);color:var(--cyan)}
.btn.active{background:rgba(0,229,255,.15);color:var(--cyan);
  border-color:var(--cyan);box-shadow:0 0 8px rgba(0,229,255,.3)}

.ml-cam{margin-bottom:10px}
.ml-hdr{font-size:11px;letter-spacing:2px;padding:3px 8px;
  display:inline-block;margin-bottom:6px}
.ml-f{background:rgba(0,229,255,.15);color:var(--cyan);border:1px solid var(--cyan)}
.ml-r{background:rgba(255,109,0,.15);color:var(--orange);border:1px solid var(--orange)}
.ml-none{color:var(--dim);font-size:11px}
.alert-box{border:2px solid var(--red);background:rgba(255,23,68,.1);
  padding:8px 10px;animation:pulse 1s infinite alternate}
@keyframes pulse{from{box-shadow:0 0 4px var(--red)}to{box-shadow:0 0 16px var(--red)}}
.alert-title{font-size:14px;font-weight:700;color:var(--red);letter-spacing:2px}
.alert-detail{font-size:11px;color:#ffb3b3;margin-top:3px}
.vbar-wrap{background:#0d1e2e;height:10px;margin:6px 0;overflow:hidden;border-radius:2px}
.vbar-fill{height:100%;border-radius:2px;transition:width .4s}

.gps-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.gk{font-size:10px;color:var(--dim);letter-spacing:1px}
.gv{font-size:16px;font-weight:700;color:var(--green)}
#map{height:200px;border:1px solid var(--border);margin-top:8px;display:none}
#maplink{font-size:11px;color:var(--cyan);margin-top:4px;display:none;text-decoration:none}

#crash-modal{display:none;position:fixed;inset:0;z-index:9999;
  background:rgba(0,0,0,.8);align-items:center;justify-content:center}
.modal-box{background:#0d0608;border:2px solid var(--red);max-width:460px;
  width:90%;padding:24px 28px;box-shadow:0 0 40px rgba(255,23,68,.5)}
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>

<div class="header">
  <div class="logo">PI<span>CAM</span> <span style="font-size:11px;color:var(--dim)">LIVE</span></div>
  <div class="hright">
    <span><span class="dot" id="dot-ws"></span>WS</span>
    <span><span class="dot" id="dot-webrtc"></span>RTC</span>
    <span><span class="dot" id="dot-pi"></span>Pi</span>
    <span style="color:var(--cyan)" id="hdr-time">--:--:--</span>
    <span>{{ username }}</span>
    <a class="logout" href="/logout">OUT</a>
  </div>
</div>

<div class="stream-box">
  <video id="videoEl" autoplay playsinline muted></video>
  <img   id="fallbackImg" alt="">
  <span class="stream-badge badge-live" id="badge-live">● WEBRTC LIVE</span>
  <span class="stream-badge badge-sse"  id="badge-sse">● SSE FALLBACK</span>
</div>
<div id="conn-msg" style="color:var(--dim)">Connecting...</div>

<div class="grid">

  <div class="panel">
    <div class="panel-title">◉ Status</div>
    <div class="stat-row"><span class="sk">FRONT</span><span class="sv" id="s-front">—</span></div>
    <div class="stat-row"><span class="sk">REAR</span> <span class="sv" id="s-rear">—</span></div>
    <div class="stat-row"><span class="sk">STREAM</span><span class="sv" id="s-stream">—</span></div>
    <div class="stat-row"><span class="sk">SIGNAL</span><span class="sv" id="s-signal">—</span></div>
    <div style="margin-top:10px">
      <button class="btn" id="btn-ml"  onclick="toggleML()">▶ ML Detection</button>
      <button class="btn" onclick="window.open('/snapshot.jpg','_blank')">📷 Snapshot</button>
      <button class="btn" onclick="doReconnect()">↻ Reconnect</button>
    </div>
  </div>

  <div class="panel" style="grid-column:span 2">
    <div class="panel-title">⚠ Accident Detection</div>
    <div id="ml-panel"><div class="ml-none">ML Detection: OFF</div></div>
  </div>

  <div class="panel" style="grid-column:span 2">
    <div class="panel-title">◎ GPS</div>
    <div class="gps-grid">
      <div><div class="gk">LATITUDE</div> <div class="gv" id="g-lat">—</div></div>
      <div><div class="gk">LONGITUDE</div><div class="gv" id="g-lon">—</div></div>
      <div><div class="gk">SPEED</div>    <div class="gv" id="g-spd">—</div></div>
      <div><div class="gk">UPDATED</div>  <div class="gv" id="g-upd" style="font-size:11px;color:var(--dim)">—</div></div>
    </div>
    <div id="map"></div>
    <a id="maplink" href="#" target="_blank">📌 Google Maps</a>
  </div>

  <div class="panel">
    <div class="panel-title">ℹ Latency</div>
    <div style="font-size:11px;color:var(--dim);line-height:2.2">
      WS + WebRTC:<br>
      <span style="color:var(--green)">~30–100ms ⚡</span><br>
      HTTP + WebRTC:<br>
      <span style="color:var(--yellow)">~100–300ms</span><br>
      SSE fallback:<br>
      <span style="color:var(--orange)">~400–800ms</span>
    </div>
  </div>

</div>

<div id="crash-modal">
  <div class="modal-box">
    <div style="font-size:22px;font-weight:700;color:var(--red);letter-spacing:3px;margin-bottom:8px">
      🚨 CRASH CONFIRMED
    </div>
    <div style="font-size:13px;color:#ffb3b3;margin-bottom:14px" id="modal-detail">—</div>
    <div style="font-size:11px;color:var(--dim);margin-bottom:16px">
      Held ≥ <span style="color:var(--yellow)" id="modal-secs">3</span>s — real event.
    </div>
    <div style="display:flex;gap:8px">
      <button onclick="document.getElementById('crash-modal').style.display='none'"
        style="flex:1;padding:10px;font-family:monospace;border:1px solid var(--dim);
               background:transparent;color:var(--dim);cursor:pointer">DISMISS</button>
      <button onclick="document.getElementById('crash-modal').style.display='none'"
        style="flex:2;padding:10px;font-family:monospace;border:1px solid var(--red);
               background:rgba(255,23,68,.15);color:var(--red);cursor:pointer">ACKNOWLEDGE</button>
    </div>
  </div>
</div>

<script>
// ══════════════════════════════════════════════════════════
//  State
// ══════════════════════════════════════════════════════════
const ICE_CFG = {
  iceServers: [
    {urls:'stun:stun.l.google.com:19302'},
    {urls:'stun:stun1.l.google.com:19302'},
    {urls:'stun:stun.cloudflare.com:3478'},
  ]
};

let pc          = null;
let ws          = null;
let sseEs       = null;
let webrtcLive  = false;
let wsAlive     = false;

// ── Helpers ───────────────────────────────────────────────
function setMsg(t, c){ const e=document.getElementById('conn-msg'); e.textContent=t; e.style.color=c||'var(--dim)'; }
function setStreamStat(t,c){ const e=document.getElementById('s-stream'); e.textContent=t; e.className='sv'+(c?' '+c:''); }
function setSignalStat(t){ document.getElementById('s-signal').textContent=t; }
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }

// ── Clock ─────────────────────────────────────────────────
setInterval(()=>{ document.getElementById('hdr-time').textContent=new Date().toTimeString().slice(0,8); },1000);

// ══════════════════════════════════════════════════════════
//  SSE Fallback — starts immediately so there's something to see
// ══════════════════════════════════════════════════════════
function startSSE(){
  if(sseEs) return;
  document.getElementById('badge-sse').style.display  = 'block';
  document.getElementById('badge-live').style.display = 'none';
  document.getElementById('fallbackImg').style.display = 'block';
  setStreamStat('SSE fallback');

  sseEs = new EventSource('/stream/sse');
  sseEs.onmessage = e => {
    document.getElementById('fallbackImg').src = 'data:image/jpeg;base64,'+e.data;
    document.getElementById('dot-pi').className = 'dot live';
    if(!webrtcLive) setMsg('SSE fallback active (~500ms latency)','var(--orange)');
  };
  sseEs.onerror = ()=>{ stopSSE(); setTimeout(startSSE,3000); };
}
function stopSSE(){
  if(sseEs){ sseEs.close(); sseEs=null; }
}

// ══════════════════════════════════════════════════════════
//  WebRTC — called with an SDP offer from Pi
// ══════════════════════════════════════════════════════════
async function handleOffer(sdp){
  if(pc){ pc.close(); pc=null; }
  setMsg('Received Pi offer — creating answer...','var(--yellow)');

  pc = new RTCPeerConnection(ICE_CFG);

  pc.ontrack = ev => {
    if(!ev.streams || !ev.streams[0]) return;
    const v = document.getElementById('videoEl');
    v.srcObject = ev.streams[0];
    v.style.display = 'block';
    document.getElementById('fallbackImg').style.display = 'none';
    document.getElementById('badge-live').style.display  = 'block';
    document.getElementById('badge-sse').style.display   = 'none';
    document.getElementById('dot-webrtc').className = 'dot live';
    document.getElementById('dot-pi').className     = 'dot live';
    webrtcLive = true;
    setMsg('⚡ WebRTC live — low latency!','var(--green)');
    setStreamStat('WebRTC ⚡','ok');
    stopSSE();
  };

  // Send our ICE candidates to Pi
  pc.onicecandidate = ev => {
    if(!ev.candidate) return;
    const msg = {type:'ice', candidate:ev.candidate};
    if(wsAlive && ws && ws.readyState===WebSocket.OPEN){
      ws.send(JSON.stringify(msg));
    } else {
      fetch('/signal/ice',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({candidate:ev.candidate})}).catch(()=>{});
    }
  };

  pc.onconnectionstatechange = ()=>{
    const s = pc.connectionState;
    if(s==='failed'||s==='disconnected'||s==='closed'){
      webrtcLive = false;
      document.getElementById('dot-webrtc').className='dot err';
      setMsg('WebRTC dropped — reconnecting...','var(--red)');
      setStreamStat('Reconnecting...');
      startSSE();
      setTimeout(doReconnect, 3000);
    }
  };

  try {
    await pc.setRemoteDescription(new RTCSessionDescription(sdp));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);

    // Send answer to Pi
    const answerMsg = {type:'answer', sdp: answer};
    if(wsAlive && ws && ws.readyState===WebSocket.OPEN){
      ws.send(JSON.stringify(answerMsg));
    } else {
      await fetch('/signal/answer',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(answer)});
    }

    // If using HTTP fallback, poll for Pi's ICE
    if(!wsAlive){ pollPiIce(); }

  } catch(err){
    console.error('WebRTC handleOffer error:', err);
    setMsg('WebRTC failed — using SSE fallback','var(--orange)');
    startSSE();
  }
}

// Add Pi ICE candidates we receive
async function addPiIce(candidate){
  if(pc && candidate){
    try { await pc.addIceCandidate(new RTCIceCandidate(candidate)); }
    catch(e){ console.warn('addIceCandidate:', e); }
  }
}

// HTTP fallback: poll for Pi ICE candidates
async function pollPiIce(){
  for(let i=0; i<30; i++){
    try{
      const res = await fetch('/signal/ice/pi');
      const cands = await res.json();
      for(const c of (cands||[])){ await addPiIce(c); }
    } catch(e){}
    await sleep(1000);
  }
}

// ══════════════════════════════════════════════════════════
//  WebSocket Signaling (primary path)
// ══════════════════════════════════════════════════════════
function connectWS(){
  document.getElementById('dot-ws').className = 'dot warn';
  setSignalStat('WS connecting...');

  const proto = location.protocol==='https:'?'wss:':'ws:';
  ws = new WebSocket(proto+'//'+location.host+'/ws/viewer');

  ws.onopen = ()=>{
    wsAlive = true;
    document.getElementById('dot-ws').className='dot live';
    setSignalStat('WS ✓');
    setMsg('WebSocket connected — waiting for Pi offer...','var(--yellow)');
  };

  ws.onmessage = async e => {
    let data;
    try { data=JSON.parse(e.data); } catch{ return; }

    if(data.type==='offer'){
      await handleOffer(data.sdp);
    } else if(data.type==='ice'){
      await addPiIce(data.candidate);
    } else if(data.type==='pi_connected'){
      setMsg('Pi connected via WebSocket — offer incoming...','var(--yellow)');
    } else if(data.type==='pi_disconnected'){
      setMsg('Pi disconnected','var(--orange)');
      document.getElementById('dot-pi').className='dot err';
    }
  };

  ws.onclose = ()=>{
    wsAlive = false;
    document.getElementById('dot-ws').className='dot err';
    setSignalStat('WS ✗ (HTTP fallback)');
    setMsg('WebSocket closed — using HTTP signaling fallback','var(--orange)');
    // Fall through to HTTP polling
    pollForOffer();
    setTimeout(connectWS, 8000); // try reconnecting WS later
  };

  ws.onerror = ()=>{
    document.getElementById('dot-ws').className='dot err';
  };
}

// ══════════════════════════════════════════════════════════
//  HTTP Signaling Fallback (if WS fails)
// ══════════════════════════════════════════════════════════
async function pollForOffer(){
  setMsg('HTTP fallback: polling for Pi offer...','var(--orange)');
  for(let i=0; i<20; i++){
    if(wsAlive) return; // WS came back, stop HTTP polling
    try{
      const res=await fetch('/signal/offer');
      if(res.status===200){
        const offer=await res.json();
        if(offer){ await handleOffer(offer); return; }
      }
    } catch(e){}
    await sleep(2000);
  }
  setMsg('No offer received — check Pi is running','var(--red)');
  startSSE();
}

// ══════════════════════════════════════════════════════════
//  Reconnect
// ══════════════════════════════════════════════════════════
function doReconnect(){
  if(pc){ pc.close(); pc=null; }
  webrtcLive=false;
  document.getElementById('dot-webrtc').className='dot warn';
  if(wsAlive && ws && ws.readyState===WebSocket.OPEN){
    setMsg('Requesting new offer from Pi...','var(--yellow)');
    // Pi will re-send offer on its own when it detects viewer reconnect
  } else {
    pollForOffer();
  }
}

// ══════════════════════════════════════════════════════════
//  ML Detection
// ══════════════════════════════════════════════════════════
let mlOn=false, mlTimer=null, modalDone={0:false,1:false};

function toggleML(){
  mlOn=!mlOn;
  const btn=document.getElementById('btn-ml');
  btn.className='btn'+(mlOn?' active':'');
  btn.textContent=(mlOn?'⏹ Disable':'▶ Enable')+' ML Detection';
  if(mlOn){ mlTimer=setInterval(updateML,1200); updateML(); }
  else{
    clearInterval(mlTimer);
    document.getElementById('ml-panel').innerHTML='<div class="ml-none">ML Detection: OFF</div>';
    modalDone={0:false,1:false};
  }
}

function updateML(){
  fetch('/ml_results').then(r=>r.json()).then(data=>{
    let html='';
    [{k:'0',l:'FRONT',c:'ml-f'},{k:'1',l:'REAR',c:'ml-r'}].forEach(({k,l,c})=>{
      const d=data[k], idx=parseInt(k);
      html+=`<div class="ml-cam"><span class="ml-hdr ${c}">${l} CAM</span>`;
      if(!d||(!d.first_seen&&!d.confirmed)){
        html+='<div class="ml-none" style="margin:4px 0 8px">No detections</div>';
      } else if(d.confirmed){
        const best=d.boxes&&d.boxes.length?d.boxes.reduce((a,b)=>a.conf>b.conf?a:b):null;
        html+=`<div class="alert-box">
          <div class="alert-title">🚨 CRASH CONFIRMED</div>
          <div class="alert-detail">${best?'Side: '+best.side+' | Conf: '+(best.conf*100).toFixed(1)+'%':''}</div>
        </div>`;
        if(!modalDone[idx]&&best){
          modalDone[idx]=true;
          document.getElementById('modal-detail').textContent=
            'Camera: '+l+' | Side: '+best.side+' | Conf: '+(best.conf*100).toFixed(1)+'%';
          document.getElementById('modal-secs').textContent=(d.elapsed||0).toFixed(1);
          document.getElementById('crash-modal').style.display='flex';
        }
      } else if(d.first_seen){
        const secs=d.confirm_secs||3, pct=Math.min(1,d.elapsed/secs);
        const r=Math.round(255*pct), g=Math.round(255*(1-pct));
        html+=`<div style="font-size:11px;color:var(--yellow);margin:4px 0">
          ⏱ VERIFYING — ${d.elapsed.toFixed(1)}s / ${secs}s</div>
          <div class="vbar-wrap"><div class="vbar-fill"
            style="width:${(pct*100).toFixed(1)}%;background:rgb(${r},${g},0)"></div></div>`;
        if(d.boxes&&d.boxes.length){
          const b=d.boxes.reduce((a,x)=>a.conf>x.conf?a:x);
          html+=`<div style="font-size:11px;color:var(--dim);margin-top:4px">
            ${b.label} · ${b.side} · ${(b.conf*100).toFixed(1)}%</div>`;
        }
        modalDone[idx]=false;
      } else { modalDone[idx]=false; }
      html+='</div>';
    });
    document.getElementById('ml-panel').innerHTML=html;
  }).catch(()=>{});
}

// ══════════════════════════════════════════════════════════
//  Camera status
// ══════════════════════════════════════════════════════════
function updateStatus(){
  fetch('/status').then(r=>r.json()).then(d=>{
    function s(id,txt){
      const e=document.getElementById(id); e.textContent=txt;
      e.className='sv'+(txt.includes('Streaming')?' ok':txt.includes('ERROR')?' err':'');
    }
    s('s-front',d['0']||'—'); s('s-rear',d['1']||'—');
  }).catch(()=>{});
}
setInterval(updateStatus,3000); updateStatus();

// ══════════════════════════════════════════════════════════
//  GPS
// ══════════════════════════════════════════════════════════
let leafMap=null, leafMarker=null;
function updateGPS(){
  fetch('/gps').then(r=>r.json()).then(d=>{
    if(!d.lat||!d.lon) return;
    document.getElementById('g-lat').textContent=d.lat.toFixed(6)+'°';
    document.getElementById('g-lon').textContent=d.lon.toFixed(6)+'°';
    document.getElementById('g-spd').textContent=d.speed!=null?d.speed.toFixed(1)+' km/h':'—';
    document.getElementById('g-upd').textContent=d.updated||'—';
    const ml=document.getElementById('maplink');
    ml.style.display='block'; ml.href='https://maps.google.com/?q='+d.lat+','+d.lon;
    document.getElementById('map').style.display='block';
    if(!leafMap){
      leafMap=L.map('map').setView([d.lat,d.lon],16);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        {attribution:'© OSM'}).addTo(leafMap);
      leafMarker=L.marker([d.lat,d.lon]).addTo(leafMap).bindPopup('Pi Camera').openPopup();
    } else {
      leafMarker.setLatLng([d.lat,d.lon]);
      leafMap.setView([d.lat,d.lon]);
    }
  }).catch(()=>{});
}
setInterval(updateGPS,5000); updateGPS();

// ══════════════════════════════════════════════════════════
//  Boot
// ══════════════════════════════════════════════════════════
startSSE();      // show something immediately
connectWS();     // try WebSocket first (fastest path)
</script>
</body>
</html>"""
    return render_template_string(html, username=session.get('username',''))


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

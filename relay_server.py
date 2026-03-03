#!/usr/bin/env python3
"""
Enhanced Real-Time Relay Server — WebRTC signaling only.
Optimized for <50ms latency with WebSocket signaling.
No video data passes through here at all.
Pi <-> Render <-> Browser exchange SDP offer/answer + ICE candidates,
then video flows DIRECTLY Pi → Browser via WebRTC (~30-80ms latency).

Also keeps SSE snapshot fallback for crash alerts / GPS dashboard.
"""
from flask import Flask, Response, jsonify, request, render_template_string, session, redirect, url_for
from flask_sock import Sock
from functools import wraps
import threading, time, logging, os, json
import queue
import base64

app = Flask(__name__)
sock = Sock(app)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey123")
logging.getLogger('werkzeug').setLevel(logging.WARNING)

USERS = {
    "admin":  os.environ.get("ADMIN_PASSWORD", "admin123"),
    "viewer": os.environ.get("VIEWER_PASSWORD", "viewer123"),
}
PUSH_SECRET = os.environ.get("PUSH_SECRET", "changeme123")

# ── WebRTC signaling state with WebSockets ────────────────────
signal_lock = threading.Lock()
pi_connections = {}  # pi_id -> websocket
viewer_connections = {}  # viewer_id -> websocket
pending_offers = queue.Queue(maxsize=10)
pending_answers = queue.Queue(maxsize=10)
pending_ice_candidates = queue.Queue(maxsize=100)

# ── Fallback frame / data state ───────────────────────────────
latest_frame = None
ml_results = {}
cam_status = {"0": "Waiting for Pi...", "1": "Waiting for Pi..."}
gps_data = {"lat": None, "lon": None, "speed": None, "updated": None}

frame_lock = threading.Lock()
ml_lock = threading.Lock()
status_lock = threading.Lock()
gps_lock = threading.Lock()

sse_clients = []
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
    return request.headers.get('X-Secret') == PUSH_SECRET

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        u = request.form.get('username', '')
        p = request.form.get('password', '')
        if u in USERS and USERS[u] == p:
            session['logged_in'] = True
            session['username'] = u
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

# ── WebSocket Signaling ───────────────────────────────────────

@sock.route('/ws/pi')
def pi_websocket(ws):
    """Pi connects via WebSocket for real-time signaling"""
    if not check_secret():
        ws.close(1008, "Unauthorized")
        return
    
    pi_id = request.args.get('pi_id', 'default')
    with signal_lock:
        pi_connections[pi_id] = ws
    
    print(f"[WS] Pi {pi_id} connected")
    
    try:
        while True:
            message = ws.receive()
            if not message:
                break
            
            data = json.loads(message)
            msg_type = data.get('type')
            
            if msg_type == 'offer':
                # Forward offer to waiting viewers
                pending_offers.put({
                    'pi_id': pi_id,
                    'sdp': data['sdp']
                })
                print(f"[SIGNAL] Offer from Pi {pi_id}")
                
            elif msg_type == 'ice':
                # Forward ICE candidate to viewers
                pending_ice_candidates.put({
                    'pi_id': pi_id,
                    'candidate': data['candidate']
                })
                
            elif msg_type == 'answer':
                # Pi received answer from viewer? (unusual but handle)
                pass
                
    except Exception as e:
        print(f"[WS] Pi error: {e}")
    finally:
        with signal_lock:
            if pi_id in pi_connections:
                del pi_connections[pi_id]
        print(f"[WS] Pi {pi_id} disconnected")

@sock.route('/ws/viewer')
@login_required
def viewer_websocket(ws):
    """Browser connects via WebSocket for real-time signaling"""
    viewer_id = session.get('username', 'viewer') + '_' + str(time.time())
    with signal_lock:
        viewer_connections[viewer_id] = ws
    
    print(f"[WS] Viewer {viewer_id} connected")
    
    try:
        # Immediately request an offer if any Pi is connected
        if pi_connections:
            ws.send(json.dumps({
                'type': 'request_offer',
                'message': 'Pi available, initiating connection...'
            }))
        
        while True:
            message = ws.receive()
            if not message:
                break
            
            data = json.loads(message)
            msg_type = data.get('type')
            
            if msg_type == 'answer':
                # Forward answer to Pi
                pending_answers.put({
                    'viewer_id': viewer_id,
                    'sdp': data['sdp']
                })
                print(f"[SIGNAL] Answer from viewer")
                
            elif msg_type == 'ice':
                # Forward ICE candidate to Pi
                pending_ice_candidates.put({
                    'viewer_id': viewer_id,
                    'candidate': data['candidate']
                })
                
    except Exception as e:
        print(f"[WS] Viewer error: {e}")
    finally:
        with signal_lock:
            if viewer_id in viewer_connections:
                del viewer_connections[viewer_id]
        print(f"[WS] Viewer {viewer_id} disconnected")

# ── HTTP fallback signaling (for compatibility) ───────────────

@app.route('/push/offer', methods=['POST'])
def push_offer():
    """Pi sends its WebRTC SDP offer (HTTP fallback)"""
    if not check_secret(): return 'Unauthorized', 401
    data = request.get_json()
    pending_offers.put({
        'pi_id': 'http_pi',
        'sdp': data
    })
    print("[SIGNAL] HTTP Offer from Pi")
    return 'OK', 200

@app.route('/push/ice', methods=['POST'])
def push_ice_from_pi():
    """Pi sends its ICE candidates (HTTP fallback)"""
    if not check_secret(): return 'Unauthorized', 401
    data = request.get_json()
    pending_ice_candidates.put({
        'pi_id': 'http_pi',
        'candidate': data
    })
    return 'OK', 200

@app.route('/signal/offer')
@login_required
def get_offer():
    """Browser polls here to get Pi's SDP offer (HTTP fallback)"""
    try:
        offer_data = pending_offers.get(timeout=10)
        if offer_data:
            return jsonify(offer_data['sdp'])
    except queue.Empty:
        pass
    return jsonify(None), 204

@app.route('/signal/answer', methods=['POST'])
@login_required
def post_answer():
    """Browser posts its SDP answer (HTTP fallback)"""
    data = request.get_json()
    pending_answers.put({
        'viewer_id': 'http_viewer',
        'sdp': data
    })
    return 'OK', 200

@app.route('/poll/answer')
def poll_answer():
    """Pi long-polls here waiting for browser SDP answer (HTTP fallback)"""
    if not check_secret(): return 'Unauthorized', 401
    try:
        answer_data = pending_answers.get(timeout=20)
        if answer_data:
            return jsonify(answer_data['sdp'])
    except queue.Empty:
        pass
    return jsonify(None), 204

@app.route('/signal/ice', methods=['POST'])
@login_required
def post_ice_from_browser():
    """Browser posts its ICE candidates (HTTP fallback)"""
    data = request.get_json()
    pending_ice_candidates.put({
        'viewer_id': 'http_viewer',
        'candidate': data
    })
    return 'OK', 200

@app.route('/signal/ice/pi')
@login_required
def get_pi_ice():
    """Browser polls here to get Pi's ICE candidates (HTTP fallback)"""
    candidates = []
    try:
        while True:
            ice_data = pending_ice_candidates.get_nowait()
            if ice_data.get('pi_id'):
                candidates.append(ice_data['candidate'])
    except queue.Empty:
        pass
    return jsonify(candidates)

@app.route('/poll/ice')
def poll_browser_ice():
    """Pi polls here for browser ICE candidates (HTTP fallback)"""
    if not check_secret(): return 'Unauthorized', 401
    candidates = []
    try:
        while True:
            ice_data = pending_ice_candidates.get_nowait()
            if ice_data.get('viewer_id'):
                candidates.append(ice_data['candidate'])
    except queue.Empty:
        pass
    return jsonify(candidates)

# ── Pi push endpoints (unchanged) ─────────────────────────────

@app.route('/push/frame', methods=['POST'])
def push_frame():
    """Fallback snapshot — Pi still pushes this for the dashboard thumbnail."""
    if not check_secret(): return 'Unauthorized', 401
    global latest_frame
    data = request.get_data()
    if len(data) < 1024: return 'Too small', 400
    with frame_lock:
        latest_frame = data
    
    # Notify SSE clients
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
    data = request.get_json() or {}
    with gps_lock:
        gps_data = {"lat": data.get("lat"), "lon": data.get("lon"),
                    "speed": data.get("speed"), "updated": time.strftime("%H:%M:%S")}
    return 'OK', 200

# ── Browser data endpoints ────────────────────────────────────

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
    """SSE fallback stream — fires immediately when Pi pushes a frame."""
    import queue as _queue
    q = _queue.Queue(maxsize=4)
    with sse_clients_lock: sse_clients.append(q)
    
    def generate():
        try:
            with frame_lock: current = latest_frame
            if current:
                yield f"data:{base64.b64encode(current).decode('ascii')}\n\n"
            while True:
                try:
                    b64 = q.get(timeout=30)
                    yield f"data:{b64}\n\n"
                except Exception:
                    yield ": keepalive\n\n"
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

# ── Main UI with WebSocket support ────────────────────────────
@app.route('/')
@login_required
def index():
    html = r"""<!DOCTYPE html>
<html>
<head>
<title>Pi Camera — Real-Time</title>
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
.hright{display:flex;gap:14px;align-items:center;font-size:11px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);
  display:inline-block;margin-right:4px;transition:background .3s}
.dot.live{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.warn{background:var(--orange)}.dot.err{background:var(--red)}
a.logout{color:var(--red);text-decoration:none;border:1px solid var(--red);padding:3px 10px}
.stream-box{background:#000;display:flex;justify-content:center;
  position:relative;border-bottom:2px solid var(--border)}
#videoEl{display:none;width:100%;max-width:1280px;background:#000;min-height:200px}
#fallbackImg{display:block;width:100%;max-width:1280px;min-height:200px;object-fit:contain}
.stream-badge{position:absolute;top:10px;left:14px;font-size:11px;
  letter-spacing:2px;padding:3px 10px;font-weight:700}
.badge-webrtc{background:rgba(0,230,118,.2);color:var(--green);border:1px solid var(--green)}
.badge-fallback{background:rgba(255,109,0,.2);color:var(--orange);border:1px solid var(--orange)}
#conn-msg{text-align:center;background:var(--panel);font-size:12px;
  padding:5px;min-height:22px;border-bottom:1px solid var(--border);color:var(--dim)}
#latency-meter{position:absolute;top:10px;right:14px;background:rgba(0,0,0,0.7);
  padding:4px 10px;border-radius:12px;font-size:11px;color:var(--green);
  border:1px solid var(--green);display:none}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;padding:14px 20px}
@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}}
@media(max-width:580px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);padding:14px}
.panel-title{font-size:11px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;
  border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:10px}
.stat-row{display:flex;justify-content:space-between;font-size:11px;
  padding:4px 0;border-bottom:1px solid #0d1e2e}
.sk{color:var(--dim)}.sv{color:var(--text);max-width:55%;text-align:right;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sv.ok{color:var(--green)}.sv.err{color:var(--red)}
.btn{background:transparent;color:var(--text);border:1px solid var(--border);
  padding:9px 14px;cursor:pointer;font-family:monospace;font-size:12px;
  width:100%;margin-bottom:6px;transition:all .2s}
.btn:hover{border-color:var(--cyan);color:var(--cyan)}
.btn.active{background:rgba(0,229,255,.15);color:var(--cyan);
  border-color:var(--cyan);box-shadow:0 0 8px rgba(0,229,255,.3)}
.ml-cam{margin-bottom:10px}
.ml-hdr{font-size:11px;letter-spacing:2px;padding:3px 8px;display:inline-block;margin-bottom:6px}
.ml-front{background:rgba(0,229,255,.15);color:var(--cyan);border:1px solid var(--cyan)}
.ml-rear{background:rgba(255,109,0,.15);color:var(--orange);border:1px solid var(--orange)}
.ml-none{color:var(--dim);font-size:11px}
.alert-box{border:2px solid var(--red);background:rgba(255,23,68,.1);
  padding:8px 10px;animation:pulse 1s infinite alternate}
@keyframes pulse{from{box-shadow:0 0 4px var(--red)}to{box-shadow:0 0 16px var(--red)}}
.alert-title{font-size:15px;font-weight:700;color:var(--red);letter-spacing:2px}
.alert-detail{font-size:11px;color:#ffb3b3;margin-top:3px}
.vbar-wrap{background:#0d1e2e;border:1px solid var(--border);
  border-radius:2px;height:10px;margin:6px 0;overflow:hidden}
.vbar-fill{height:100%;transition:width .4s linear;border-radius:2px}
.gps-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.gk{font-size:10px;color:var(--dim);letter-spacing:1px}
.gv{font-size:18px;font-weight:700;color:var(--green)}
#map{height:220px;border:1px solid var(--border);margin-top:8px;display:none}
#maplink{font-size:11px;color:var(--cyan);margin-top:4px;display:none;text-decoration:none}
#crash-modal{display:none;position:fixed;inset:0;z-index:9999;
  background:rgba(0,0,0,.75);align-items:center;justify-content:center}
.modal-box{background:#0d0608;border:2px solid var(--red);max-width:460px;
  width:90%;padding:28px 30px;box-shadow:0 0 40px rgba(255,23,68,.5)}
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>

<div class="header">
  <div class="logo">PI<span>CAM</span> <span style="font-size:12px;color:var(--dim)">REAL-TIME</span></div>
  <div class="hright">
    <div><span class="dot" id="dot-webrtc"></span>WebRTC</div>
    <div><span class="dot" id="dot-pi"></span>Pi</div>
    <div><span class="dot" id="dot-ws"></span>WS</div>
    <div style="color:var(--cyan)" id="hdr-time">--:--:--</div>
    <div>👤 {{ username }}</div>
    <a class="logout" href="/logout">OUT</a>
  </div>
</div>

<div class="stream-box">
  <video id="videoEl" autoplay playsinline muted></video>
  <img   id="fallbackImg" alt="Connecting...">
  <span class="stream-badge badge-webrtc"   id="badge-webrtc"   style="display:none">● WEBRTC LIVE</span>
  <span class="stream-badge badge-fallback" id="badge-fallback" style="display:none">● SSE FALLBACK</span>
  <span id="latency-meter">⏱️ --ms</span>
</div>
<div id="conn-msg">Starting up...</div>

<div class="grid">

  <div class="panel">
    <div class="panel-title">◉ Status</div>
    <div class="stat-row"><span class="sk">FRONT</span><span class="sv" id="stat-front">—</span></div>
    <div class="stat-row"><span class="sk">REAR</span> <span class="sv" id="stat-rear">—</span></div>
    <div class="stat-row"><span class="sk">STREAM</span><span class="sv" id="stat-stream">—</span></div>
    <div class="stat-row"><span class="sk">LATENCY</span><span class="sv" id="stat-latency">—</span></div>
    <div style="margin-top:12px">
      <button class="btn" id="btn-ml" onclick="toggleML()">▶ Enable ML Detection</button>
      <button class="btn" onclick="window.open('/snapshot.jpg','_blank')">📷 Snapshot</button>
      <button class="btn" onclick="reconnectWebRTC()">↻ Reconnect</button>
    </div>
  </div>

  <div class="panel" style="grid-column:span 2">
    <div class="panel-title">⚠ Accident Detection</div>
    <div id="ml-panel"><div class="ml-none">ML Detection: OFF</div></div>
  </div>

  <div class="panel" style="grid-column:span 2">
    <div class="panel-title">◎ GPS</div>
    <div class="gps-grid">
      <div><div class="gk">LATITUDE</div> <div class="gv" id="glat">—</div></div>
      <div><div class="gk">LONGITUDE</div><div class="gv" id="glon">—</div></div>
      <div><div class="gk">SPEED</div>    <div class="gv" id="gspd">—</div></div>
      <div><div class="gk">UPDATED</div>  <div class="gv" id="gupdated" style="font-size:12px;color:var(--dim)">—</div></div>
    </div>
    <div id="map"></div>
    <a id="maplink" href="#" target="_blank">📌 Google Maps</a>
  </div>

  <div class="panel">
    <div class="panel-title">ℹ Connection</div>
    <div style="font-size:11px;color:var(--dim);line-height:2">
      WebRTC + WS: <span style="color:var(--green)">~30–80ms</span><br>
      WebRTC + HTTP: <span style="color:var(--yellow)">~100–200ms</span><br>
      SSE fallback: <span style="color:var(--orange)">~300–600ms</span><br><br>
      <span style="color:var(--cyan)">Using WebSocket for<br>instant signaling!</span>
    </div>
  </div>

</div>

<div id="crash-modal">
  <div class="modal-box">
    <div style="font-size:26px;font-weight:700;color:var(--red);letter-spacing:4px;margin-bottom:6px">🚨 CRASH CONFIRMED</div>
    <div style="font-size:13px;color:#ffb3b3;margin-bottom:18px" id="modal-detail">—</div>
    <div style="font-size:11px;color:var(--dim);margin-bottom:18px">
      Held ≥ <span style="color:var(--yellow)" id="modal-secs">3</span>s — real event.
    </div>
    <div style="display:flex;gap:10px">
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
// ═══════════════════════════════════════════════════════
//  Real-Time WebRTC Client with WebSocket Signaling
// ═══════════════════════════════════════════════════════

const ICE = { 
  iceServers: [
    { urls: 'stun:stun.l.google.com:19302' },
    { urls: 'stun:stun1.l.google.com:19302' },
    { urls: 'stun:stun2.l.google.com:19302' },
    { urls: 'stun:stun3.l.google.com:19302' },
    { urls: 'stun:stun4.l.google.com:19302' },
    { urls: 'stun:stun.cloudflare.com:3478' }
  ],
  iceCandidatePoolSize: 10,
  iceTransportPolicy: 'all'
};

let pc = null, webRTCActive = false;
let ws = null, fallbackSSE = null;
let latencyInterval = null;
let lastFrameTime = 0;

function msg(text, color) {
  const el = document.getElementById('conn-msg');
  el.innerText = text; el.style.color = color || 'var(--dim)';
}
function streamStat(txt, cls) {
  const el = document.getElementById('stat-stream');
  el.innerText = txt; el.className = 'sv' + (cls ? ' '+cls : '');
}
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// WebSocket Signaling
function connectWebSocket() {
  document.getElementById('dot-ws').className = 'dot warn';
  
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${window.location.host}/ws/viewer`);
  
  ws.onopen = () => {
    document.getElementById('dot-ws').className = 'dot live';
    msg('WebSocket connected, initiating WebRTC...', 'var(--green)');
    startWebRTCWithWS();
  };
  
  ws.onmessage = async (event) => {
    const data = JSON.parse(event.data);
    
    if (data.type === 'offer') {
      // Handle offer from Pi
      await handleOffer(data.sdp);
    } else if (data.type === 'ice') {
      // Handle ICE candidate from Pi
      if (pc) {
        try {
          await pc.addIceCandidate(new RTCIceCandidate(data.candidate));
        } catch (e) {
          console.error('Error adding ICE candidate:', e);
        }
      }
    } else if (data.type === 'request_offer') {
      msg('Pi available, requesting stream...', 'var(--yellow)');
      // Pi will send offer soon
    }
  };
  
  ws.onclose = () => {
    document.getElementById('dot-ws').className = 'dot err';
    msg('WebSocket disconnected, falling back to HTTP...', 'var(--orange)');
    startWebRTCWithHTTP();
  };
  
  ws.onerror = () => {
    document.getElementById('dot-ws').className = 'dot err';
  };
}

async function handleOffer(sdp) {
  try {
    pc = new RTCPeerConnection(ICE);
    
    pc.ontrack = (ev) => {
      if (!ev.streams || !ev.streams[0]) return;
      const v = document.getElementById('videoEl');
      v.srcObject = ev.streams[0];
      v.style.display = 'block';
      document.getElementById('fallbackImg').style.display = 'none';
      document.getElementById('badge-webrtc').style.display = 'block';
      document.getElementById('badge-fallback').style.display = 'none';
      document.getElementById('dot-webrtc').className = 'dot live';
      document.getElementById('dot-pi').className = 'dot live';
      document.getElementById('latency-meter').style.display = 'block';
      webRTCActive = true;
      msg('⚡ WebRTC live — real-time!', 'var(--green)');
      streamStat('WebRTC ⚡', 'ok');
      stopFallbackSSE();
      
      // Start latency measurement
      startLatencyMeasurement();
    };
    
    pc.onicecandidate = (ev) => {
      if (!ev.candidate) return;
      // Send ICE candidate via WebSocket
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'ice',
          candidate: ev.candidate
        }));
      } else {
        // Fallback to HTTP
        fetch('/signal/ice', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({candidate: ev.candidate})
        }).catch(() => {});
      }
    };
    
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'connected') {
        document.getElementById('dot-webrtc').className = 'dot live';
      } else if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
        webRTCActive = false;
        document.getElementById('dot-webrtc').className = 'dot err';
        msg('WebRTC dropped — reconnecting...', 'var(--red)');
        startFallbackSSE();
        setTimeout(reconnectWebRTC, 2000);
      }
    };
    
    pc.oniceconnectionstatechange = () => {
      if (pc.iceConnectionState === 'connected') {
        console.log('ICE connected - P2P established');
      }
    };
    
    await pc.setRemoteDescription(new RTCSessionDescription(sdp));
    const answer = await pc.createAnswer();
    await pc.setLocalDescription(answer);
    
    // Send answer via WebSocket
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'answer',
        sdp: answer
      }));
    } else {
      // Fallback to HTTP
      await fetch('/signal/answer', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(answer)
      });
    }
    
  } catch (err) {
    console.error('WebRTC error:', err);
    msg('WebRTC setup failed — falling back to SSE', 'var(--orange)');
    startFallbackSSE();
  }
}

function startWebRTCWithWS() {
  // WebSocket will handle everything
  msg('Waiting for Pi offer via WebSocket...', 'var(--yellow)');
}

function startWebRTCWithHTTP() {
  // Fallback to HTTP polling
  msg('Using HTTP signaling (slower)...', 'var(--orange)');
  pollForOffer();
}

async function pollForOffer() {
  for (let i = 0; i < 15; i++) {
    try {
      const res = await fetch('/signal/offer');
      if (res.status === 200) {
        const offerData = await res.json();
        if (offerData) {
          await handleOffer(offerData);
          return;
        }
      }
    } catch(e) {}
    await sleep(2000);
  }
  
  msg('No WebRTC offer — using SSE fallback', 'var(--orange)');
  startFallbackSSE();
}

function startLatencyMeasurement() {
  if (latencyInterval) clearInterval(latencyInterval);
  
  // Use video element's timing for latency estimation
  const video = document.getElementById('videoEl');
  let lastRenderTime = performance.now();
  let frames = 0;
  
  video.addEventListener('timeupdate', () => {
    const now = performance.now();
    const latency = now - lastRenderTime;
    lastRenderTime = now;
    
    if (frames % 10 === 0) { // Update every 10 frames
      document.getElementById('latency-meter').innerHTML = `⏱️ ${Math.round(latency)}ms`;
      document.getElementById('stat-latency').innerText = `${Math.round(latency)}ms`;
      document.getElementById('stat-latency').className = 
        latency < 50 ? 'sv ok' : (latency < 150 ? '

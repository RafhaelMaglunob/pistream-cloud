#!/usr/bin/env python3
from flask import Flask, Response, jsonify, request, render_template_string, session, redirect, url_for
from functools import wraps
import threading, time, logging, os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey123")
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# ── Auth Config ───────────────────────────────────────────────
USERS = {
    "admin":  os.environ.get("ADMIN_PASSWORD", "admin123"),
    "viewer": os.environ.get("VIEWER_PASSWORD", "viewer123"),
}

# ── Shared state ──────────────────────────────────────────────
latest_frame = None
# ml_results now stores the dict sent by Pi: {"0": {...}, "1": {...}}
ml_results   = {}
# cam_status stores dict: {"0": "Streaming!", "1": "Streaming!"}
cam_status   = {"0": "Waiting for Pi...", "1": "Waiting for Pi..."}
gps_data     = {"lat": None, "lon": None, "speed": None, "updated": None}

frame_lock  = threading.Lock()
ml_lock     = threading.Lock()
status_lock = threading.Lock()
gps_lock    = threading.Lock()
frame_event = threading.Event()

PUSH_SECRET = os.environ.get("PUSH_SECRET", "changeme123")

# ── Auth decorator ────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── Login / Logout ────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = ''
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username in USERS and USERS[username] == password:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        error = 'Wrong username or password'

    html = """<!DOCTYPE html>
<html>
<head>
<title>Login — Pi Camera</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #000; color: #0f0; font-family: monospace;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .box { border: 2px solid #0f0; padding: 40px; width: 320px; text-align: center; }
  h1 { font-size: 20px; margin-bottom: 24px; }
  input { width: 100%; background: #111; border: 1px solid #0f0; color: #0f0;
          padding: 10px; margin: 8px 0; font-family: monospace; font-size: 14px; }
  button { width: 100%; background: #0f0; color: #000; border: none;
           padding: 12px; margin-top: 16px; cursor: pointer; font-family: monospace;
           font-size: 15px; font-weight: bold; }
  button:hover { background: #0c0; }
  .error { color: #f44; margin-top: 12px; font-size: 13px; }
  .lock { font-size: 40px; margin-bottom: 16px; }
</style>
</head>
<body>
<div class="box">
  <div class="lock">🔒</div>
  <h1>Pi Camera Access</h1>
  <form method="POST">
    <input type="text" name="username" placeholder="Username" required autofocus>
    <input type="password" name="password" placeholder="Password" required>
    <button type="submit">LOGIN</button>
  </form>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
</div>
</body>
</html>"""
    return render_template_string(html, error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Pi push endpoints ─────────────────────────────────────────
def check_secret():
    return request.headers.get('X-Secret') == PUSH_SECRET

@app.route('/push/frame', methods=['POST'])
def push_frame():
    if not check_secret(): return 'Unauthorized', 401
    global latest_frame
    data = request.get_data()
    if len(data) < 1024: return 'Too small', 400
    with frame_lock:
        latest_frame = data
    frame_event.set()
    return 'OK', 200

@app.route('/push/ml', methods=['POST'])
def push_ml():
    """
    Pi sends: {"0": {"confirmed": bool, "elapsed": float, "boxes": [...]},
               "1": {"confirmed": bool, "elapsed": float, "boxes": [...]}}
    We store as-is and serve to the UI.
    """
    if not check_secret(): return 'Unauthorized', 401
    global ml_results
    with ml_lock:
        ml_results = request.get_json() or {}
    return 'OK', 200

@app.route('/push/status', methods=['POST'])
def push_status():
    """
    Pi sends: {"0": "Streaming!", "1": "Streaming!"}
    """
    if not check_secret(): return 'Unauthorized', 401
    global cam_status
    data = request.get_json() or {}
    with status_lock:
        cam_status = data  # store the whole dict
    return 'OK', 200

@app.route('/push/gps', methods=['POST'])
def push_gps():
    if not check_secret(): return 'Unauthorized', 401
    global gps_data
    data = request.get_json() or {}
    with gps_lock:
        gps_data = {
            "lat":     data.get("lat"),
            "lon":     data.get("lon"),
            "speed":   data.get("speed"),
            "updated": time.strftime("%H:%M:%S")
        }
    return 'OK', 200

# ── Keep-alive ping ───────────────────────────────────────────
@app.route('/ping')
def ping():
    return 'pong', 200

# ── Browser endpoints (login required) ───────────────────────
@app.route('/snapshot.jpg')
@login_required
def snapshot():
    with frame_lock:
        frame = latest_frame
    if frame is None:
        return 'No frame yet', 503
    return Response(frame, mimetype='image/jpeg',
                    headers={'Cache-Control': 'no-cache, no-store'})

@app.route('/ml_results')
@login_required
def get_ml():
    with ml_lock:
        return jsonify(ml_results)

@app.route('/status')
@login_required
def get_status():
    with status_lock:
        return jsonify(cam_status)

@app.route('/gps')
@login_required
def get_gps():
    with gps_lock:
        return jsonify(gps_data)

# ── Main Web UI ───────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    html = r"""<!DOCTYPE html>
<html>
<head>
<title>Pi Camera Cloud</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #050a0e; color: #c8d8e8; font-family: 'Share Tech Mono', monospace; }

  /* ── HEADER ── */
  .header { display:flex; align-items:center; justify-content:space-between;
    padding: 12px 20px; background:#0a1520; border-bottom:1px solid #1a2e40; }
  .logo { font-size:20px; font-weight:700; letter-spacing:3px; color:#00e5ff; }
  .logo span { color:#ff6d00; }
  .hright { display:flex; gap:16px; align-items:center; font-size:11px; }
  .dot { width:8px; height:8px; border-radius:50%; background:#4a6070;
         display:inline-block; margin-right:4px; }
  .dot.live { background:#00e676; box-shadow:0 0 6px #00e676; }
  .dot.warn { background:#ff6d00; }
  .logout-btn { color:#ff4444; text-decoration:none; font-size:11px;
                border:1px solid #ff4444; padding:3px 10px; }

  /* ── STREAM ── */
  .stream-box { background:#000; display:flex; justify-content:center;
    border-bottom:2px solid #1a2e40; }
  #snapshot { display:block; width:100%; max-width:1280px; min-height:200px;
              object-fit:contain; }
  #piconn { text-align:center; font-size:12px; padding:4px;
            min-height:22px; background:#0a1520; }
  .online  { color:#00e676; }
  .offline { color:#ff1744; }

  /* ── GRID ── */
  .grid { display:grid; grid-template-columns:1fr 1fr 1fr;
          gap:12px; padding:14px 20px; }
  @media(max-width:900px){ .grid { grid-template-columns:1fr 1fr; } }
  @media(max-width:580px){ .grid { grid-template-columns:1fr; } }
  .panel { background:#0a1520; border:1px solid #1a2e40; padding:14px; }
  .panel-title { font-size:11px; letter-spacing:2px; color:#4a6070;
    text-transform:uppercase; border-bottom:1px solid #1a2e40;
    padding-bottom:6px; margin-bottom:10px; }

  /* ── BUTTONS ── */
  .btn { background:transparent; color:#c8d8e8; border:1px solid #1a2e40;
         padding:9px 14px; cursor:pointer; font-family:monospace; font-size:13px;
         width:100%; margin-bottom:6px; text-align:left; transition:all .2s; }
  .btn:hover { border-color:#00e5ff; color:#00e5ff; }
  .btn.active { background:rgba(0,229,255,.15); color:#00e5ff;
                border-color:#00e5ff; box-shadow:0 0 8px rgba(0,229,255,.3); }

  /* ── STATUS ── */
  .stat-row { display:flex; justify-content:space-between;
              font-size:11px; padding:4px 0; border-bottom:1px solid #0d1e2e; }
  .sk { color:#4a6070; }
  .sv { color:#c8d8e8; max-width:55%; text-align:right;
        overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .sv.ok  { color:#00e676; }
  .sv.err { color:#ff1744; }

  /* ── ML PANEL ── */
  .ml-cam { margin-bottom:10px; }
  .ml-cam-hdr { font-size:11px; letter-spacing:2px; padding:3px 8px;
                display:inline-block; margin-bottom:6px; }
  .front-hdr { background:rgba(0,229,255,.15); color:#00e5ff; border:1px solid #00e5ff; }
  .rear-hdr  { background:rgba(255,109,0,.15);  color:#ff6d00; border:1px solid #ff6d00; }
  .ml-none { color:#4a6070; font-size:11px; }

  .alert-box { border:2px solid #ff1744; background:rgba(255,23,68,.1);
    padding:8px 10px; animation:pulse 1s infinite alternate; }
  @keyframes pulse { from{box-shadow:0 0 4px #ff1744} to{box-shadow:0 0 16px #ff1744} }
  .alert-title { font-size:15px; font-weight:700; color:#ff1744; letter-spacing:2px; }
  .alert-detail { font-size:11px; color:#ffb3b3; margin-top:3px; }

  .verify-bar-wrap { background:#0d1e2e; border:1px solid #1a2e40;
    border-radius:2px; height:10px; margin:6px 0; overflow:hidden; }
  .verify-bar-fill { height:100%; transition:width .4s linear; border-radius:2px; }

  /* ── GPS ── */
  .gps-grid { display:grid; grid-template-columns:1fr 1fr; gap:6px; }
  .gk { font-size:10px; color:#4a6070; letter-spacing:1px; }
  .gv { font-size:18px; font-weight:700; color:#00e676; }
  #map { height:220px; border:1px solid #1a2e40; margin-top:8px; }
  #maplink { font-size:11px; color:#00e5ff; margin-top:4px;
             display:none; text-decoration:none; }

  /* ── CRASH MODAL ── */
  #crash-modal { display:none; position:fixed; inset:0; z-index:9999;
    background:rgba(0,0,0,.75); align-items:center; justify-content:center; }
  .modal-box { background:#0d0608; border:2px solid #ff1744; max-width:460px;
    width:90%; padding:28px 30px; box-shadow:0 0 40px rgba(255,23,68,.5); }
  .modal-title { font-size:26px; font-weight:700; color:#ff1744;
    letter-spacing:4px; margin-bottom:6px; }
  .modal-detail { font-size:13px; color:#ffb3b3; margin-bottom:18px; }
  .modal-note { font-size:11px; color:#4a6070; margin-bottom:18px; }
  .modal-btns { display:flex; gap:10px; }
  .mbtn { flex:1; padding:10px; font-family:monospace; font-size:14px;
          letter-spacing:2px; cursor:pointer; }
  .mbtn-dismiss { border:1px solid #4a6070; background:transparent; color:#4a6070; }
  .mbtn-report  { flex:2; border:1px solid #ff1744;
                  background:rgba(255,23,68,.15); color:#ff1744; }
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>

<div class="header">
  <div class="logo">PI<span>CAM</span> <span style="font-size:13px;color:#4a6070">CLOUD</span></div>
  <div class="hright">
    <div><span class="dot" id="dot-pi"></span>PI</div>
    <div style="color:#00e5ff" id="hdr-time">--:--:--</div>
    <div>👤 {{ username }}</div>
    <a class="logout-btn" href="/logout">LOGOUT</a>
  </div>
</div>

<div class="stream-box">
  <img id="snapshot" src="/snapshot.jpg" alt="Waiting for Pi...">
</div>
<div id="piconn" class="offline">⬤ Pi: Connecting...</div>

<div class="grid">

  <!-- CAMERA STATUS -->
  <div class="panel">
    <div class="panel-title">◉ Camera Status</div>
    <div class="stat-row"><span class="sk">FRONT</span><span class="sv" id="stat-front">—</span></div>
    <div class="stat-row"><span class="sk">REAR</span> <span class="sv" id="stat-rear">—</span></div>
    <div style="margin-top:12px">
      <button class="btn" id="btn-ml" onclick="toggleML()">▶ Enable ML Detection</button>
      <button class="btn" onclick="window.open('/snapshot.jpg','_blank')">📷 Full Snapshot</button>
    </div>
    <div style="font-size:10px;color:#4a6070;margin-top:8px;line-height:1.6">
      Crash confirm: <span style="color:#ffd600">3s hold</span><br>
      Cooldown: <span style="color:#ffd600">10s after alert</span>
    </div>
  </div>

  <!-- ML / CRASH PANEL -->
  <div class="panel" style="grid-column:span 2">
    <div class="panel-title">⚠ Accident Detection</div>
    <div id="ml-panel">
      <div class="ml-none">ML Detection: OFF — click Enable to begin monitoring</div>
    </div>
  </div>

  <!-- GPS -->
  <div class="panel" style="grid-column:span 2">
    <div class="panel-title">◎ GPS Location</div>
    <div class="gps-grid">
      <div><div class="gk">LATITUDE</div> <div class="gv" id="glat">—</div></div>
      <div><div class="gk">LONGITUDE</div><div class="gv" id="glon">—</div></div>
      <div><div class="gk">SPEED</div>    <div class="gv" id="gspd">—</div></div>
      <div><div class="gk">UPDATED</div>  <div class="gv" id="gupdated" style="font-size:13px;color:#4a6070">—</div></div>
    </div>
    <div id="map"></div>
    <a id="maplink" href="#" target="_blank">📌 Open in Google Maps</a>
  </div>

  <!-- ABOUT -->
  <div class="panel">
    <div class="panel-title">ℹ Info</div>
    <div style="font-size:11px;color:#4a6070;line-height:1.8">
      Stream is polled at ~1fps<br>from Render free tier.<br><br>
      For live stream, connect<br>to Pi directly via ngrok.<br><br>
      Crash alerts push here<br>via /push/ml endpoint.
    </div>
  </div>

</div>

<!-- CRASH MODAL -->
<div id="crash-modal">
  <div class="modal-box">
    <div class="modal-title">🚨 CRASH CONFIRMED</div>
    <div class="modal-detail" id="modal-detail">—</div>
    <div class="modal-note">
      Detection persisted for ≥ <span style="color:#ffd600" id="modal-secs">3</span>s
      — classified as a <strong style="color:#ff1744">real event</strong>.
    </div>
    <div class="modal-btns">
      <button class="mbtn mbtn-dismiss" onclick="dismissModal()">DISMISS</button>
      <button class="mbtn mbtn-report"  onclick="dismissModal(true)">ACKNOWLEDGE &amp; REPORT</button>
    </div>
  </div>
</div>

<script>
/* ── STATE ── */
let mlOn = false, mlInterval = null, failCount = 0;
let map = null, marker = null;
let modalShownFor = {0: false, 1: false};

/* ── CLOCK ── */
setInterval(() => {
  document.getElementById('hdr-time').innerText = new Date().toTimeString().slice(0,8);
}, 1000);

/* ── SNAPSHOT POLLING ~1fps ── */
const img = document.getElementById('snapshot');
setInterval(() => {
  const tmp = new Image();
  tmp.onload = () => {
    img.src = tmp.src;
    failCount = 0;
    document.getElementById('piconn').className = 'online';
    document.getElementById('piconn').innerText = '⬤ Pi: Connected';
    document.getElementById('dot-pi').className = 'dot live';
  };
  tmp.onerror = () => {
    if (++failCount > 4) {
      document.getElementById('piconn').className = 'offline';
      document.getElementById('piconn').innerText = '⬤ Pi: Disconnected — waiting for frame push';
      document.getElementById('dot-pi').className = 'dot warn';
    }
  };
  tmp.src = '/snapshot.jpg?t=' + Date.now();
}, 1100);

/* ── ML TOGGLE ── */
function toggleML() {
  mlOn = !mlOn;
  const btn = document.getElementById('btn-ml');
  btn.className = 'btn' + (mlOn ? ' active' : '');
  btn.innerText = (mlOn ? '⏹ Disable' : '▶ Enable') + ' ML Detection';
  if (mlInterval) clearInterval(mlInterval);
  if (mlOn) {
    mlInterval = setInterval(updateML, 1000);
    updateML();
  } else {
    document.getElementById('ml-panel').innerHTML =
      '<div class="ml-none">ML Detection: OFF</div>';
    modalShownFor = {0: false, 1: false};
  }
}

/* ── ML UPDATE ──
   Pi pushes: {"0": {confirmed, elapsed, boxes, confirm_secs}, "1": {...}}
*/
function updateML() {
  fetch('/ml_results').then(r => r.json()).then(data => {
    let html = '';
    [{key:'0', label:'FRONT', hdrCls:'front-hdr'}, {key:'1', label:'REAR', hdrCls:'rear-hdr'}]
    .forEach(({key, label, hdrCls}) => {
      const d   = data[key];
      const idx = parseInt(key);
      html += `<div class="ml-cam">
        <span class="ml-cam-hdr ${hdrCls}">${label} CAM</span>`;

      if (!d || (!d.first_seen && !d.confirmed)) {
        html += '<div class="ml-none" style="margin:4px 0 8px">No detections</div>';
      } else if (d.confirmed) {
        const best = d.boxes && d.boxes.length
          ? d.boxes.reduce((a,b) => a.conf > b.conf ? a : b)
          : null;
        html += `<div class="alert-box">
          <div class="alert-title">🚨 CRASH CONFIRMED</div>
          <div class="alert-detail">${best
            ? `Side: ${best.side} &nbsp;|&nbsp; Conf: ${(best.conf*100).toFixed(1)}%`
            : ''}</div>
        </div>`;
        if (!modalShownFor[idx] && best) {
          modalShownFor[idx] = true;
          showModal(label, d.boxes, d.elapsed);
        }
      } else if (d.first_seen) {
        const secs = d.confirm_secs || 3;
        const pct  = Math.min(1, d.elapsed / secs);
        const r    = Math.round(255 * pct);
        const g    = Math.round(255 * (1 - pct));
        html += `<div style="font-size:11px;color:#ffd600;margin:4px 0">
          ⏱ VERIFYING — ${d.elapsed.toFixed(1)}s / ${secs}s</div>
          <div class="verify-bar-wrap">
            <div class="verify-bar-fill"
              style="width:${(pct*100).toFixed(1)}%;background:rgb(${r},${g},0)"></div>
          </div>`;
        if (d.boxes && d.boxes.length) {
          const best = d.boxes.reduce((a,b) => a.conf > b.conf ? a : b);
          html += `<div style="font-size:11px;color:#4a6070;margin-top:4px">
            ${best.label} · ${best.side} · ${(best.conf*100).toFixed(1)}%</div>`;
        }
        modalShownFor[idx] = false;
      } else {
        modalShownFor[idx] = false;
      }
      html += '</div>';
    });
    document.getElementById('ml-panel').innerHTML = html;
  }).catch(() => {});
}

/* ── CRASH MODAL ── */
function showModal(camLabel, boxes, elapsed) {
  const best = boxes.reduce((a,b) => a.conf > b.conf ? a : b);
  document.getElementById('modal-detail').innerText =
    `Camera: ${camLabel}  |  Side: ${best.side}  |  Conf: ${(best.conf*100).toFixed(1)}%`;
  document.getElementById('modal-secs').innerText = elapsed.toFixed(1);
  document.getElementById('crash-modal').style.display = 'flex';
  try {
    const ac = new AudioContext();
    const osc = ac.createOscillator();
    osc.connect(ac.destination); osc.frequency.value = 880;
    osc.start(); setTimeout(() => osc.stop(), 300);
  } catch(e) {}
}
function dismissModal(report) {
  document.getElementById('crash-modal').style.display = 'none';
  if (report) console.log('Crash acknowledged by operator');
}

/* ── CAMERA STATUS ──
   Pi sends {"0": "Streaming!", "1": "Streaming!"}
*/
function updateStatus() {
  fetch('/status').then(r => r.json()).then(d => {
    function setEl(id, txt) {
      const el = document.getElementById(id);
      el.innerText = txt;
      el.className = 'sv' + (
        txt.includes('Streaming') ? ' ok' :
        txt.includes('ERROR') || txt.includes('error') ? ' err' : '');
    }
    setEl('stat-front', d['0'] || '—');
    setEl('stat-rear',  d['1'] || '—');
  }).catch(() => {});
}
setInterval(updateStatus, 2000);
updateStatus();

/* ── GPS ── */
function updateGPS() {
  fetch('/gps').then(r => r.json()).then(d => {
    if (d.lat && d.lon) {
      document.getElementById('glat').innerText    = d.lat.toFixed(6) + '°';
      document.getElementById('glon').innerText    = d.lon.toFixed(6) + '°';
      document.getElementById('gspd').innerText    = d.speed != null ? d.speed.toFixed(1) + ' km/h' : '—';
      document.getElementById('gupdated').innerText = d.updated || '—';
      const ml = document.getElementById('maplink');
      ml.style.display = 'block';
      ml.href = `https://maps.google.com/?q=${d.lat},${d.lon}`;
      const mapEl = document.getElementById('map');
      mapEl.style.display = 'block';
      if (!map) {
        map = L.map('map').setView([d.lat, d.lon], 16);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
          {attribution:'© OSM'}).addTo(map);
        marker = L.marker([d.lat, d.lon]).addTo(map)
          .bindPopup('Pi Camera').openPopup();
      } else {
        marker.setLatLng([d.lat, d.lon]);
        map.setView([d.lat, d.lon]);
      }
    }
  }).catch(() => {});
}
setInterval(updateGPS, 4000);
updateGPS();
</script>
</body>
</html>"""
    return render_template_string(html, username=session.get('username', ''))

# ── Main ──────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

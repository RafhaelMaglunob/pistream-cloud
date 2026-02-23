#!/usr/bin/env python3
from flask import Flask, Response, jsonify, request, render_template_string, session, redirect, url_for
from functools import wraps
import threading, time, logging, os

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey123")
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# ── Auth Config ───────────────────────────────────────────────
# Users are set via Render environment variables
# ADMIN_PASSWORD and VIEWER_PASSWORD
USERS = {
    "admin":  os.environ.get("ADMIN_PASSWORD", "admin123"),
    "viewer": os.environ.get("VIEWER_PASSWORD", "viewer123"),
}

# ── Shared state ──────────────────────────────────────────────
latest_frame = None
ml_results   = []
cam_status   = "Waiting for Pi to connect..."
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
    if not check_secret(): return 'Unauthorized', 401
    global ml_results
    with ml_lock:
        ml_results = request.get_json() or []
    return 'OK', 200

@app.route('/push/status', methods=['POST'])
def push_status():
    if not check_secret(): return 'Unauthorized', 401
    global cam_status
    with status_lock:
        cam_status = request.get_json().get('status', '')
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

# ── Keep-alive ping (for UptimeRobot) ────────────────────────
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
        return jsonify({'ml_results': ml_results})

@app.route('/status')
@login_required
def get_status():
    with status_lock:
        return jsonify({'camera_status': cam_status})

@app.route('/gps')
@login_required
def get_gps():
    with gps_lock:
        return jsonify(gps_data)

# ── Main Web UI ───────────────────────────────────────────────
@app.route('/')
@login_required
def index():
    html = """<!DOCTYPE html>
<html>
<head>
<title>Pi Camera Cloud</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #000; color: #0f0; font-family: monospace; text-align: center; padding: 10px; }
  h1 { font-size: 18px; margin: 10px 0 4px; }
  #userbar { font-size: 11px; color: #555; margin-bottom: 10px; }
  #userbar a { color: #f44; text-decoration: none; }
  #snapshot { border: 2px solid #0f0; max-width: 100%; width: 640px; display: block; margin: 0 auto 10px; }
  #piconn { font-size: 11px; margin: 4px 0; }
  .online  { color: #0f0; }
  .offline { color: #f00; }
  .btn { background: #111; color: #0f0; border: 2px solid #0f0; padding: 10px 16px;
         margin: 4px; cursor: pointer; font-family: monospace; font-size: 13px; }
  .btn:hover, .btn.active { background: #0f0; color: #000; }
  #status    { margin-top: 10px; font-size: 13px; min-height: 22px; }
  #camstatus { font-size: 11px; color: #0aa; margin-top: 4px; }
  #gpsbox { border: 1px solid #0a0; background: #001a00; margin: 12px auto;
            width: 640px; max-width: 100%; padding: 12px; text-align: left; }
  #gpsbox h2 { font-size: 13px; color: #0f0; margin-bottom: 8px; }
  .grow { display: flex; justify-content: space-between; font-size: 12px; margin: 4px 0; }
  .glabel { color: #0a0; }
  .gval   { color: #fff; font-weight: bold; }
  #map { width: 640px; max-width: 100%; height: 300px; margin: 8px auto;
         border: 1px solid #0a0; }
  #maplink { font-size: 11px; color: #0af; margin: 6px auto; display: block; text-align: center; }
  #noloc { font-size: 12px; color: #555; padding: 6px 0; }
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>

<h1>🎥 Pi Camera + Accident Detection</h1>
<div id="userbar">
  Logged in as <b>{{ username }}</b> &nbsp;|&nbsp;
  <a href="/logout">Logout</a>
</div>

<img id="snapshot" src="/snapshot.jpg" alt="Waiting for Pi...">
<div id="piconn" class="offline">⬤ Pi: Connecting...</div>
<br>
<button class="btn" id="btnML" onclick="toggleML()">Toggle ML Detection</button>
<div id="status">Status: Connected to cloud</div>
<div id="camstatus">Camera: waiting...</div>

<div id="gpsbox">
  <h2>📍 GPS Location</h2>
  <div class="grow"><span class="glabel">Latitude:</span>  <span class="gval" id="glat">—</span></div>
  <div class="grow"><span class="glabel">Longitude:</span> <span class="gval" id="glon">—</span></div>
  <div class="grow"><span class="glabel">Speed:</span>     <span class="gval" id="gspeed">—</span></div>
  <div class="grow"><span class="glabel">Last update:</span><span class="gval" id="gupdated">—</span></div>
  <div id="noloc">Waiting for GPS data from Pi...</div>
</div>
<div id="map" style="display:none"></div>
<a id="maplink" href="#" target="_blank" style="display:none">📌 Open in Google Maps</a>

<script>
  let mlEnabled = false, mlInterval = null, failCount = 0, lastStatus = '';
  let map = null, marker = null;

  // Snapshot polling at 2fps
  const img = document.getElementById('snapshot');
  setInterval(() => {
    const tmp = new Image();
    tmp.onload = () => {
      img.src = tmp.src;
      failCount = 0;
      document.getElementById('piconn').className = 'online';
      document.getElementById('piconn').innerText = '⬤ Pi: Connected';
    };
    tmp.onerror = () => {
      if (++failCount > 5) {
        document.getElementById('piconn').className = 'offline';
        document.getElementById('piconn').innerText = '⬤ Pi: Disconnected';
      }
    };
    tmp.src = '/snapshot.jpg?t=' + Date.now();
  }, 1000); // 1fps — Render free tier friendly

  // ML toggle
  function toggleML() {
    mlEnabled = !mlEnabled;
    document.getElementById('btnML').classList.toggle('active', mlEnabled);
    document.getElementById('status').innerText = 'ML Detection: ' + (mlEnabled ? 'ON' : 'OFF');
    if (mlInterval) clearInterval(mlInterval);
    if (mlEnabled) mlInterval = setInterval(updateML, 1000);
  }
  function updateML() {
    fetch('/ml_results').then(r => r.json()).then(d => {
      let t = 'ML: ';
      if (!d.ml_results.length) t += 'No detections';
      else d.ml_results.forEach(m => {
        t += m.label + ' (' + m.side + ') — ' + (m.conf*100).toFixed(1) + '% | ';
      });
      t = t.replace(/ \\| $/, '');
      if (t !== lastStatus) { lastStatus = t; document.getElementById('status').innerText = t; }
    }).catch(() => { document.getElementById('status').innerText = 'ML: connection error'; });
  }

  // Camera status
  setInterval(() => {
    fetch('/status').then(r => r.json()).then(d => {
      document.getElementById('camstatus').innerText = 'Camera: ' + d.camera_status;
    }).catch(() => {});
  }, 3000);

  // GPS
  function updateGPS() {
    fetch('/gps').then(r => r.json()).then(d => {
      if (d.lat && d.lon) {
        document.getElementById('noloc').style.display = 'none';
        document.getElementById('glat').innerText     = d.lat.toFixed(6) + '°';
        document.getElementById('glon').innerText     = d.lon.toFixed(6) + '°';
        document.getElementById('gspeed').innerText   = d.speed != null ? d.speed.toFixed(1) + ' km/h' : '—';
        document.getElementById('gupdated').innerText = d.updated || '—';

        const link = document.getElementById('maplink');
        link.style.display = 'block';
        link.href = 'https://maps.google.com/?q=' + d.lat + ',' + d.lon;

        const mapEl = document.getElementById('map');
        mapEl.style.display = 'block';
        if (!map) {
          map = L.map('map').setView([d.lat, d.lon], 16);
          L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
            { attribution: '© OpenStreetMap' }).addTo(map);
          marker = L.marker([d.lat, d.lon]).addTo(map)
                    .bindPopup('Pi Camera Location').openPopup();
        } else {
          marker.setLatLng([d.lat, d.lon]);
          map.setView([d.lat, d.lon]);
        }
      }
    }).catch(() => {});
  }
  setInterval(updateGPS, 3000);
  updateGPS();
</script>
</body>
</html>"""
    return render_template_string(html, username=session.get('username', ''))

# ── Main ──────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

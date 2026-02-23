#!/usr/bin/env python3
from flask import Flask, Response, jsonify, request, render_template_string, session, redirect, url_for
import threading, time, logging, os, hashlib

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# ── Shared state ──────────────────────────────────────────────
latest_frame = None
ml_results   = []
cam_status   = "Waiting for Pi to connect..."
frame_lock   = threading.Lock()
ml_lock      = threading.Lock()
status_lock  = threading.Lock()
frame_event  = threading.Event()

# Set this in Render's environment variables dashboard
PUSH_SECRET = os.environ.get("PUSH_SECRET", "changeme123")

# ── Pi push endpoints ─────────────────────────────────────────
@app.route('/push/frame', methods=['POST'])
def push_frame():
    if request.headers.get('X-Secret') != PUSH_SECRET:
        return 'Unauthorized', 401
    global latest_frame
    data = request.get_data()
    if len(data) < 1024:
        return 'Too small', 400
    with frame_lock:
        latest_frame = data
    frame_event.set()
    return 'OK', 200

@app.route('/push/ml', methods=['POST'])
def push_ml():
    if request.headers.get('X-Secret') != PUSH_SECRET:
        return 'Unauthorized', 401
    global ml_results
    with ml_lock:
        ml_results = request.get_json() or []
    return 'OK', 200

@app.route('/push/status', methods=['POST'])
def push_status():
    if request.headers.get('X-Secret') != PUSH_SECRET:
        return 'Unauthorized', 401
    global cam_status
    with status_lock:
        cam_status = request.get_json().get('status', '')
    return 'OK', 200

# ── Keep-alive ping (for UptimeRobot) ────────────────────────
@app.route('/ping')
def ping():
    return 'pong', 200

# ── Snapshot endpoint (works perfectly on Render free tier) ──
@app.route('/snapshot.jpg')
def snapshot():
    with frame_lock:
        frame = latest_frame
    if frame is None:
        return 'No frame yet', 503
    return Response(frame, mimetype='image/jpeg',
                    headers={'Cache-Control': 'no-cache, no-store'})

# ── ML results ────────────────────────────────────────────────
@app.route('/ml_results')
def get_ml():
    with ml_lock:
        return jsonify({'ml_results': ml_results})

# ── Camera status ─────────────────────────────────────────────
@app.route('/status')
def get_status():
    with status_lock:
        return jsonify({'camera_status': cam_status})

# ── Web UI (snapshot polling — works on all free platforms) ───
@app.route('/')
def index():
    html = """<!DOCTYPE html>
<html>
<head>
<title>Pi Camera Cloud</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #000; color: #0f0; font-family: monospace; text-align: center; padding: 10px; }
  h1 { font-size: 18px; margin: 10px 0; }
  #snapshot { border: 2px solid #0f0; max-width: 100%; width: 640px; display: block; margin: 10px auto; }
  .btn { background: #111; color: #0f0; border: 2px solid #0f0; padding: 10px 16px; margin: 5px; cursor: pointer; font-family: monospace; font-size: 14px; }
  .btn:hover, .btn.active { background: #0f0; color: #000; }
  #status  { margin-top: 10px; font-size: 14px; min-height: 24px; }
  #camstatus { font-size: 11px; color: #0aa; margin-top: 6px; }
  #fps { font-size: 11px; color: #555; margin-top: 4px; }
  #piconn { font-size: 11px; margin-top: 4px; }
  .online  { color: #0f0; }
  .offline { color: #f00; }
</style>
</head>
<body>
<h1>🎥 Pi Camera + Accident Detection</h1>
<img id="snapshot" src="/snapshot.jpg" alt="Loading...">
<div id="piconn" class="offline">⬤ Pi: Connecting...</div>
<br>
<button class="btn" id="btnML" onclick="toggleML()">Toggle ML Detection</button>
<div id="status">Status: Connected to cloud</div>
<div id="camstatus">Camera: waiting...</div>
<div id="fps"></div>

<script>
  let mlEnabled = false;
  let mlInterval = null;
  let fpsInterval = null;
  let frameCount = 0;
  let lastStatus = '';
  let lastFrameTime = null;
  let snapshotInterval = null;
  let failCount = 0;

  const img = document.getElementById('snapshot');

  // ── Snapshot polling (replaces MJPEG stream) ──
  function refreshSnapshot() {
    const t = Date.now();
    const newImg = new Image();
    newImg.onload = function() {
      img.src = newImg.src;
      frameCount++;
      lastFrameTime = Date.now();
      failCount = 0;
      document.getElementById('piconn').className = 'online';
      document.getElementById('piconn').innerText = '⬤ Pi: Connected';
    };
    newImg.onerror = function() {
      failCount++;
      if (failCount > 5) {
        document.getElementById('piconn').className = 'offline';
        document.getElementById('piconn').innerText = '⬤ Pi: Disconnected';
      }
    };
    newImg.src = '/snapshot.jpg?t=' + t;
  }

  // Start snapshot polling at 2fps (adjustable)
  snapshotInterval = setInterval(refreshSnapshot, 500);
  refreshSnapshot();

  // ── FPS counter ──
  fpsInterval = setInterval(() => {
    document.getElementById('fps').innerText = 'Refresh rate: ' + frameCount*2 + ' frames/min';
    frameCount = 0;
  }, 30000);

  // ── ML toggle ──
  function toggleML() {
    mlEnabled = !mlEnabled;
    document.getElementById('btnML').classList.toggle('active', mlEnabled);
    document.getElementById('status').innerText = 'ML Detection: ' + (mlEnabled ? 'ON' : 'OFF');
    if (mlInterval) clearInterval(mlInterval);
    if (mlEnabled) mlInterval = setInterval(updateML, 1000);
    else { lastStatus = ''; }
  }

  function updateML() {
    fetch('/ml_results').then(r => r.json()).then(d => {
      let t = 'ML: ';
      if (!d.ml_results.length) { t += 'No detections'; }
      else d.ml_results.forEach(m => {
        t += m.label + ' (' + m.side + ') — ' + (m.conf * 100).toFixed(1) + '% | ';
      });
      t = t.replace(/ \| $/, '');
      if (t !== lastStatus) { lastStatus = t; document.getElementById('status').innerText = t; }
    }).catch(() => {
      document.getElementById('status').innerText = 'ML: connection error';
    });
  }

  // ── Camera status polling ──
  setInterval(() => {
    fetch('/status').then(r => r.json()).then(d => {
      document.getElementById('camstatus').innerText = 'Camera: ' + d.camera_status;
    }).catch(() => {});
  }, 3000);
</script>
</body>
</html>"""
    return render_template_string(html)

# ── Main ──────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
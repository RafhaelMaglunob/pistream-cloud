#!/usr/bin/env python3
"""
Pi Camera Relay Server
Deploy on Railway — WebSocket signaling + SSE fallback + Gmail crash alerts.
Video flows DIRECTLY Pi → Browser via WebRTC (~100-200ms).
Railway only handles the tiny SDP/ICE handshake + dashboard.
"""
from flask import Flask, Response, jsonify, request, render_template_string, session, redirect, url_for
from flask_sock import Sock
from functools import wraps
import threading, time, logging, os, json, queue, base64
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage

app  = Flask(__name__)
sock = Sock(app)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey123")
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# ── Users ─────────────────────────────────────────────────────
USERS = {
    "admin@gmail.com":  os.environ.get("ADMIN_PASSWORD",  "Admin@2351"),
    "rafhaelmaglunob02@gmail.com": os.environ.get("VIEWER_PASSWORD", "Rafhael@1"),
}
PUSH_SECRET = os.environ.get("PUSH_SECRET", "changeme123")

# ── Gmail config ──────────────────────────────────────────────
GMAIL_USER     = os.environ.get("GMAIL_USER",     "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")
ALERT_EMAILS   = [e.strip() for e in os.environ.get("ALERT_EMAILS", "").split(",") if e.strip()]

# ── Crash notification cooldown ───────────────────────────────
_crash_notified    = {0: False, 1: False}
_crash_notified_at = {0: 0.0,  1: 0.0}
_crash_notify_lock = threading.Lock()
CRASH_NOTIFY_COOLDOWN = 60  # seconds

# ── WebSocket connections ─────────────────────────────────────
signal_lock    = threading.Lock()
pi_ws          = None
viewer_ws_list = []

# ── HTTP fallback queues ──────────────────────────────────────
http_offers  = queue.Queue(maxsize=5)
http_answers = queue.Queue(maxsize=5)
http_pi_ice  = queue.Queue(maxsize=100)
http_br_ice  = queue.Queue(maxsize=100)

# ── Shared state ──────────────────────────────────────────────
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

# ═════════════════════════════════════════════════════════════
#  Gmail Alert
# ═════════════════════════════════════════════════════════════
def send_crash_email(cam_label, side, conf, elapsed, snapshot_bytes=None):
    if not GMAIL_USER or not GMAIL_PASSWORD or not ALERT_EMAILS:
        print("[EMAIL] Not configured — set GMAIL_USER, GMAIL_PASSWORD, ALERT_EMAILS in Railway")
        return

    def _send():
        with gps_lock:
            gps = gps_data.copy()

        maps_link = ""
        if gps.get('lat') and gps.get('lon'):
            maps_link = f"https://maps.google.com/?q={gps['lat']},{gps['lon']}"

        speed_row = ""
        if gps.get('speed') is not None:
            speed_row = f"""
            <tr style="background:#0a1018">
              <td style="color:#aaa;padding:8px 12px;width:140px">Speed</td>
              <td style="color:#fff;font-weight:bold;padding:8px 12px">{gps['speed']:.1f} km/h</td>
            </tr>"""

        location_btn = (
            f'<a href="{maps_link}" style="display:inline-block;background:#1a73e8;'
            f'color:#fff;padding:12px 24px;border-radius:4px;text-decoration:none;'
            f'font-weight:bold;margin:16px 0;font-size:14px">📍 View on Google Maps</a>'
        ) if maps_link else "<p style='color:#aaa'>GPS not available</p>"

        html_body = f"""
<html><body style="font-family:Arial,sans-serif;background:#0a0a0a;color:#eee;padding:20px;margin:0">
<div style="max-width:560px;margin:auto;background:#111;border:2px solid #ff1744;
            border-radius:8px;padding:28px">
  <h1 style="color:#ff1744;letter-spacing:3px;margin:0 0 4px;font-size:22px">
    🚨 CRASH CONFIRMED
  </h1>
  <p style="color:#666;font-size:12px;margin:0 0 20px">
    {time.strftime('%Y-%m-%d %H:%M:%S')} UTC
  </p>
  <table style="width:100%;border-collapse:collapse;background:#0d1520;border-radius:6px;margin-bottom:16px">
    <tr>
      <td style="color:#aaa;padding:8px 12px;width:140px">Camera</td>
      <td style="color:#fff;font-weight:bold;padding:8px 12px">{cam_label}</td>
    </tr>
    <tr style="background:#0a1018">
      <td style="color:#aaa;padding:8px 12px">Side of impact</td>
      <td style="color:#ff6d00;font-weight:bold;padding:8px 12px">{side}</td>
    </tr>
    <tr>
      <td style="color:#aaa;padding:8px 12px">Confidence</td>
      <td style="color:#00e676;font-weight:bold;padding:8px 12px">{conf:.1f}%</td>
    </tr>
    <tr style="background:#0a1018">
      <td style="color:#aaa;padding:8px 12px">Detection held</td>
      <td style="color:#fff;font-weight:bold;padding:8px 12px">{elapsed:.1f}s</td>
    </tr>
    {speed_row}
  </table>
  {location_btn}
  {"<p style='color:#aaa;font-size:12px;margin-top:8px'>📸 Snapshot attached.</p>" if snapshot_bytes else ""}
  <hr style="border:1px solid #222;margin:20px 0">
  <p style="color:#444;font-size:11px;margin:0">
    Pi Camera Crash Detection System<br>
    Dashboard: <a href="https://your-app.up.railway.app" style="color:#00e5ff">Railway Dashboard</a>
  </p>
</div></body></html>"""

        subject = f"🚨 CRASH — {cam_label} | {side} | {conf:.0f}%"

        for recipient in ALERT_EMAILS:
            try:
                msg = MIMEMultipart('related')
                msg['Subject'] = subject
                msg['From']    = f"Pi Camera <{GMAIL_USER}>"
                msg['To']      = recipient
                msg.attach(MIMEText(html_body, 'html'))
                if snapshot_bytes:
                    img = MIMEImage(snapshot_bytes, _subtype='jpeg')
                    img.add_header('Content-Disposition', 'attachment',
                                   filename='crash_snapshot.jpg')
                    msg.attach(img)
                with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as smtp:
                    smtp.login(GMAIL_USER, GMAIL_PASSWORD)
                    smtp.sendmail(GMAIL_USER, recipient, msg.as_string())
                print(f"[EMAIL] ✅ Sent to {recipient}")
            except Exception as e:
                print(f"[EMAIL] ❌ {recipient}: {e}")

    threading.Thread(target=_send, daemon=True).start()


def check_and_notify_crash(ml_data):
    now = time.time()
    with _crash_notify_lock:
        for key, label in [('0', 'FRONT'), ('1', 'REAR')]:
            idx = int(key)
            d   = ml_data.get(key, {})
            if not d or not d.get('confirmed'):
                _crash_notified[idx] = False
                continue
            if _crash_notified[idx]: continue
            if now - _crash_notified_at[idx] < CRASH_NOTIFY_COOLDOWN: continue
            _crash_notified[idx]    = True
            _crash_notified_at[idx] = now
            boxes   = d.get('boxes', [])
            best    = max(boxes, key=lambda b: b['conf']) if boxes else None
            side    = best['side']       if best else 'Unknown'
            conf    = best['conf'] * 100 if best else 0.0
            elapsed = d.get('elapsed', 0.0)
            with frame_lock: snap = latest_frame
            print(f"[EMAIL] 🚨 Crash on {label} — alerting {len(ALERT_EMAILS)} contact(s)")
            send_crash_email(label, side, conf, elapsed, snap)


# ═════════════════════════════════════════════════════════════
#  Auth
# ═════════════════════════════════════════════════════════════
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def check_secret():
    return (request.headers.get('X-Secret') == PUSH_SECRET or
            request.args.get('secret')       == PUSH_SECRET)

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
    html = """<!DOCTYPE html><html><head><title>Login — Pi Camera</title>
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


# ═════════════════════════════════════════════════════════════
#  WebSocket — Pi
# ═════════════════════════════════════════════════════════════
@sock.route('/ws/pi')
def ws_pi(ws):
    if not check_secret():
        ws.close(1008, "Unauthorized")
        return
    global pi_ws
    with signal_lock: pi_ws = ws
    print("[WS-PI] Connected")
    _broadcast_viewers({"type": "pi_connected"})
    try:
        while True:
            raw = ws.receive()
            if raw is None: break
            data = json.loads(raw)
            t = data.get('type')
            if t == 'offer':
                print("[WS-PI] Offer → viewers")
                _broadcast_viewers({"type": "offer", "sdp": data['sdp']})
                try: http_offers.put_nowait(data['sdp'])
                except queue.Full: pass
            elif t == 'ice':
                _broadcast_viewers({"type": "ice", "candidate": data['candidate']})
                try: http_pi_ice.put_nowait(data['candidate'])
                except queue.Full: pass
    except Exception as e:
        print(f"[WS-PI] Error: {e}")
    finally:
        with signal_lock:
            if pi_ws is ws: pi_ws = None
        _broadcast_viewers({"type": "pi_disconnected"})
        print("[WS-PI] Disconnected")


# ═════════════════════════════════════════════════════════════
#  WebSocket — Viewer
# ═════════════════════════════════════════════════════════════
@sock.route('/ws/viewer')
def ws_viewer(ws):
    if not session.get('logged_in'):
        ws.close(1008, "Login required")
        return
    with signal_lock: viewer_ws_list.append(ws)
    print(f"[WS-VIEWER] Connected ({len(viewer_ws_list)} total)")
    with signal_lock: pi_alive = pi_ws is not None
    if pi_alive:
        try: pi_ws.send(json.dumps({"type": "request_offer"}))
        except Exception: pass
    try:
        while True:
            raw = ws.receive()
            if raw is None: break
            data = json.loads(raw)
            t = data.get('type')
            if t == 'answer':
                _send_to_pi({"type": "answer", "sdp": data['sdp']})
                try: http_answers.put_nowait(data['sdp'])
                except queue.Full: pass
            elif t == 'ice':
                _send_to_pi({"type": "ice", "candidate": data['candidate']})
                try: http_br_ice.put_nowait(data['candidate'])
                except queue.Full: pass
            elif t == 'ping':
                ws.send(json.dumps({"type": "pong"}))
    except Exception as e:
        print(f"[WS-VIEWER] Error: {e}")
    finally:
        with signal_lock:
            if ws in viewer_ws_list: viewer_ws_list.remove(ws)
        print(f"[WS-VIEWER] Disconnected ({len(viewer_ws_list)} remaining)")

def _broadcast_viewers(msg):
    raw = json.dumps(msg)
    with signal_lock:
        dead = []
        for v in viewer_ws_list:
            try: v.send(raw)
            except Exception: dead.append(v)
        for v in dead:
            if v in viewer_ws_list: viewer_ws_list.remove(v)

def _send_to_pi(msg):
    with signal_lock: ws = pi_ws
    if ws:
        try: ws.send(json.dumps(msg))
        except Exception as e: print(f"[WS] Pi send failed: {e}")


# ═════════════════════════════════════════════════════════════
#  HTTP Fallback Signaling
# ═════════════════════════════════════════════════════════════
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
    try: return jsonify(http_offers.get(timeout=10))
    except queue.Empty: return jsonify(None), 204

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
    candidates = []
    try:
        while True: candidates.append(http_pi_ice.get_nowait())
    except queue.Empty: pass
    return jsonify(candidates)

@app.route('/poll/answer')
def http_poll_answer():
    if not check_secret(): return 'Unauthorized', 401
    try: return jsonify(http_answers.get(timeout=20))
    except queue.Empty: return jsonify(None), 204

@app.route('/poll/ice')
def http_poll_ice():
    if not check_secret(): return 'Unauthorized', 401
    candidates = []
    try:
        while True: candidates.append(http_br_ice.get_nowait())
    except queue.Empty: pass
    return jsonify(candidates)


# ═════════════════════════════════════════════════════════════
#  Pi Push Endpoints
# ═════════════════════════════════════════════════════════════
@app.route('/push/frame', methods=['POST'])
def push_frame():
    if not check_secret(): return 'Unauthorized', 401
    global latest_frame
    data = request.get_data()
    if len(data) < 1024: return 'Too small', 400
    with frame_lock: latest_frame = data
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
    data = request.get_json() or {}
    with ml_lock: ml_results = data
    check_and_notify_crash(data)   # ← fires Gmail alert if crash confirmed
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


# ═════════════════════════════════════════════════════════════
#  Browser Endpoints
# ═════════════════════════════════════════════════════════════
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
            if cur: yield f"data:{base64.b64encode(cur).decode('ascii')}\n\n"
            while True:
                try:    yield f"data:{q.get(timeout=30)}\n\n"
                except: yield ": keepalive\n\n"
        except GeneratorExit: pass
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

@app.route('/test/email')
@login_required
def test_email():
    if not GMAIL_USER:
        return "Not configured. Set GMAIL_USER, GMAIL_PASSWORD, ALERT_EMAILS in Railway.", 400
    send_crash_email("FRONT", "Center", 97.3, 3.2, None)
    return f"Test email sent to: {', '.join(ALERT_EMAILS) or 'nobody — set ALERT_EMAILS'}", 200


# ═════════════════════════════════════════════════════════════
#  Main Dashboard UI
# ═════════════════════════════════════════════════════════════
@app.route('/')
@login_required
def index():
    alert_status = f"{len(ALERT_EMAILS)} contact(s)" if ALERT_EMAILS else "Not configured"
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
.hright{display:flex;gap:12px;align-items:center;font-size:11px;flex-wrap:wrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);
  display:inline-block;margin-right:4px;transition:background .3s}
.dot.live{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.warn{background:var(--orange)}.dot.err{background:var(--red)}
a.logout{color:var(--red);text-decoration:none;border:1px solid var(--red);padding:3px 10px}
.stream-box{background:#000;display:flex;justify-content:center;
  position:relative;border-bottom:2px solid var(--border);min-height:200px}
#videoEl{display:none;width:100%;max-width:1280px;background:#000}
#fallbackImg{display:block;width:100%;max-width:1280px;object-fit:contain}
.badge{position:absolute;top:10px;left:14px;font-size:11px;
  letter-spacing:2px;padding:3px 10px;font-weight:700;display:none}
.badge-live{background:rgba(0,230,118,.2);color:var(--green);border:1px solid var(--green)}
.badge-sse{background:rgba(255,109,0,.2);color:var(--orange);border:1px solid var(--orange)}
#conn-msg{text-align:center;background:var(--panel);font-size:12px;
  padding:6px;min-height:24px;border-bottom:1px solid var(--border)}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;padding:14px 20px}
@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}}
@media(max-width:580px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);padding:14px}
.pt{font-size:11px;letter-spacing:2px;color:var(--dim);text-transform:uppercase;
  border-bottom:1px solid var(--border);padding-bottom:6px;margin-bottom:10px}
.sr{display:flex;justify-content:space-between;font-size:11px;
  padding:4px 0;border-bottom:1px solid #0d1e2e}
.sk{color:var(--dim)}.sv{color:var(--text);max-width:60%;text-align:right;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sv.ok{color:var(--green)}.sv.err{color:var(--red)}
.btn{background:transparent;color:var(--text);border:1px solid var(--border);
  padding:8px 12px;cursor:pointer;font-family:monospace;font-size:11px;
  width:100%;margin-bottom:5px;transition:all .2s;text-align:left}
.btn:hover{border-color:var(--cyan);color:var(--cyan)}
.btn.active{background:rgba(0,229,255,.15);color:var(--cyan);border-color:var(--cyan)}
.ml-cam{margin-bottom:10px}
.ml-hdr{font-size:11px;letter-spacing:2px;padding:3px 8px;display:inline-block;margin-bottom:6px}
.ml-f{background:rgba(0,229,255,.15);color:var(--cyan);border:1px solid var(--cyan)}
.ml-r{background:rgba(255,109,0,.15);color:var(--orange);border:1px solid var(--orange)}
.ml-none{color:var(--dim);font-size:11px}
.alert-box{border:2px solid var(--red);background:rgba(255,23,68,.1);
  padding:8px 10px;animation:pulse 1s infinite alternate}
@keyframes pulse{from{box-shadow:0 0 4px var(--red)}to{box-shadow:0 0 16px var(--red)}}
.vbar-wrap{background:#0d1e2e;height:8px;margin:6px 0;overflow:hidden;border-radius:2px}
.vbar-fill{height:100%;border-radius:2px;transition:width .4s}
.gps-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.gk{font-size:10px;color:var(--dim);letter-spacing:1px}
.gv{font-size:16px;font-weight:700;color:var(--green)}
#map{height:200px;border:1px solid var(--border);margin-top:8px;display:none}
#maplink{font-size:11px;color:var(--cyan);margin-top:4px;display:none;text-decoration:none}
#crash-modal{display:none;position:fixed;inset:0;z-index:9999;
  background:rgba(0,0,0,.85);align-items:center;justify-content:center}
.mbox{background:#0d0608;border:2px solid var(--red);max-width:440px;
  width:90%;padding:24px;box-shadow:0 0 40px rgba(255,23,68,.5)}
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>

<div class="header">
  <div class="logo">PI<span>CAM</span> <span style="font-size:11px;color:var(--dim)">LIVE</span></div>
  <div class="hright">
    <span><span class="dot" id="dot-ws"></span>WS</span>
    <span><span class="dot" id="dot-rtc"></span>RTC</span>
    <span><span class="dot" id="dot-pi"></span>Pi</span>
    <span style="color:var(--cyan)" id="clock">--:--:--</span>
    <span style="color:var(--dim)">{{ username }}</span>
    <a class="logout" href="/logout">OUT</a>
  </div>
</div>

<div class="stream-box">
  <video id="videoEl" autoplay playsinline muted></video>
  <img   id="fallbackImg" alt="">
  <span class="badge badge-live" id="badge-live">● WEBRTC</span>
  <span class="badge badge-sse"  id="badge-sse">● SSE</span>
</div>
<div id="conn-msg" style="color:var(--dim)">Connecting...</div>

<div class="grid">

  <!-- Status + Controls -->
  <div class="panel">
    <div class="pt">◉ Status</div>
    <div class="sr"><span class="sk">FRONT</span><span class="sv" id="s-f">—</span></div>
    <div class="sr"><span class="sk">REAR</span> <span class="sv" id="s-r">—</span></div>
    <div class="sr"><span class="sk">STREAM</span><span class="sv" id="s-s">—</span></div>
    <div class="sr"><span class="sk">SIGNAL</span><span class="sv" id="s-sig">—</span></div>
    <div class="sr"><span class="sk">ALERTS</span>
      <span class="sv ok">{{ alert_status }}</span></div>
    <div style="margin-top:10px">
      <button class="btn" id="btn-ml" onclick="toggleML()">▶ ML Detection</button>
      <button class="btn" onclick="window.open('/snapshot.jpg','_blank')">📷 Snapshot</button>
      <button class="btn" onclick="doReconnect()">↻ Reconnect</button>
      <button class="btn" style="color:var(--green);border-color:var(--green)"
        onclick="testEmail()">📧 Test Alert</button>
    </div>
  </div>

  <!-- ML Panel -->
  <div class="panel" style="grid-column:span 2">
    <div class="pt">⚠ Accident Detection</div>
    <div id="ml-panel"><div class="ml-none">ML Detection: OFF</div></div>
  </div>

  <!-- GPS -->
  <div class="panel" style="grid-column:span 2">
    <div class="pt">◎ GPS Location</div>
    <div class="gps-grid">
      <div><div class="gk">LATITUDE</div> <div class="gv" id="g-lat">—</div></div>
      <div><div class="gk">LONGITUDE</div><div class="gv" id="g-lon">—</div></div>
      <div><div class="gk">SPEED</div>    <div class="gv" id="g-spd">—</div></div>
      <div><div class="gk">UPDATED</div>  <div class="gv" id="g-upd" style="font-size:11px;color:var(--dim)">—</div></div>
    </div>
    <div id="map"></div>
    <a id="maplink" href="#" target="_blank">📌 Google Maps</a>
  </div>

  <!-- Alert contacts -->
  <div class="panel">
    <div class="pt">📧 Alert Contacts</div>
    <div style="font-size:11px;line-height:2">
      {% if alert_emails %}
        {% for e in alert_emails %}
          <div style="color:var(--green)">✓ {{ e }}</div>
        {% endfor %}
        <div style="color:var(--dim);margin-top:8px;font-size:10px">
          Email sent on crash confirm<br>with snapshot + GPS link
        </div>
      {% else %}
        <div style="color:var(--orange)">⚠ No contacts set</div>
        <div style="color:var(--dim);margin-top:6px;font-size:10px">
          Add ALERT_EMAILS in<br>Railway environment vars
        </div>
      {% endif %}
    </div>
  </div>

</div>

<!-- Crash modal -->
<div id="crash-modal">
  <div class="mbox">
    <div style="font-size:20px;font-weight:700;color:var(--red);letter-spacing:3px;margin-bottom:8px">
      🚨 CRASH CONFIRMED
    </div>
    <div style="font-size:13px;color:#ffb3b3;margin-bottom:12px" id="modal-detail">—</div>
    <div style="font-size:11px;color:var(--dim);margin-bottom:16px">
      Held ≥ <span style="color:var(--yellow)" id="modal-secs">3</span>s
      — alert email sent to contacts.
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
const ICE_CFG = {
  iceServers:[
    {urls:'stun:stun.l.google.com:19302'},
    {urls:'stun:stun1.l.google.com:19302'},
    {urls:'stun:stun.cloudflare.com:3478'},
    // TURN for CGNAT (Globe/Smart)
    {urls:['turn:openrelay.metered.ca:80',
           'turn:openrelay.metered.ca:443',
           'turn:openrelay.metered.ca:443?transport=tcp'],
     username:'openrelayproject',credential:'openrelayproject'}
  ]
};

let pc=null, ws=null, sse=null, rtcLive=false, wsLive=false;

function setMsg(t,c){const e=document.getElementById('conn-msg');e.textContent=t;e.style.color=c||'var(--dim)';}
function setStat(id,t,c){const e=document.getElementById(id);e.textContent=t;e.className='sv'+(c?' '+c:'');}
function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
setInterval(()=>{document.getElementById('clock').textContent=new Date().toTimeString().slice(0,8);},1000);

// ── SSE fallback ──────────────────────────────────────────────
function startSSE(){
  if(sse) return;
  document.getElementById('badge-sse').style.display='block';
  document.getElementById('badge-live').style.display='none';
  document.getElementById('fallbackImg').style.display='block';
  setStat('s-s','SSE fallback');
  sse=new EventSource('/stream/sse');
  sse.onmessage=e=>{
    document.getElementById('fallbackImg').src='data:image/jpeg;base64,'+e.data;
    document.getElementById('dot-pi').className='dot live';
    if(!rtcLive) setMsg('SSE active (~1s latency)','var(--orange)');
  };
  sse.onerror=()=>{stopSSE();setTimeout(startSSE,3000);};
}
function stopSSE(){if(sse){sse.close();sse=null;}}

// ── WebRTC ────────────────────────────────────────────────────
async function handleOffer(sdp){
  if(pc){pc.close();pc=null;}
  setMsg('Pi offer received — connecting...','var(--yellow)');
  pc=new RTCPeerConnection(ICE_CFG);

  pc.ontrack=ev=>{
    if(!ev.streams||!ev.streams[0]) return;
    document.getElementById('videoEl').srcObject=ev.streams[0];
    document.getElementById('videoEl').style.display='block';
    document.getElementById('fallbackImg').style.display='none';
    document.getElementById('badge-live').style.display='block';
    document.getElementById('badge-sse').style.display='none';
    document.getElementById('dot-rtc').className='dot live';
    document.getElementById('dot-pi').className='dot live';
    rtcLive=true;
    setMsg('⚡ WebRTC live!','var(--green)');
    setStat('s-s','WebRTC ⚡','ok');
    stopSSE();
  };

  pc.onicecandidate=ev=>{
    if(!ev.candidate) return;
    const m={type:'ice',candidate:ev.candidate};
    if(wsLive&&ws&&ws.readyState===1) ws.send(JSON.stringify(m));
    else fetch('/signal/ice',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({candidate:ev.candidate})}).catch(()=>{});
  };

  pc.onconnectionstatechange=()=>{
    const s=pc.connectionState;
    if(s==='failed'||s==='disconnected'||s==='closed'){
      rtcLive=false;
      document.getElementById('dot-rtc').className='dot err';
      setMsg('WebRTC dropped — reconnecting...','var(--red)');
      startSSE();
      setTimeout(doReconnect,3000);
    }
  };

  try{
    await pc.setRemoteDescription(new RTCSessionDescription(sdp));
    const ans=await pc.createAnswer();
    await pc.setLocalDescription(ans);
    const m={type:'answer',sdp:ans};
    if(wsLive&&ws&&ws.readyState===1) ws.send(JSON.stringify(m));
    else await fetch('/signal/answer',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(ans)});
    if(!wsLive) pollPiIce();
  }catch(e){
    console.error('WebRTC error:',e);
    startSSE();
  }
}

async function addIce(c){
  if(pc&&c){
    try{await pc.addIceCandidate(new RTCIceCandidate(c));}catch(e){}
  }
}

async function pollPiIce(){
  for(let i=0;i<30;i++){
    if(wsLive) return;
    try{
      const r=await fetch('/signal/ice/pi');
      const cs=await r.json();
      for(const c of(cs||[])) await addIce(c);
    }catch(e){}
    await sleep(1000);
  }
}

// ── WebSocket signaling ───────────────────────────────────────
function connectWS(){
  document.getElementById('dot-ws').className='dot warn';
  setStat('s-sig','WS connecting...');
  const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws/viewer');

  ws.onopen=()=>{
    wsLive=true;
    document.getElementById('dot-ws').className='dot live';
    setStat('s-sig','WS ✓');
    setMsg('WebSocket connected — waiting for Pi...','var(--yellow)');
  };

  ws.onmessage=async e=>{
    let d; try{d=JSON.parse(e.data);}catch{return;}
    if(d.type==='offer')          await handleOffer(d.sdp);
    else if(d.type==='ice')       await addIce(d.candidate);
    else if(d.type==='pi_connected')    setMsg('Pi connected — offer incoming...','var(--yellow)');
    else if(d.type==='pi_disconnected'){
      setMsg('Pi disconnected','var(--orange)');
      document.getElementById('dot-pi').className='dot err';
    }
  };

  ws.onclose=()=>{
    wsLive=false;
    document.getElementById('dot-ws').className='dot err';
    setStat('s-sig','WS ✗ HTTP fallback');
    pollForOffer();
    setTimeout(connectWS,8000);
  };
}

async function pollForOffer(){
  setMsg('HTTP fallback: polling for offer...','var(--orange)');
  for(let i=0;i<20;i++){
    if(wsLive) return;
    try{
      const r=await fetch('/signal/offer');
      if(r.status===200){const o=await r.json();if(o){await handleOffer(o);return;}}
    }catch(e){}
    await sleep(2000);
  }
  startSSE();
}

function doReconnect(){
  if(pc){pc.close();pc=null;}
  rtcLive=false;
  document.getElementById('dot-rtc').className='dot warn';
  if(wsLive&&ws&&ws.readyState===1) setMsg('Waiting for Pi offer...','var(--yellow)');
  else pollForOffer();
}

// ── ML ────────────────────────────────────────────────────────
let mlOn=false,mlTimer=null,modalDone={0:false,1:false};
function toggleML(){
  mlOn=!mlOn;
  const btn=document.getElementById('btn-ml');
  btn.className='btn'+(mlOn?' active':'');
  btn.textContent=(mlOn?'⏹ Disable':'▶ Enable')+' ML Detection';
  if(mlOn){mlTimer=setInterval(updateML,1200);updateML();}
  else{clearInterval(mlTimer);
    document.getElementById('ml-panel').innerHTML='<div class="ml-none">ML Detection: OFF</div>';
    modalDone={0:false,1:false};}
}
function updateML(){
  fetch('/ml_results').then(r=>r.json()).then(data=>{
    let html='';
    [{k:'0',l:'FRONT',c:'ml-f'},{k:'1',l:'REAR',c:'ml-r'}].forEach(({k,l,c})=>{
      const d=data[k],idx=parseInt(k);
      html+=`<div class="ml-cam"><span class="ml-hdr ${c}">${l} CAM</span>`;
      if(!d||(!d.first_seen&&!d.confirmed)){
        html+='<div class="ml-none" style="margin:4px 0 8px">No detections</div>';
      }else if(d.confirmed){
        const best=d.boxes&&d.boxes.length?d.boxes.reduce((a,b)=>a.conf>b.conf?a:b):null;
        html+=`<div class="alert-box">
          <div style="font-size:13px;font-weight:700;color:var(--red);letter-spacing:2px">🚨 CRASH CONFIRMED</div>
          <div style="font-size:11px;color:#ffb3b3;margin-top:3px">
            ${best?'Side: '+best.side+' | Conf: '+(best.conf*100).toFixed(1)+'%':''}</div>
          <div style="font-size:10px;color:var(--green);margin-top:4px">📧 Alert email sent</div>
        </div>`;
        if(!modalDone[idx]&&best){
          modalDone[idx]=true;
          document.getElementById('modal-detail').textContent=
            'Camera: '+l+' | Side: '+best.side+' | Conf: '+(best.conf*100).toFixed(1)+'%';
          document.getElementById('modal-secs').textContent=(d.elapsed||0).toFixed(1);
          document.getElementById('crash-modal').style.display='flex';
        }
      }else if(d.first_seen){
        const secs=d.confirm_secs||3,pct=Math.min(1,d.elapsed/secs);
        const r=Math.round(255*pct),g=Math.round(255*(1-pct));
        html+=`<div style="font-size:11px;color:var(--yellow);margin:4px 0">
          ⏱ VERIFYING — ${d.elapsed.toFixed(1)}s / ${secs}s</div>
          <div class="vbar-wrap"><div class="vbar-fill"
            style="width:${(pct*100).toFixed(1)}%;background:rgb(${r},${g},0)"></div></div>`;
        if(d.boxes&&d.boxes.length){const b=d.boxes.reduce((a,x)=>a.conf>x.conf?a:x);
          html+=`<div style="font-size:11px;color:var(--dim);margin-top:4px">
            ${b.label} · ${b.side} · ${(b.conf*100).toFixed(1)}%</div>`;}
        modalDone[idx]=false;
      }else{modalDone[idx]=false;}
      html+='</div>';
    });
    document.getElementById('ml-panel').innerHTML=html;
  }).catch(()=>{});
}

// ── Camera status ─────────────────────────────────────────────
function updateStatus(){
  fetch('/status').then(r=>r.json()).then(d=>{
    function s(id,txt){const e=document.getElementById(id);e.textContent=txt;
      e.className='sv'+(txt.includes('Streaming')?' ok':txt.includes('ERROR')?' err':'');}
    s('s-f',d['0']||'—');s('s-r',d['1']||'—');
  }).catch(()=>{});
}
setInterval(updateStatus,3000);updateStatus();

// ── GPS ───────────────────────────────────────────────────────
let lmap=null,lmk=null;
function updateGPS(){
  fetch('/gps').then(r=>r.json()).then(d=>{
    if(!d.lat||!d.lon) return;
    document.getElementById('g-lat').textContent=d.lat.toFixed(6)+'°';
    document.getElementById('g-lon').textContent=d.lon.toFixed(6)+'°';
    document.getElementById('g-spd').textContent=d.speed!=null?d.speed.toFixed(1)+' km/h':'—';
    document.getElementById('g-upd').textContent=d.updated||'—';
    document.getElementById('maplink').style.display='block';
    document.getElementById('maplink').href='https://maps.google.com/?q='+d.lat+','+d.lon;
    document.getElementById('map').style.display='block';
    if(!lmap){
      lmap=L.map('map').setView([d.lat,d.lon],16);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        {attribution:'© OSM'}).addTo(lmap);
      lmk=L.marker([d.lat,d.lon]).addTo(lmap).bindPopup('Pi Camera').openPopup();
    }else{lmk.setLatLng([d.lat,d.lon]);lmap.setView([d.lat,d.lon]);}
  }).catch(()=>{});
}
setInterval(updateGPS,5000);updateGPS();

// ── Test email ────────────────────────────────────────────────
function testEmail(){
  fetch('/test/email').then(r=>r.text()).then(t=>alert(t)).catch(()=>alert('Failed'));
}

// ── Boot ──────────────────────────────────────────────────────
startSSE();
connectWS();
</script>
</body>
</html>"""
    return render_template_string(html,
        username=session.get('username',''),
        alert_status=alert_status,
        alert_emails=ALERT_EMAILS)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)

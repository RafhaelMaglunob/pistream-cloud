#!/usr/bin/env python3
"""
Pi Camera Relay Server with Firebase Integration
Deploy on Railway — WebSocket signaling + Firebase TrustedContact alerts
10-second countdown before sending crash email to trusted contacts
"""
from flask import Flask, Response, jsonify, request, render_template_string, session, redirect, url_for
from flask_sock import Sock
from functools import wraps
import threading, time, logging, os, json, queue, base64
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import firebase_admin
from firebase_admin import credentials, firestore

app  = Flask(__name__)
sock = Sock(app)
app.secret_key = os.environ.get("SECRET_KEY", "supersecretkey123")
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# ── Firebase Init ─────────────────────────────────
try:
    # Initialize Firebase (use your Firebase credentials JSON)
    firebase_admin.initialize_app(credentials.Certificate("firebase-key.json"))
    db = firestore.client()
    FIREBASE_ENABLED = True
    print("[FIREBASE] ✅ Connected")
except Exception as e:
    FIREBASE_ENABLED = False
    print(f"[FIREBASE] ⚠ Disabled: {e}")
    db = None

# ── Gmail config ──────────────────────────────────
GMAIL_USER     = os.environ.get("GMAIL_USER",     "")
GMAIL_PASSWORD = os.environ.get("GMAIL_PASSWORD", "")

# ── Crash notification with countdown ─────────────
CRASH_DISMISS_TIMEOUT = 10  # seconds user has to dismiss
CRASH_COOLDOWN = 60         # cooldown after confirmed send

_crash_state = {0: {"detected": False, "timer": None, "time_left": 0}, 
                1: {"detected": False, "timer": None, "time_left": 0}}
_crash_lock = threading.Lock()

# ── WebSocket connections ─────────────────────────────────
signal_lock    = threading.Lock()
pi_ws          = None
viewer_ws_list = []

# ── HTTP fallback queues ──────────────────────────────────
http_offers  = queue.Queue(maxsize=5)
http_answers = queue.Queue(maxsize=5)
http_pi_ice  = queue.Queue(maxsize=100)
http_br_ice  = queue.Queue(maxsize=100)

# ── Shared state ──────────────────────────────────
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

# ── Current logged-in user (for Firebase lookup) ──
current_user_email = None
user_lock = threading.Lock()

# ═════════════════════════════════════════════════════════════
#  Firebase TrustedContact Handler
# ═════════════════════════════════════════════════════════════
def get_trusted_contacts(user_email):
    """Fetch trusted contacts for user from Firebase"""
    if not FIREBASE_ENABLED or not db:
        print("[FIREBASE] DB not available")
        return []
    
    try:
        doc = db.collection('TrustedContact').document(user_email).get()
        if doc.exists:
            data = doc.to_dict()
            contacts = data.get('contacts', [])
            print(f"[FIREBASE] Found {len(contacts)} trusted contact(s) for {user_email}")
            return contacts
        else:
            print(f"[FIREBASE] No TrustedContact doc for {user_email}")
            return []
    except Exception as e:
        print(f"[FIREBASE] Error fetching contacts: {e}")
        return []

def send_crash_email(cam_label, side, conf, elapsed, snapshot_bytes, user_email):
    """Send crash alert email to trusted contacts"""
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print("[EMAIL] Not configured — set GMAIL_USER, GMAIL_PASSWORD")
        return
    
    contacts = get_trusted_contacts(user_email)
    if not contacts:
        print(f"[EMAIL] No trusted contacts for {user_email}")
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
    From: {user_email}
  </p>
</div></body></html>"""

        subject = f"🚨 CRASH — {cam_label} | {side} | {conf:.0f}%"

        for contact in contacts:
            contact_email = contact.get('email') if isinstance(contact, dict) else contact
            try:
                msg = MIMEMultipart('related')
                msg['Subject'] = subject
                msg['From']    = f"Pi Camera <{GMAIL_USER}>"
                msg['To']      = contact_email
                msg.attach(MIMEText(html_body, 'html'))
                if snapshot_bytes:
                    img = MIMEImage(snapshot_bytes, _subtype='jpeg')
                    img.add_header('Content-Disposition', 'attachment',
                                   filename='crash_snapshot.jpg')
                    msg.attach(img)
                with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as smtp:
                    smtp.login(GMAIL_USER, GMAIL_PASSWORD)
                    smtp.sendmail(GMAIL_USER, contact_email, msg.as_string())
                print(f"[EMAIL] ✅ Sent to {contact_email}")
            except Exception as e:
                print(f"[EMAIL] ❌ {contact_email}: {e}")

    threading.Thread(target=_send, daemon=True).start()


def handle_crash_detection(cam_idx, ml_data):
    """
    Handle crash detection with 10-second countdown.
    User can dismiss as false alarm within 10 seconds.
    """
    now = time.time()
    with _crash_lock:
        state = _crash_state[cam_idx]
        
        d = ml_data.get(str(cam_idx), {})
        if not d or not d.get('confirmed'):
            # No detection — reset
            if state["timer"]:
                state["timer"].cancel()
                state["timer"] = None
            state["detected"] = False
            state["time_left"] = 0
            return
        
        # Crash confirmed by Pi
        if not state["detected"]:
            # NEW detection — start 10-second countdown
            state["detected"] = True
            state["time_left"] = CRASH_DISMISS_TIMEOUT
            
            label = 'FRONT' if cam_idx == 0 else 'REAR'
            boxes = d.get('boxes', [])
            best = max(boxes, key=lambda b: b['conf']) if boxes else None
            side = best['side'] if best else 'Unknown'
            conf = best['conf'] * 100 if best else 0.0
            elapsed = d.get('elapsed', 0.0)
            
            print(f"[CRASH] 🚨 {label} crash detected! 10s countdown to alert...")
            _broadcast_viewers({"type": "crash_countdown", "cam": cam_idx, "seconds": CRASH_DISMISS_TIMEOUT})
            
            # Schedule email send in 10 seconds if not dismissed
            def send_alert():
                with _crash_lock:
                    if _crash_state[cam_idx]["detected"]:
                        # Still detected → send email
                        with frame_lock: snap = latest_frame
                        with user_lock: user_email = current_user_email
                        if user_email:
                            send_crash_email(label, side, conf, elapsed, snap, user_email)
                        print(f"[CRASH] 📧 Alert sent for {label}")
                        _broadcast_viewers({"type": "crash_sent", "cam": cam_idx})
            
            state["timer"] = threading.Timer(CRASH_DISMISS_TIMEOUT, send_alert)
            state["timer"].start()

def dismiss_crash(cam_idx):
    """User dismisses crash as false alarm"""
    with _crash_lock:
        state = _crash_state[cam_idx]
        if state["timer"]:
            state["timer"].cancel()
            state["timer"] = None
        state["detected"] = False
        state["time_left"] = 0
    
    label = 'FRONT' if cam_idx == 0 else 'REAR'
    print(f"[CRASH] ✓ {label} crash dismissed by user")
    _broadcast_viewers({"type": "crash_dismissed", "cam": cam_idx})


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
    return (request.headers.get('X-Secret') == "changeme123" or
            request.args.get('secret')       == "changeme123")

@app.route('/login', methods=['GET', 'POST'])
def login():
    global current_user_email
    error = ''
    if request.method == 'POST':
        u = request.form.get('email', '').strip()
        p = request.form.get('password', '')
        
        # For demo: accept any email with password "User@123"
        if p == os.environ.get("USER_PASSWORD", "User@123"):
            session['logged_in'] = True
            session['username']  = u
            with user_lock:
                current_user_email = u
            return redirect(url_for('index'))
        error = 'Invalid email or password'
    
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
    <input type="email" name="email" placeholder="Email" required autofocus>
    <input type="password" name="password" placeholder="Password" required>
    <button type="submit">LOGIN</button>
  </form>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
</div></body></html>"""
    return render_template_string(html, error=error)

@app.route('/logout')
def logout():
    global current_user_email
    session.clear()
    with user_lock:
        current_user_email = None
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
            elif t == 'crash_dismiss':
                cam_idx = data.get('cam')
                if cam_idx is not None:
                    dismiss_crash(int(cam_idx))
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
    
    # Check both cameras for crash confirmation
    for cam_idx in (0, 1):
        handle_crash_detection(cam_idx, data)
    
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


# ═════════════════════════════════════════════════════════════
#  Main User Dashboard UI — Lite 360 Style
# ═════════════════════════════════════════════════════════════
@app.route('/')
@login_required
def index():
    username = session.get('username', '')
    html = r"""<!DOCTYPE html>
<html>
<head>
<title>Pi Camera — Live Detection</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root {
  --cyan: #00e5ff; --orange: #ff6d00; --green: #00e676;
  --red: #ff1744; --yellow: #ffd600;
  --bg: #050a0e; --panel: #0a1520; --border: #1a2e40;
  --text: #c8d8e8; --dim: #4a6070;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  background: var(--bg); color: var(--text);
  font-family: 'Share Tech Mono', monospace; min-height: 100vh;
}

.header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 20px;
  background: linear-gradient(135deg, #050a0e 60%, #0a1a28);
  border-bottom: 1px solid var(--border);
}
.header-title {
  font-family: 'Rajdhani', sans-serif; font-weight: 700; font-size: 20px;
  letter-spacing: 3px; color: var(--cyan);
  text-shadow: 0 0 12px rgba(0, 229, 255, 0.5);
}
.header-title span { color: var(--orange); }
.header-right {
  display: flex; gap: 12px; align-items: center; font-size: 11px;
}
.dot {
  width: 8px; height: 8px; border-radius: 50%; display: inline-block;
  margin-right: 4px; transition: background 0.3s;
}
.dot.live { background: var(--green); box-shadow: 0 0 8px var(--green); }
.dot.warn { background: var(--orange); }
.dot.err { background: var(--red); }
a.logout { color: var(--red); text-decoration: none; border: 1px solid var(--red); padding: 3px 8px; }

.stream-container {
  display: flex; gap: 12px; padding: 14px 20px;
  flex-wrap: wrap; justify-content: center;
}
.stream-panel {
  flex: 1; min-width: 300px; max-width: 600px;
  background: #000; position: relative;
  border: 1px solid var(--border); border-radius: 4px; overflow: hidden;
}
.stream-panel img {
  width: 100%; display: block; background: #000;
}
.fullscreen-btn {
  position: absolute; top: 8px; right: 8px;
  background: rgba(0, 229, 255, 0.2); border: 1px solid var(--cyan);
  color: var(--cyan); cursor: pointer; padding: 6px 10px;
  font-family: monospace; font-size: 11px; border-radius: 2px;
  z-index: 10;
}
.fullscreen-btn:hover { background: rgba(0, 229, 255, 0.4); }
.cam-label {
  position: absolute; top: 8px; left: 8px; font-size: 12px;
  font-weight: 700; letter-spacing: 2px; padding: 3px 10px;
  pointer-events: none;
}
.label-front {
  background: rgba(0, 229, 255, 0.2); color: var(--cyan);
  border: 1px solid var(--cyan);
}
.label-rear {
  background: rgba(255, 109, 0, 0.2); color: var(--orange);
  border: 1px solid var(--orange);
}

/* FULLSCREEN OVERLAY */
#fullscreen-modal {
  display: none; position: fixed; inset: 0; z-index: 9998;
  background: #000; align-items: center; justify-content: center;
}
#fullscreen-modal.active { display: flex; }
#fullscreen-modal img {
  width: 100%; height: 100%; object-fit: contain;
}
.fs-close {
  position: absolute; top: 10px; right: 10px; z-index: 9999;
  background: rgba(0, 0, 0, 0.7); border: 1px solid var(--cyan);
  color: var(--cyan); font-size: 24px; cursor: pointer;
  width: 40px; height: 40px; display: flex;
  align-items: center; justify-content: center;
}

/* CRASH ALERT MODAL */
#crash-modal {
  display: none; position: fixed; inset: 0; z-index: 9999;
  background: rgba(0, 0, 0, 0.85);
  align-items: center; justify-content: center;
}
#crash-modal.active { display: flex; }
.crash-box {
  background: #0d0608; border: 3px solid var(--red);
  max-width: 480px; width: 90%; padding: 32px;
  box-shadow: 0 0 40px rgba(255, 23, 68, 0.6);
  text-align: center;
  animation: crashPulse 0.8s infinite alternate;
}
@keyframes crashPulse {
  from { box-shadow: 0 0 20px rgba(255, 23, 68, 0.4); }
  to { box-shadow: 0 0 50px rgba(255, 23, 68, 0.8); }
}
.crash-title {
  font-family: 'Rajdhani', sans-serif; font-weight: 700;
  font-size: 32px; color: var(--red);
  letter-spacing: 3px; margin-bottom: 8px;
}
.crash-detail {
  font-size: 13px; color: #ffb3b3; margin-bottom: 16px;
  line-height: 1.6;
}
.crash-countdown {
  font-size: 28px; font-weight: 700; color: var(--yellow);
  margin: 16px 0;
  font-family: 'Rajdhani', sans-serif;
}
.crash-buttons {
  display: flex; gap: 10px; margin-top: 20px;
}
.crash-btn {
  flex: 1; padding: 14px 20px;
  font-family: 'Rajdhani', sans-serif; font-weight: 700;
  font-size: 13px; letter-spacing: 1px;
  border: 1px solid; cursor: pointer; border-radius: 2px;
  transition: all 0.2s;
}
.crash-dismiss {
  background: transparent; border-color: var(--dim); color: var(--dim);
}
.crash-dismiss:hover { border-color: var(--cyan); color: var(--cyan); }
.crash-confirm {
  background: rgba(255, 23, 68, 0.2); border-color: var(--red);
  color: var(--red);
}
.crash-confirm:hover {
  background: rgba(255, 23, 68, 0.4); box-shadow: 0 0 12px var(--red);
}

/* INFO PANEL */
.info-grid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
  padding: 14px 20px;
}
@media (max-width: 600px) { .info-grid { grid-template-columns: 1fr; } }
.info-panel {
  background: var(--panel); border: 1px solid var(--border);
  padding: 12px; border-radius: 2px;
}
.panel-title {
  font-family: 'Rajdhani', sans-serif; font-weight: 600;
  font-size: 11px; letter-spacing: 2px; color: var(--dim);
  margin-bottom: 8px; text-transform: uppercase;
  border-bottom: 1px solid var(--border); padding-bottom: 4px;
}
.stat-row {
  display: flex; justify-content: space-between;
  font-size: 11px; padding: 4px 0;
}
.stat-key { color: var(--dim); }
.stat-val { color: var(--text); }
.stat-val.ok { color: var(--green); }
.stat-val.err { color: var(--red); }

/* GPS */
.gps-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.gps-val { font-size: 16px; font-weight: 700; color: var(--green); }
.gps-key { font-size: 10px; color: var(--dim); letter-spacing: 1px; }
#map { height: 200px; border: 1px solid var(--border); margin-top: 8px; }

.logout { float: right; }
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>

<div class="header">
  <div class="header-title">PI<span>CAM</span> LIVE</div>
  <div class="header-right">
    <span><span class="dot" id="dot-ws"></span>WS</span>
    <span><span class="dot" id="dot-rtc"></span>RTC</span>
    <span><span class="dot" id="dot-pi"></span>Pi</span>
    <span style="color: var(--cyan);" id="clock">--:--:--</span>
    <span style="color: var(--dim);">{{ username }}</span>
    <a class="logout" href="/logout">OUT</a>
  </div>
</div>

<!-- CAMERAS: 2-COLUMN LAYOUT -->
<div class="stream-container">
  <div class="stream-panel">
    <img id="stream-front" src="/stream/front" alt="Front Camera">
    <span class="cam-label label-front">◀ FRONT</span>
    <button class="fullscreen-btn" onclick="toggleFullscreen('front')">⛶</button>
  </div>
  <div class="stream-panel">
    <img id="stream-rear" src="/stream/rear" alt="Rear Camera">
    <span class="cam-label label-rear">REAR ▶</span>
    <button class="fullscreen-btn" onclick="toggleFullscreen('rear')">⛶</button>
  </div>
</div>

<!-- FULLSCREEN MODAL -->
<div id="fullscreen-modal">
  <img id="fullscreen-img" src="" alt="">
  <div class="fs-close" onclick="toggleFullscreen()">✕</div>
</div>

<!-- CRASH ALERT MODAL -->
<div id="crash-modal">
  <div class="crash-box">
    <div class="crash-title">🚨 CRASH DETECTED</div>
    <div class="crash-detail" id="crash-info">—</div>
    <div class="crash-countdown" id="crash-timer">10</div>
    <div style="font-size: 12px; color: var(--dim); margin: 8px 0;">
      Alerting your trusted contacts in <span id="timer-text">10s</span>...
    </div>
    <div class="crash-buttons">
      <button class="crash-btn crash-dismiss" id="dismiss-btn" onclick="dismissCrash()">
        FALSE ALARM
      </button>
      <button class="crash-btn crash-confirm" disabled style="opacity: 0.6;">
        ALERT SENT
      </button>
    </div>
  </div>
</div>

<!-- INFO PANELS -->
<div class="info-grid">
  <!-- Status -->
  <div class="info-panel">
    <div class="panel-title">◉ Status</div>
    <div class="stat-row"><span class="stat-key">FRONT</span><span class="stat-val" id="s-f">—</span></div>
    <div class="stat-row"><span class="stat-key">REAR</span><span class="stat-val" id="s-r">—</span></div>
    <div class="stat-row"><span class="stat-key">STREAM</span><span class="stat-val" id="s-s">—</span></div>
    <div class="stat-row"><span class="stat-key">SIGNAL</span><span class="stat-val" id="s-sig">—</span></div>
  </div>

  <!-- GPS -->
  <div class="info-panel">
    <div class="panel-title">◎ GPS Location</div>
    <div class="gps-grid">
      <div><div class="gps-key">LATITUDE</div><div class="gps-val" id="g-lat">—</div></div>
      <div><div class="gps-key">LONGITUDE</div><div class="gps-val" id="g-lon">—</div></div>
      <div><div class="gps-key">SPEED</div><div class="gps-val" id="g-spd">—</div></div>
    </div>
    <div id="map"></div>
  </div>
</div>

<script>
const ICE_CFG = {
  iceServers: [
    {urls: 'stun:stun.l.google.com:19302'},
    {urls: 'stun:stun1.l.google.com:19302'},
    {urls: 'stun:stun.cloudflare.com:3478'},
    {urls: ['turn:openrelay.metered.ca:80',
           'turn:openrelay.metered.ca:443',
           'turn:openrelay.metered.ca:443?transport=tcp'],
     username: 'openrelayproject', credential: 'openrelayproject'}
  ]
};

let ws = null, rtcLive = false, wsLive = false;
let fsMode = null;
let crashCountdown = 0, crashTimer = null, crashCam = null;

// ── TIME ──
setInterval(() => {
  document.getElementById('clock').textContent = new Date().toTimeString().slice(0, 8);
}, 1000);

// ── FULLSCREEN ──
function toggleFullscreen(cam) {
  const modal = document.getElementById('fullscreen-modal');
  const img = document.getElementById('fullscreen-img');
  if (cam) {
    fsMode = cam;
    img.src = (cam === 'front' ? '/stream/front' : '/stream/rear') + '?t=' + Date.now();
    modal.classList.add('active');
  } else {
    modal.classList.remove('active');
    fsMode = null;
  }
}

// ── CRASH HANDLING ──
function dismissCrash() {
  if (crashTimer) clearInterval(crashTimer);
  const modal = document.getElementById('crash-modal');
  modal.classList.remove('active');
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({type: 'crash_dismiss', cam: crashCam}));
  }
  crashCam = null;
}

function showCrashAlert(cam, info) {
  crashCam = cam;
  crashCountdown = 10;
  document.getElementById('crash-info').textContent = info;
  document.getElementById('crash-modal').classList.add('active');
  
  if (crashTimer) clearInterval(crashTimer);
  crashTimer = setInterval(() => {
    crashCountdown--;
    document.getElementById('crash-timer').textContent = crashCountdown;
    document.getElementById('timer-text').textContent = crashCountdown + 's';
    document.getElementById('dismiss-btn').disabled = false;
    
    if (crashCountdown <= 0) {
      clearInterval(crashTimer);
      // Auto-close modal when timer expires
      setTimeout(() => {
        document.getElementById('crash-modal').classList.remove('active');
      }, 1000);
    }
  }, 1000);
}

// ── WEBSOCKET ──
function connectWS() {
  document.getElementById('dot-ws').classList = 'dot warn';
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws/viewer');

  ws.onopen = () => {
    wsLive = true;
    document.getElementById('dot-ws').classList = 'dot live';
  };

  ws.onmessage = async (e) => {
    let d;
    try { d = JSON.parse(e.data); } catch { return; }
    
    if (d.type === 'crash_countdown') {
      const cam = d.cam;
      const camLabel = cam === 0 ? 'FRONT' : 'REAR';
      showCrashAlert(cam, `🚨 ${camLabel} camera detected a crash!`);
    } else if (d.type === 'crash_sent') {
      document.getElementById('crash-modal').classList.remove('active');
    } else if (d.type === 'crash_dismissed') {
      document.getElementById('crash-modal').classList.remove('active');
    }
  };

  ws.onclose = () => {
    wsLive = false;
    document.getElementById('dot-ws').classList = 'dot err';
    setTimeout(connectWS, 5000);
  };
}

// ── STATUS ──
function updateStatus() {
  fetch('/status').then(r => r.json()).then(d => {
    document.getElementById('s-f').textContent = d['0'] || '—';
    document.getElementById('s-r').textContent = d['1'] || '—';
  });
}
setInterval(updateStatus, 3000);
updateStatus();

// ── GPS ──
let lmap = null, lmk = null;
function updateGPS() {
  fetch('/gps').then(r => r.json()).then(d => {
    if (!d.lat || !d.lon) return;
    document.getElementById('g-lat').textContent = d.lat.toFixed(6) + '°';
    document.getElementById('g-lon').textContent = d.lon.toFixed(6) + '°';
    document.getElementById('g-spd').textContent = d.speed != null ? d.speed.toFixed(1) + ' km/h' : '—';
    
    if (!lmap) {
      lmap = L.map('map').setView([d.lat, d.lon], 16);
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
        {attribution: '© OSM'}).addTo(lmap);
      lmk = L.marker([d.lat, d.lon]).addTo(lmap).bindPopup('Pi Camera').openPopup();
    } else {
      lmk.setLatLng([d.lat, d.lon]);
      lmap.setView([d.lat, d.lon]);
    }
  });
}
setInterval(updateGPS, 5000);
updateGPS();

// ── INIT ──
connectWS();
</script>
</body>
</html>"""
    return render_template_string(html, username=username)


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
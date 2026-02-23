#!/usr/bin/env python3
from flask import Flask, Response, request, jsonify, render_template_string
import threading, time, socket, io, subprocess, queue
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import colorsys
import torch
from ultralytics import YOLO
import requests as req_lib

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# ------------------------ CLOUD CONFIG ------------------------
CLOUD_URL     = "https://pistream-cloud.onrender.com"
PUSH_SECRET   = "Rafhael@1"
CLOUD_ENABLED = True

PUSH_HEADERS_FRAME = {"X-Secret": PUSH_SECRET, "Content-Type": "image/jpeg"}
PUSH_HEADERS_JSON  = {"X-Secret": PUSH_SECRET, "Content-Type": "application/json"}

# ------------------------ GPS CONFIG --------------------------
STATIC_LAT = None   # e.g. 14.5995
STATIC_LON = None   # e.g. 120.9842
GPS_PORT   = "/dev/ttyAMA0"
GPS_BAUD   = 9600

# ------------------------ GLOBALS ------------------------
latest_frame = None
first_frame  = None
frame_lock   = threading.Lock()
frame_ready  = threading.Event()

latest_overlay_frame = None
overlay_lock = threading.Lock()

color_detection_enabled = False
detection_mode  = 'center'
detected_colors = []
detection_lock  = threading.Lock()

ml_detection_enabled = False
ml_lock    = threading.Lock()
ml_results = []
model      = None

camera_status      = "Starting..."
camera_status_lock = threading.Lock()

gps_state      = {"lat": None, "lon": None, "speed": None}
gps_state_lock = threading.Lock()

# Non-blocking queue for cloud pushes — never blocks local stream
frame_queue  = queue.Queue(maxsize=2)   # only keep latest 2 frames
ml_queue     = queue.Queue(maxsize=5)
status_queue = queue.Queue(maxsize=5)
gps_queue    = queue.Queue(maxsize=5)

# ------------------------ UTILS ------------------------
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"

def check_port(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', port))
    sock.close()
    return result != 0

def set_camera_status(msg):
    global camera_status
    with camera_status_lock:
        camera_status = msg
    print(f"[CAMERA] {msg}")
    try:
        status_queue.put_nowait(msg)
    except queue.Full:
        pass

def rgb_to_color_name(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
    h = h*360; s = s*100; v = v*100
    if s < 10:
        if v < 20:   return "Black"
        elif v > 80: return "White"
        else:        return "Gray"
    if v < 20: return "Black"
    if h < 15 or h >= 345: return "Red"
    elif h < 45:  return "Orange"
    elif h < 75:  return "Yellow"
    elif h < 155: return "Green"
    elif h < 185: return "Cyan"
    elif h < 250: return "Blue"
    elif h < 290: return "Purple"
    elif h < 345: return "Magenta"
    return "Unknown"

# ------------------------ COLOR DETECTION ------------------------
def detect_colors_in_frame(frame_bytes, mode='center'):
    global detected_colors
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        arr = np.array(img)
        h, w = arr.shape[:2]
        colors = []
        if mode == 'center':
            cx, cy = w//2, h//2
            sample = arr[max(0,cy-30):cy+30, max(0,cx-30):cx+30]
            r, g, b = sample.mean(axis=(0,1)).astype(int)
            colors.append({
                'position': 'center',
                'rgb': f'rgb({r},{g},{b})',
                'rgba': f'rgba({r},{g},{b},1)',
                'hex': f'#{r:02x}{g:02x}{b:02x}',
                'name': rgb_to_color_name(r, g, b),
                'coords': (cx, cy),
                'r': int(r), 'g': int(g), 'b': int(b)
            })
        elif mode == 'grid':
            grid_size = 3
            for i in range(grid_size):
                for j in range(grid_size):
                    x = int(w*(j+0.5)/grid_size)
                    y = int(h*(i+0.5)/grid_size)
                    sample = arr[max(0,y-20):y+20, max(0,x-20):x+20]
                    r, g, b = sample.mean(axis=(0,1)).astype(int)
                    colors.append({
                        'position': f'grid_{i}_{j}',
                        'rgb': f'rgb({r},{g},{b})',
                        'rgba': f'rgba({r},{g},{b},1)',
                        'hex': f'#{r:02x}{g:02x}{b:02x}',
                        'name': rgb_to_color_name(r, g, b),
                        'coords': (x, y),
                        'r': int(r), 'g': int(g), 'b': int(b)
                    })
        with detection_lock:
            detected_colors = colors
    except Exception as e:
        print(f"Color detection error: {e}")

# ------------------------ ML DETECTION ------------------------
CRASH_LABEL          = 'motor_crash'
CRASH_CONF_THRESHOLD = 0.90

def detect_accidents_in_frame(frame_bytes):
    global ml_results
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        results = model.predict(source=np.array(img), imgsz=320, conf=0.5, verbose=False)
        boxes = []
        w, h = img.size
        for r in results:
            if len(r.boxes) == 0:
                continue
            for box, conf, cls in zip(r.boxes.xyxy, r.boxes.conf, r.boxes.cls):
                label = model.names[int(cls)]
                if label != CRASH_LABEL:
                    continue
                if float(conf) < CRASH_CONF_THRESHOLD:
                    continue
                x1, y1, x2, y2 = map(int, box.tolist())
                cx   = (x1+x2)//2
                side = 'Left' if cx < w/3 else ('Center' if cx < w*2/3 else 'Right')
                boxes.append({
                    'box': [x1, y1, x2, y2],
                    'label': label,
                    'conf': float(conf),
                    'side': side
                })
        with ml_lock:
            ml_results = boxes
        try:
            ml_queue.put_nowait(boxes)
        except queue.Full:
            pass
    except Exception as e:
        print(f"ML detection error: {e}")

# ------------------------ OVERLAY ------------------------
def add_overlay(frame_bytes, mode='center'):
    try:
        img  = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        draw = ImageDraw.Draw(img, 'RGBA')
        font = ImageFont.load_default()

        with detection_lock:
            colors = detected_colors.copy()
        for color in colors:
            x, y = color['coords']
            if mode == 'center':
                draw.line([(x-20, y), (x+20, y)], fill=(0,255,0,255), width=2)
                draw.line([(x, y-20), (x, y+20)], fill=(0,255,0,255), width=2)
            elif mode == 'grid':
                draw.ellipse([x-10, y-10, x+10, y+10],
                             fill=(color['r'], color['g'], color['b'], 255))

        with ml_lock:
            boxes = ml_results.copy()
        for b in boxes:
            x1, y1, x2, y2 = b['box']
            draw.rectangle([x1, y1, x2, y2], outline=(255,0,0,255), width=2)
            text = f"{b['label']} {b['conf']*100:.0f}% {b['side']}"
            draw.text((x1, max(0, y1-10)), text, fill=(255,0,0,255), font=font)

        out = io.BytesIO()
        img.save(out, format='JPEG', quality=85)
        return out.getvalue()
    except Exception as e:
        print(f"Overlay error: {e}")
        return frame_bytes

# ------------------------ CAMERA THREAD ------------------------
def kill_existing_camera():
    try:
        subprocess.run(['pkill', '-f', 'rpicam-vid'], capture_output=True)
        time.sleep(1)
    except Exception:
        pass

def camera_thread():
    global latest_frame, first_frame, latest_overlay_frame
    restart_delay = 2
    max_delay     = 15
    consecutive_failures = 0

    while True:
        process = None
        frames_captured = 0
        set_camera_status("Killing any stale camera processes...")
        kill_existing_camera()

        try:
            set_camera_status("Starting rpicam-vid...")
            process = subprocess.Popen(
                ['rpicam-vid', '-t', '0',
                 '--width', '640', '--height', '480',
                 '--framerate', '15',
                 '--codec', 'mjpeg',
                 '--quality', '50',
                 '--inline', '--nopreview',
                 '--denoise', 'off',
                 '--sharpness', '1.0',
                 '--contrast', '1.0',
                 '--brightness', '0.0',
                 '--saturation', '1.0',
                 '--awb', 'auto',
                 '--flush', '1',
                 '-o', '-'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )

            SOI = b'\xff\xd8'
            EOI = b'\xff\xd9'
            buffer = b''
            last_frame_time = time.time()
            set_camera_status("Camera started, waiting for first frame...")

            while True:
                if time.time() - last_frame_time > 10:
                    set_camera_status("Watchdog: no frame for 10s, restarting...")
                    break

                try:
                    chunk = process.stdout.read(8192)
                except Exception as e:
                    set_camera_status(f"Read error: {e}")
                    break

                if not chunk:
                    try:
                        err = process.stderr.read(2048).decode(errors='replace').strip()
                    except Exception:
                        err = "(could not read stderr)"
                    set_camera_status(f"Camera process ended. stderr: {err or '(none)'}")
                    with frame_lock:
                        latest_frame = None
                    with overlay_lock:
                        latest_overlay_frame = None
                    break

                buffer += chunk

                if len(buffer) > 4 * 1024 * 1024:
                    set_camera_status("Buffer overflow, resetting...")
                    buffer = b''
                    continue

                while True:
                    soi = buffer.find(SOI)
                    if soi == -1: break
                    eoi = buffer.find(EOI, soi + 2)
                    if eoi == -1: break

                    frame  = buffer[soi:eoi + 2]
                    buffer = buffer[eoi + 2:]

                    if len(frame) < 1024:
                        continue

                    last_frame_time  = time.time()
                    frames_captured += 1

                    with frame_lock:
                        latest_frame = frame
                        if first_frame is None:
                            first_frame = frame
                            frame_ready.set()
                            set_camera_status("First frame received — streaming!")

                    if frames_captured % 150 == 0:
                        set_camera_status(f"Running OK — {frames_captured} frames captured")

        except FileNotFoundError:
            set_camera_status("ERROR: rpicam-vid not found!")
            time.sleep(10)
            continue
        except Exception as e:
            set_camera_status(f"Unexpected camera error: {e}")
        finally:
            if process:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except Exception:
                    try: process.kill()
                    except Exception: pass

        if frames_captured == 0:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
            restart_delay = 2

        restart_delay = min(max_delay, 2 * (consecutive_failures + 1))
        set_camera_status(f"Restarting in {restart_delay}s...")
        time.sleep(restart_delay)

# ------------------------ DETECTION WORKERS ------------------------
def overlay_worker():
    global latest_overlay_frame
    last_processed = None
    color_skip     = 0

    while True:
        time.sleep(0.066)  # 15fps local stream — unaffected by cloud
        with frame_lock:
            frame = latest_frame
        if frame is None or frame is last_processed:
            continue
        last_processed = frame

        if color_detection_enabled and color_skip % 2 == 0:
            detect_colors_in_frame(frame, detection_mode)
        color_skip += 1

        rendered = add_overlay(frame, detection_mode)
        with overlay_lock:
            latest_overlay_frame = rendered

        # Queue frame for cloud — non-blocking, drops if cloud is slow
        if CLOUD_ENABLED:
            try:
                frame_queue.put_nowait(rendered)
            except queue.Full:
                pass  # cloud is slow, drop frame — local stream unaffected

def ml_worker():
    last_processed = None
    while True:
        time.sleep(0.5)
        if not ml_detection_enabled:
            continue
        with frame_lock:
            frame = latest_frame
        if frame is None or frame is last_processed:
            continue
        last_processed = frame
        detect_accidents_in_frame(frame)

# ------------------------ CLOUD SENDER (single thread, never blocks local) --
def cloud_sender():
    """
    Single thread handles ALL cloud pushes via queues.
    If Render is slow/down, this thread waits — local stream is NEVER affected.
    """
    if not CLOUD_ENABLED:
        return

    session = req_lib.Session()
    session.headers.update({"X-Secret": PUSH_SECRET})

    last_frame_push  = 0
    last_status_push = 0
    last_gps_push    = 0
    last_ml_push     = 0

    print(f"[CLOUD] Sender started → {CLOUD_URL}")

    while True:
        now = time.time()

        # ── Push frame at max 2fps ──────────────────────────────
        if now - last_frame_push >= 0.5:
            frame = None
            # Drain queue, keep only latest
            while not frame_queue.empty():
                try:
                    frame = frame_queue.get_nowait()
                except queue.Empty:
                    break
            if frame:
                try:
                    r = session.post(f"{CLOUD_URL}/push/frame",
                                     data=frame,
                                     headers={"Content-Type": "image/jpeg"},
                                     timeout=5)
                    if r.status_code == 401:
                        print("[CLOUD] ❌ Wrong PUSH_SECRET!")
                    elif r.status_code == 200:
                        last_frame_push = now
                except Exception as e:
                    print(f"[CLOUD] Frame push failed: {e}")

        # ── Push ML results every 1s ────────────────────────────
        if now - last_ml_push >= 1.0:
            ml = None
            while not ml_queue.empty():
                try:
                    ml = ml_queue.get_nowait()
                except queue.Empty:
                    break
            if ml is not None:
                try:
                    session.post(f"{CLOUD_URL}/push/ml",
                                 json=ml,
                                 timeout=5)
                    last_ml_push = now
                except Exception as e:
                    print(f"[CLOUD] ML push failed: {e}")

        # ── Push status every 5s ────────────────────────────────
        if now - last_status_push >= 5.0:
            with camera_status_lock:
                status = camera_status
            try:
                session.post(f"{CLOUD_URL}/push/status",
                             json={"status": status},
                             timeout=5)
                last_status_push = now
            except Exception:
                pass

        # ── Push GPS every 5s ───────────────────────────────────
        if now - last_gps_push >= 5.0:
            gps = None
            while not gps_queue.empty():
                try:
                    gps = gps_queue.get_nowait()
                except queue.Empty:
                    break
            if gps:
                try:
                    session.post(f"{CLOUD_URL}/push/gps",
                                 json=gps,
                                 timeout=5)
                    last_gps_push = now
                except Exception:
                    pass

        time.sleep(0.1)

# ------------------------ GPS WORKER ------------------------
def gps_worker():
    def push_gps_local(lat, lon, speed):
        with gps_state_lock:
            gps_state.update({"lat": lat, "lon": lon, "speed": speed})
        try:
            gps_queue.put_nowait({"lat": lat, "lon": lon, "speed": speed})
        except queue.Full:
            pass

    try:
        import serial, pynmea2
        print(f"[GPS] Hardware GPS — reading from {GPS_PORT}")
        while True:
            try:
                with serial.Serial(GPS_PORT, GPS_BAUD, timeout=1) as ser:
                    while True:
                        line = ser.readline().decode('ascii', errors='replace').strip()
                        if line.startswith('$GPRMC') or line.startswith('$GNRMC'):
                            try:
                                msg = pynmea2.parse(line)
                                if msg.status == 'A':
                                    push_gps_local(float(msg.latitude),
                                                   float(msg.longitude),
                                                   float(msg.spd_over_grnd) * 1.852)
                            except Exception:
                                pass
            except Exception as e:
                print(f"[GPS] Serial error: {e}, retrying in 10s...")
                time.sleep(10)

    except ImportError:
        if STATIC_LAT and STATIC_LON:
            print(f"[GPS] Static location: {STATIC_LAT}, {STATIC_LON}")
            while True:
                push_gps_local(STATIC_LAT, STATIC_LON, 0)
                time.sleep(30)
        else:
            print("[GPS] No GPS configured — set STATIC_LAT/STATIC_LON or install pyserial pynmea2")

# ------------------------ FRAME GENERATOR (LOCAL STREAM) ----
def generate_frames():
    frame_ready.wait(timeout=15)
    last_frame  = None
    stall_count = 0

    while True:
        with overlay_lock:
            frame = latest_overlay_frame
        if frame is None:
            with frame_lock:
                frame = latest_frame

        if frame is None or frame is last_frame:
            stall_count += 1
            if stall_count > 450:
                return
            time.sleep(0.033)
            continue

        stall_count = 0
        last_frame  = frame
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

# ------------------------ LOCAL PI DISPLAY ------------------------
def local_display_thread():
    try:
        import tkinter as tk
    except ImportError:
        print("tkinter not available, skipping local display")
        return
    try:
        root = tk.Tk()
    except Exception as e:
        print(f"No display available: {e}")
        return

    root.title("Pi ML Monitor")
    root.configure(bg='black')
    root.geometry("520x420")
    root.attributes('-topmost', True)

    tk.Label(root, text="ACCIDENT DETECTION MONITOR",
             bg='black', fg='lime', font=('Courier', 14, 'bold')).pack(pady=8)

    result_label = tk.Label(root, text="Waiting for detections...",
                            bg='black', fg='lime', font=('Courier', 12),
                            wraplength=500, justify='left')
    result_label.pack(pady=4, padx=10)

    acc_label = tk.Label(root, text="", bg='black', fg='yellow',
                         font=('Courier', 18, 'bold'))
    acc_label.pack(pady=4)

    status_label = tk.Label(root, text="ML Detection: OFF",
                            bg='black', fg='gray', font=('Courier', 10))
    status_label.pack(pady=4)

    cam_label = tk.Label(root, text="", bg='black', fg='cyan',
                         font=('Courier', 9), wraplength=500)
    cam_label.pack(pady=2, padx=10)

    gps_label = tk.Label(root, text="GPS: waiting...",
                         bg='black', fg='#0f0', font=('Courier', 9), wraplength=500)
    gps_label.pack(pady=2, padx=10)

    cloud_label = tk.Label(root, text="", bg='black', fg='#0af',
                           font=('Courier', 9))
    cloud_label.pack(pady=2, padx=10)

    def refresh():
        with camera_status_lock:
            cam_label.config(text=f"Camera: {camera_status}")
        with gps_state_lock:
            g = gps_state.copy()
        if g['lat'] and g['lon']:
            spd = f"{g['speed']:.1f} km/h" if g['speed'] is not None else "—"
            gps_label.config(text=f"GPS: {g['lat']:.5f}, {g['lon']:.5f} | {spd}", fg='#0f0')
        else:
            gps_label.config(text="GPS: No fix yet", fg='#555')
        cloud_label.config(text=f"Cloud: {'ON → ' + CLOUD_URL if CLOUD_ENABLED else 'OFF'}")

        if ml_detection_enabled:
            status_label.config(text="ML Detection: ON", fg='lime')
            with ml_lock:
                boxes = ml_results.copy()
            if not boxes:
                result_label.config(text="No detections", fg='lime')
                acc_label.config(text="")
            else:
                lines = [f"  {b['label']}  |  {b['side']}  |  {b['conf']*100:.1f}%" for b in boxes]
                result_label.config(text='\n'.join(lines), fg='white')
                best = max(boxes, key=lambda x: x['conf'])
                acc_label.config(
                    text=f"HIGHEST: {best['conf']*100:.1f}%",
                    fg='red' if best['conf'] >= 0.7 else 'orange'
                )
        else:
            status_label.config(text="ML Detection: OFF", fg='gray')
            result_label.config(text="Enable ML detection to begin", fg='gray')
            acc_label.config(text="")

        root.after(500, refresh)

    root.after(500, refresh)
    root.mainloop()

# ------------------------ FLASK ROUTES ------------------------
@app.route('/stream')
def stream():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snapshot.jpg')
def snapshot():
    with overlay_lock:
        frame = latest_overlay_frame
    if frame is None:
        with frame_lock:
            frame = latest_frame
    if frame is None:
        return "No frame available", 503
    return Response(frame, mimetype='image/jpeg')

@app.route('/detection', methods=['GET', 'POST'])
def detection():
    global color_detection_enabled, detection_mode
    if request.method == 'POST':
        data = request.get_json()
        color_detection_enabled = data.get('enabled', False)
        detection_mode = data.get('mode', 'center')
        return jsonify({'success': True, 'enabled': color_detection_enabled, 'mode': detection_mode})
    return jsonify({'enabled': color_detection_enabled, 'mode': detection_mode})

@app.route('/ml', methods=['GET', 'POST'])
def ml_toggle():
    global ml_detection_enabled
    if request.method == 'POST':
        data = request.get_json()
        ml_detection_enabled = data.get('enabled', False)
        return jsonify({'success': True, 'enabled': ml_detection_enabled})
    return jsonify({'enabled': ml_detection_enabled})

@app.route('/colors')
def get_colors():
    with detection_lock:
        return jsonify({'colors': detected_colors})

@app.route('/ml_results')
def get_ml_results():
    with ml_lock:
        return jsonify({'ml_results': ml_results})

@app.route('/status')
def get_status():
    with camera_status_lock:
        return jsonify({'camera_status': camera_status})

@app.route('/gps')
def get_gps():
    with gps_state_lock:
        return jsonify(gps_state)

@app.route('/')
def index():
    html = '''<!DOCTYPE html>
<html>
<head>
<title>Pi Camera + ML (Local)</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #000; color: #0f0; font-family: monospace; text-align: center; padding: 10px; }
  h1 { font-size: 18px; margin: 10px 0; }
  #stream { border: 2px solid #0f0; max-width: 100%; width: 640px; display: block; margin: 0 auto 10px; }
  .btn { background: #111; color: #0f0; border: 2px solid #0f0; padding: 10px;
         margin: 5px; cursor: pointer; font-family: monospace; }
  .btn:hover, .btn.active { background: #0f0; color: #000; }
  #status    { margin-top: 10px; font-size: 14px; min-height: 24px; }
  #camstatus { font-size: 11px; color: #0aa; margin-top: 4px; }
  #reconnect-msg { color: #f80; font-size: 12px; min-height: 18px; }
  #gpsbox { border: 1px solid #0a0; background: #001a00; margin: 12px auto;
            width: 640px; max-width: 100%; padding: 10px; text-align: left; }
  #gpsbox h2 { font-size: 13px; margin-bottom: 8px; }
  .grow { display: flex; justify-content: space-between; font-size: 12px; margin: 3px 0; }
  .glabel { color: #0a0; } .gval { color: #fff; font-weight: bold; }
  #map { width: 640px; max-width: 100%; height: 280px; margin: 8px auto;
         border: 1px solid #0a0; display: none; }
  #maplink { font-size: 11px; color: #0af; display: none; margin: 4px auto; }
  #noloc { font-size: 12px; color: #555; padding: 4px 0; }
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
</head>
<body>
<h1>Pi Camera + Accident Detection (Local)</h1>
<img id="stream" src="/stream">
<div id="reconnect-msg"></div>
<br>
<button class="btn" id="btnColor" onclick="toggleDetection()">Toggle Color Detection</button>
<button class="btn" id="btnML"    onclick="toggleML()">Toggle ML Detection</button>
<div id="status">Status: Ready</div>
<div id="camstatus">Camera: -</div>

<div id="gpsbox">
  <h2>📍 GPS Location</h2>
  <div class="grow"><span class="glabel">Latitude:</span>  <span class="gval" id="glat">—</span></div>
  <div class="grow"><span class="glabel">Longitude:</span> <span class="gval" id="glon">—</span></div>
  <div class="grow"><span class="glabel">Speed:</span>     <span class="gval" id="gspd">—</span></div>
  <div id="noloc">Waiting for GPS...</div>
</div>
<div id="map"></div>
<a id="maplink" href="#" target="_blank">📌 Open in Google Maps</a>

<script>
let colorEnabled=false, mlEnabled=false, mlInterval=null, lastStatus='', streamRetries=0;
let map=null, marker=null;

function toggleDetection() {
  colorEnabled=!colorEnabled;
  fetch('/detection',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:colorEnabled,mode:'center'})});
  document.getElementById('btnColor').classList.toggle('active',colorEnabled);
  document.getElementById('status').innerText='Color Detection: '+(colorEnabled?'ON':'OFF');
}
function toggleML() {
  mlEnabled=!mlEnabled;
  fetch('/ml',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:mlEnabled})});
  document.getElementById('btnML').classList.toggle('active',mlEnabled);
  document.getElementById('status').innerText='ML Detection: '+(mlEnabled?'ON':'OFF');
  if(mlInterval) clearInterval(mlInterval);
  if(mlEnabled) mlInterval=setInterval(updateML,1000);
}
function updateML() {
  fetch('/ml_results').then(r=>r.json()).then(d=>{
    let t='ML: ';
    if(!d.ml_results.length) t+='No detections';
    else d.ml_results.forEach(m=>{
      t+=m.label+' ('+m.side+') — '+(m.conf*100).toFixed(1)+'% | ';
    });
    t=t.replace(/ \| $/,'');
    if(t!==lastStatus){lastStatus=t;document.getElementById('status').innerText=t;}
  }).catch(()=>{document.getElementById('status').innerText='ML: Connection error';});
}
setInterval(()=>{
  fetch('/status').then(r=>r.json()).then(d=>{
    document.getElementById('camstatus').innerText='Camera: '+d.camera_status;
  }).catch(()=>{});
},2000);
function updateGPS(){
  fetch('/gps').then(r=>r.json()).then(d=>{
    if(d.lat&&d.lon){
      document.getElementById('noloc').style.display='none';
      document.getElementById('glat').innerText=d.lat.toFixed(6)+'°';
      document.getElementById('glon').innerText=d.lon.toFixed(6)+'°';
      document.getElementById('gspd').innerText=d.speed!=null?d.speed.toFixed(1)+' km/h':'—';
      const ml=document.getElementById('maplink');
      ml.style.display='block';
      ml.href='https://maps.google.com/?q='+d.lat+','+d.lon;
      const mapEl=document.getElementById('map');
      mapEl.style.display='block';
      if(!map){
        map=L.map('map').setView([d.lat,d.lon],16);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
          {attribution:'© OpenStreetMap'}).addTo(map);
        marker=L.marker([d.lat,d.lon]).addTo(map).bindPopup('Pi Location').openPopup();
      } else { marker.setLatLng([d.lat,d.lon]); map.setView([d.lat,d.lon]); }
    }
  }).catch(()=>{});
}
setInterval(updateGPS,3000); updateGPS();

const streamEl=document.getElementById('stream');
streamEl.onerror=function(){
  streamRetries++;
  const delay=Math.min(10000,streamRetries*1500);
  document.getElementById('reconnect-msg').innerText='Stream lost. Reconnecting in '+(delay/1000).toFixed(1)+'s...';
  setTimeout(()=>{streamEl.src='/stream?t='+Date.now();document.getElementById('reconnect-msg').innerText='';},delay);
};
streamEl.onload=function(){streamRetries=0;document.getElementById('reconnect-msg').innerText='';};
</script>
</body>
</html>'''
    return render_template_string(html)

# ------------------------ MAIN ------------------------
if __name__ == "__main__":
    print("Loading ML model...")
    model = YOLO("accident_model2.pt")
    print("Model loaded!")

    ports = [5000, 5001, 8000, 8080]
    selected_port = next((p for p in ports if check_port(p)), None)
    if not selected_port:
        print("No port available!")
        exit(1)

    local_ip = get_local_ip()

    threading.Thread(target=camera_thread,        daemon=True).start()
    threading.Thread(target=overlay_worker,       daemon=True).start()
    threading.Thread(target=ml_worker,            daemon=True).start()
    threading.Thread(target=local_display_thread, daemon=True).start()
    threading.Thread(target=cloud_sender,         daemon=True).start()  # single cloud thread
    threading.Thread(target=gps_worker,           daemon=True).start()

    frame_ready.wait(timeout=15)

    import logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    print(f"\n{'='*50}")
    print(f"  Pi Camera Server running")
    print(f"  Local stream:   http://{local_ip}:{selected_port}/stream")
    print(f"  Local UI:       http://{local_ip}:{selected_port}/")
    print(f"  Cloud UI:       {CLOUD_URL}/")
    print(f"  Cloud pushing:  {'ENABLED' if CLOUD_ENABLED else 'DISABLED'}")
    print(f"{'='*50}\n")

    app.run(host='0.0.0.0', port=selected_port, threaded=True)

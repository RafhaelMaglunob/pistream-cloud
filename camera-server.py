#!/usr/bin/env python3
from flask import Flask, Response, request, jsonify, render_template_string
import threading, time, socket, io, subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import colorsys
import torch
from ultralytics import YOLO
import requests as req_lib

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# ------------------------ CLOUD CONFIG ------------------------
# ⚠️  CHANGE THESE to match your Render deployment
CLOUD_URL    = "https://YOUR-APP-NAME.onrender.com"  # ← your Render URL
PUSH_SECRET  = "changeme123"                          # ← must match render.yaml PUSH_SECRET
CLOUD_ENABLED = True                                  # ← set False to disable cloud push

PUSH_HEADERS_FRAME = {"X-Secret": PUSH_SECRET, "Content-Type": "image/jpeg"}
PUSH_HEADERS_JSON  = {"X-Secret": PUSH_SECRET, "Content-Type": "application/json"}

# ------------------------ GLOBALS ------------------------
latest_frame = None
first_frame = None
frame_lock = threading.Lock()
frame_ready = threading.Event()

latest_overlay_frame = None
overlay_lock = threading.Lock()

color_detection_enabled = False
detection_mode = 'center'
detected_colors = []
detection_lock = threading.Lock()

ml_detection_enabled = False
ml_lock = threading.Lock()
ml_results = []
model = None

camera_status = "Starting..."
camera_status_lock = threading.Lock()

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
CRASH_LABEL = 'motor_crash'
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
                cx = (x1+x2)//2
                side = 'Left' if cx < w/3 else ('Center' if cx < w*2/3 else 'Right')
                boxes.append({
                    'box': [x1, y1, x2, y2],
                    'label': label,
                    'conf': float(conf),
                    'side': side
                })
        with ml_lock:
            ml_results = boxes
    except Exception as e:
        print(f"ML detection error: {e}")

# ------------------------ OVERLAY ------------------------
def add_overlay(frame_bytes, mode='center'):
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        draw = ImageDraw.Draw(img, 'RGBA')
        font = ImageFont.load_default()

        with detection_lock:
            colors = detected_colors.copy()
        for color in colors:
            x, y = color['coords']
            if mode == 'center':
                draw.line([(x-20, y), (x+20, y)], fill=(0, 255, 0, 255), width=2)
                draw.line([(x, y-20), (x, y+20)], fill=(0, 255, 0, 255), width=2)
            elif mode == 'grid':
                draw.ellipse([x-10, y-10, x+10, y+10],
                             fill=(color['r'], color['g'], color['b'], 255))

        with ml_lock:
            boxes = ml_results.copy()
        for b in boxes:
            x1, y1, x2, y2 = b['box']
            draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0, 255), width=2)
            text = f"{b['label']} {b['conf']*100:.0f}% {b['side']}"
            draw.text((x1, max(0, y1-10)), text, fill=(255, 0, 0, 255), font=font)

        out = io.BytesIO()
        img.save(out, format='JPEG', quality=85)
        return out.getvalue()
    except Exception as e:
        print(f"Overlay error: {e}")
        return frame_bytes

# ------------------------ CAMERA THREAD ------------------------
def kill_existing_camera():
    """Kill any existing rpicam-vid processes to free the camera device."""
    try:
        subprocess.run(['pkill', '-f', 'rpicam-vid'], capture_output=True)
        time.sleep(1)
    except Exception:
        pass

def camera_thread():
    global latest_frame, first_frame, latest_overlay_frame

    restart_delay = 2
    max_delay = 15
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
                    set_camera_status("Buffer overflow (4MB), resetting buffer...")
                    buffer = b''
                    continue

                while True:
                    soi = buffer.find(SOI)
                    if soi == -1:
                        break
                    eoi = buffer.find(EOI, soi + 2)
                    if eoi == -1:
                        break

                    frame = buffer[soi:eoi + 2]
                    buffer = buffer[eoi + 2:]

                    if len(frame) < 1024:
                        print(f"[CAMERA] Skipping tiny frame ({len(frame)} bytes)")
                        continue

                    last_frame_time = time.time()
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
            set_camera_status("ERROR: rpicam-vid not found! Is libcamera installed?")
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
                    try:
                        process.kill()
                    except Exception:
                        pass

        if frames_captured == 0:
            consecutive_failures += 1
        else:
            consecutive_failures = 0
            restart_delay = 2

        restart_delay = min(max_delay, 2 * (consecutive_failures + 1))
        set_camera_status(f"Restarting camera in {restart_delay}s (failures: {consecutive_failures})...")
        time.sleep(restart_delay)

# ------------------------ DETECTION WORKERS ------------------------
def overlay_worker():
    """Runs at ~15fps — only does color detection + overlay rendering."""
    global latest_overlay_frame
    last_processed = None
    color_skip = 0

    while True:
        time.sleep(0.066)

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

def ml_worker():
    """Runs ML inference in its own thread so it never stalls the stream."""
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

# ------------------------ CLOUD PUSH WORKERS ------------------------
def cloud_frame_worker():
    """Pushes overlay frames to Render cloud at ~2fps."""
    if not CLOUD_ENABLED:
        return
    last_pushed = None
    print(f"[CLOUD] Frame pusher started → {CLOUD_URL}")

    while True:
        time.sleep(0.5)  # 2fps — friendly for free tier

        with overlay_lock:
            frame = latest_overlay_frame
        if frame is None:
            with frame_lock:
                frame = latest_frame
        if frame is None or frame is last_pushed:
            continue
        last_pushed = frame

        try:
            r = req_lib.post(
                f"{CLOUD_URL}/push/frame",
                data=frame,
                headers=PUSH_HEADERS_FRAME,
                timeout=3
            )
            if r.status_code == 401:
                print("[CLOUD] ❌ Wrong PUSH_SECRET — check CLOUD CONFIG at top of file!")
        except req_lib.exceptions.ConnectionError:
            print("[CLOUD] ⚠️  Render unreachable (may be waking up, retrying...)")
        except req_lib.exceptions.Timeout:
            print("[CLOUD] ⚠️  Frame push timed out")
        except Exception as e:
            print(f"[CLOUD] Frame error: {e}")


def cloud_ml_worker():
    """Pushes ML detection results to Render every second."""
    if not CLOUD_ENABLED:
        return
    last_pushed = None

    while True:
        time.sleep(1)

        with ml_lock:
            results = ml_results.copy()

        if results == last_pushed:
            continue
        last_pushed = results

        try:
            req_lib.post(
                f"{CLOUD_URL}/push/ml",
                json=results,
                headers=PUSH_HEADERS_JSON,
                timeout=3
            )
        except Exception as e:
            print(f"[CLOUD] ML push error: {e}")


def cloud_status_worker():
    """Pushes camera status to Render every 3 seconds."""
    if not CLOUD_ENABLED:
        return

    while True:
        time.sleep(3)

        with camera_status_lock:
            status = camera_status

        try:
            req_lib.post(
                f"{CLOUD_URL}/push/status",
                json={"status": status},
                headers=PUSH_HEADERS_JSON,
                timeout=3
            )
        except Exception as e:
            print(f"[CLOUD] Status push error: {e}")

# ------------------------ FRAME GENERATOR ------------------------
def generate_frames():
    frame_ready.wait(timeout=15)
    last_frame = None
    stall_count = 0

    while True:
        with overlay_lock:
            frame = latest_overlay_frame
        if frame is None:
            with frame_lock:
                frame = latest_frame

        if frame is None or frame is last_frame:
            stall_count += 1
            if stall_count > 450:  # ~15s stall → drop dead client
                return
            if stall_count % 30 == 0:
                print(f"[STREAM] Stalled for ~{stall_count * 0.033:.1f}s waiting for new frame...")
            time.sleep(0.033)
            continue

        stall_count = 0
        last_frame = frame
        yield (
            b'--frame\r\n'
            b'Content-Type: image/jpeg\r\n\r\n'
            + frame +
            b'\r\n'
        )

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
        print(f"No display available for local monitor: {e}")
        return

    root.title("Pi ML Monitor")
    root.configure(bg='black')
    root.geometry("520x360")
    root.attributes('-topmost', True)

    tk.Label(root, text="ACCIDENT DETECTION MONITOR",
             bg='black', fg='lime', font=('Courier', 14, 'bold')).pack(pady=10)

    result_label = tk.Label(root, text="Waiting for detections...",
                            bg='black', fg='lime', font=('Courier', 12),
                            wraplength=500, justify='left')
    result_label.pack(pady=5, padx=10)

    acc_label = tk.Label(root, text="",
                         bg='black', fg='yellow', font=('Courier', 18, 'bold'))
    acc_label.pack(pady=5)

    status_label = tk.Label(root, text="ML Detection: OFF",
                            bg='black', fg='gray', font=('Courier', 10))
    status_label.pack(pady=5)

    cam_label = tk.Label(root, text="",
                         bg='black', fg='cyan', font=('Courier', 9),
                         wraplength=500)
    cam_label.pack(pady=2, padx=10)

    cloud_label = tk.Label(root, text="",
                           bg='black', fg='#0af', font=('Courier', 9))
    cloud_label.pack(pady=2, padx=10)

    def refresh():
        with camera_status_lock:
            cam_label.config(text=f"Camera: {camera_status}")

        cloud_label.config(
            text=f"Cloud: {'PUSHING → ' + CLOUD_URL if CLOUD_ENABLED else 'DISABLED'}"
        )

        if ml_detection_enabled:
            status_label.config(text="ML Detection: ON", fg='lime')
            with ml_lock:
                boxes = ml_results.copy()
            if not boxes:
                result_label.config(text="No detections", fg='lime')
                acc_label.config(text="")
            else:
                lines = []
                for b in boxes:
                    acc = b['conf'] * 100
                    lines.append(f"  {b['label']}  |  {b['side']}  |  Accuracy: {acc:.1f}%")
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
    return Response(
        generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

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

@app.route('/')
def index():
    html = '''<!DOCTYPE html>
<html>
<head>
<title>Pi Camera + ML</title>
<style>
  body { background: #000; color: #0f0; font-family: monospace; text-align: center; }
  #stream { border: 2px solid #0f0; width: 640px; }
  .btn { background: #111; color: #0f0; border: 2px solid #0f0; padding: 10px; margin: 5px; cursor: pointer; }
  .btn:hover { background: #0f0; color: #000; }
  .btn.active { background: #0f0; color: #000; }
  #status { margin-top: 10px; font-size: 14px; min-height: 24px; }
  #camstatus { font-size: 11px; color: #0aa; margin-top: 4px; }
  #fps { font-size: 11px; color: #555; margin-top: 4px; }
  #reconnect-msg { color: #f80; font-size: 12px; min-height: 18px; }
</style>
</head>
<body>
<h1>Pi Camera + Accident Detection (Local)</h1>
<img id="stream" src="/stream"><br>
<div id="reconnect-msg"></div>
<button class="btn" id="btnColor" onclick="toggleDetection()">Toggle Color Detection</button>
<button class="btn" id="btnML" onclick="toggleML()">Toggle ML Detection</button>
<div id="status">Status: Ready</div>
<div id="camstatus">Camera: -</div>
<div id="fps"></div>
<script>
let colorEnabled = false;
let mlEnabled = false;
let mlInterval = null;
let statusInterval = null;
let lastStatus = '';
let streamRetries = 0;

function toggleDetection() {
    colorEnabled = !colorEnabled;
    fetch('/detection', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: colorEnabled, mode: 'center' })
    });
    document.getElementById('btnColor').classList.toggle('active', colorEnabled);
    document.getElementById('status').innerText = 'Color Detection: ' + (colorEnabled ? 'ON' : 'OFF');
}

function toggleML() {
    mlEnabled = !mlEnabled;
    fetch('/ml', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: mlEnabled })
    });
    document.getElementById('btnML').classList.toggle('active', mlEnabled);
    document.getElementById('status').innerText = 'ML Detection: ' + (mlEnabled ? 'ON' : 'OFF');
    if (mlInterval) clearInterval(mlInterval);
    if (mlEnabled) {
        mlInterval = setInterval(updateML, 1000);
    } else {
        lastStatus = '';
        document.getElementById('fps').innerText = '';
    }
}

function updateML() {
    fetch('/ml_results').then(r => r.json()).then(data => {
        let text = 'ML: ';
        if (data.ml_results.length === 0) {
            text += 'No detections';
        } else {
            data.ml_results.forEach(m => {
                let acc = (m.conf * 100).toFixed(1);
                text += m.label + ' (' + m.side + ') — Accuracy: ' + acc + '% | ';
            });
            text = text.slice(0, -3);
        }
        if (text !== lastStatus) {
            lastStatus = text;
            document.getElementById('status').innerText = text;
        }
    }).catch(() => {
        document.getElementById('status').innerText = 'ML: Connection error';
    });
}

function updateCamStatus() {
    fetch('/status').then(r => r.json()).then(data => {
        document.getElementById('camstatus').innerText = 'Camera: ' + data.camera_status;
    }).catch(() => {});
}

const streamEl = document.getElementById('stream');
streamEl.onerror = function() {
    streamRetries++;
    const delay = Math.min(10000, streamRetries * 1500);
    document.getElementById('reconnect-msg').innerText =
        'Stream lost. Reconnecting in ' + (delay/1000).toFixed(1) + 's... (attempt ' + streamRetries + ')';
    setTimeout(() => {
        streamEl.src = '/stream?t=' + Date.now();
        document.getElementById('reconnect-msg').innerText = 'Reconnecting...';
    }, delay);
};

streamEl.onload = function() {
    streamRetries = 0;
    document.getElementById('reconnect-msg').innerText = '';
};

statusInterval = setInterval(updateCamStatus, 2000);
updateCamStatus();
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

    # Local workers
    threading.Thread(target=camera_thread,      daemon=True).start()
    threading.Thread(target=overlay_worker,     daemon=True).start()
    threading.Thread(target=ml_worker,          daemon=True).start()
    threading.Thread(target=local_display_thread, daemon=True).start()

    # Cloud push workers
    threading.Thread(target=cloud_frame_worker,  daemon=True).start()
    threading.Thread(target=cloud_ml_worker,     daemon=True).start()
    threading.Thread(target=cloud_status_worker, daemon=True).start()

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

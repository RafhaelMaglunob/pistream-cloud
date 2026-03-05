#!/usr/bin/env python3
"""
Pi Dual CSI Camera — Lite 360 Style with Crash Confirmation
Compatible with Firebase-enabled Railway server
"""

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

# ─────────────────── CLOUD CONFIG ───────────────────
CLOUD_URL     = "https://pi"  # Update to your Railway URL
PUSH_SECRET   = "Rafhael@1"  # Must match server SECRET_KEY
CLOUD_ENABLED = True

# ─────────────────── GPS CONFIG ─────────────────────
STATIC_LAT  = 14.5995  # Example: Manila
STATIC_LON  = 120.9842
GPS_PORT    = "/dev/ttyAMA0"
GPS_BAUD    = 9600
GPS_ENABLED = False   # Set True only if you have physical GPS module

# ─────────────────── CAMERA CONFIG ──────────────────
CAM_WIDTH  = 640
CAM_HEIGHT = 480
CAM_FPS    = 15
COMBINED_W = 1280
COMBINED_H = 480

# ─────────────────── CRASH CONFIRMATION CONFIG ───────
CRASH_LABEL           = 'motor_crash'
CRASH_CONF_THRESHOLD  = 0.90
CRASH_CONFIRM_SECONDS = 3      # seconds detection must be held
CRASH_COOLDOWN_SECONDS = 10    # after confirmed, suppress re-alerts

# ─────────────────── GLOBALS ────────────────────────
cameras = {
    0: {"label": "FRONT", "latest_frame": None, "overlay_frame": None,
        "status": "Starting...", "frame_lock": threading.Lock(),
        "overlay_lock": threading.Lock(), "status_lock": threading.Lock(),
        "frame_ready": threading.Event()},
    1: {"label": "REAR",  "latest_frame": None, "overlay_frame": None,
        "status": "Starting...", "frame_lock": threading.Lock(),
        "overlay_lock": threading.Lock(), "status_lock": threading.Lock(),
        "frame_ready": threading.Event()},
}

combined_frame      = None
combined_frame_lock = threading.Lock()
combined_ready      = threading.Event()

ml_detection_enabled = True
ml_lock    = threading.Lock()
ml_results = {0: [], 1: []}

# ── Confirmation state per camera ──────────────────
confirm_lock  = threading.Lock()
confirm_state = {
    0: {
        "first_seen":    None,
        "elapsed":       0.0,
        "confirmed":     False,
        "confirmed_at":  None,
        "cooldown_until": 0.0,
        "boxes":         [],
    },
    1: {
        "first_seen":    None,
        "elapsed":       0.0,
        "confirmed":     False,
        "confirmed_at":  None,
        "cooldown_until": 0.0,
        "boxes":         [],
    },
}

model      = None
gps_state      = {"lat": STATIC_LAT, "lon": STATIC_LON, "speed": 0}
gps_state_lock = threading.Lock()

# ─────────────────── UTILS ──────────────────────────
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "0.0.0.0"

def check_port(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', port)); sock.close(); return result != 0

def set_cam_status(cam_idx, msg):
    with cameras[cam_idx]["status_lock"]:
        cameras[cam_idx]["status"] = msg
    print(f"[CAM{cam_idx}] {msg}")

# ─────────────────── ML DETECTION + CONFIRMATION ────
def detect_accidents_in_frame(cam_idx, frame_bytes):
    """
    Run YOLO inference, update confirmation state machine.
    Per-camera state tracks:
      - first_seen: when continuous detection started
      - elapsed: how long detection has been held
      - confirmed: has it been held for CRASH_CONFIRM_SECONDS?
    """
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        results = model.predict(source=np.array(img), imgsz=320, conf=0.5, verbose=False)
        boxes = []
        w, h = img.size
        
        for r in results:
            if len(r.boxes) == 0: continue
            for box, conf, cls in zip(r.boxes.xyxy, r.boxes.conf, r.boxes.cls):
                label = model.names[int(cls)]
                if label != CRASH_LABEL: continue
                if float(conf) < CRASH_CONF_THRESHOLD: continue
                
                x1, y1, x2, y2 = map(int, box.tolist())
                cx = (x1 + x2) // 2
                side = 'Left' if cx < w/3 else ('Center' if cx < w*2/3 else 'Right')
                boxes.append({
                    'box': [x1, y1, x2, y2],
                    'label': label,
                    'conf': float(conf),
                    'side': side,
                    'cam': cam_idx
                })

        with ml_lock:
            ml_results[cam_idx] = boxes

        # ── Confirmation state machine ──────────────────
        now = time.time()
        with confirm_lock:
            cs = confirm_state[cam_idx]

            if boxes:
                # Detection still present or newly detected
                if cs["first_seen"] is None:
                    cs["first_seen"]   = now
                    cs["elapsed"]      = 0.0
                    cs["confirmed"]    = False
                    cs["confirmed_at"] = None

                cs["boxes"]   = boxes
                cs["elapsed"] = now - cs["first_seen"]

                # Check if we've crossed the confirmation threshold
                if (not cs["confirmed"]
                        and cs["elapsed"] >= CRASH_CONFIRM_SECONDS
                        and now >= cs["cooldown_until"]):
                    cs["confirmed"]    = True
                    cs["confirmed_at"] = now
                    cs["cooldown_until"] = now + CRASH_COOLDOWN_SECONDS
                    print(f"[CAM{cam_idx}] ✅ CRASH CONFIRMED after {cs['elapsed']:.1f}s")
            else:
                # No detection this frame → reset
                if cs["first_seen"] is not None:
                    held = now - cs["first_seen"]
                    if not cs["confirmed"]:
                        print(f"[CAM{cam_idx}] ❌ Detection cleared after {held:.1f}s — FALSE ALARM")
                
                cs["first_seen"]   = None
                cs["elapsed"]      = 0.0
                cs["confirmed"]    = False
                cs["confirmed_at"] = None
                cs["boxes"]        = []

    except Exception as e:
        print(f"[ML] Error cam{cam_idx}: {e}")

# ─────────────────── OVERLAY ─────────────────────────
def add_overlay(cam_idx, frame_bytes):
    """Add bounding boxes and confirmation progress to frame"""
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        draw = ImageDraw.Draw(img, 'RGBA')
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 14)
            sfont = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 11)
        except:
            font = ImageFont.load_default()
            sfont = font

        # Camera label
        badge_col = (0, 200, 255, 220) if cam_idx == 0 else (255, 100, 0, 220)
        draw.rectangle([4, 4, 100, 24], fill=badge_col)
        draw.text((8, 6), cameras[cam_idx]["label"], fill=(0, 0, 0, 255), font=font)

        # Get confirmation state and boxes
        with confirm_lock:
            cs = confirm_state[cam_idx].copy()
        with ml_lock:
            boxes = ml_results[cam_idx].copy()

        now = time.time()
        for b in boxes:
            x1, y1, x2, y2 = b['box']

            if cs["confirmed"] and cs["confirmed_at"] and \
               now - cs["confirmed_at"] < CRASH_COOLDOWN_SECONDS:
                # RED — confirmed crash
                box_color = (255, 0, 0, 255)
                text_bg = (200, 0, 0, 220)
                status_tag = "CONFIRMED"
            elif cs["first_seen"] is not None and not cs["confirmed"]:
                # YELLOW — accumulating
                pct = min(1.0, cs["elapsed"] / CRASH_CONFIRM_SECONDS)
                fill = int(pct * 255)
                box_color = (255, fill, 0, 255)
                text_bg = (160, 100, 0, 200)
                status_tag = f"VERIFYING {cs['elapsed']:.1f}/{CRASH_CONFIRM_SECONDS}s"
            else:
                continue

            draw.rectangle([x1, y1, x2, y2], outline=box_color, width=3)
            text = f"{b['label']} {b['conf']*100:.0f}% {b['side']} | {status_tag}"
            tw = len(text) * 7
            draw.rectangle([x1, max(0, y1-18), x1+tw, y1], fill=text_bg)
            draw.text((x1+2, max(0, y1-16)), text, fill=(255, 255, 255, 255), font=sfont)

        # Progress bar at bottom
        if cs["first_seen"] is not None and not cs["confirmed"]:
            pct = min(1.0, cs["elapsed"] / CRASH_CONFIRM_SECONDS)
            iw, ih = img.size
            bar_w = int(iw * pct)
            draw.rectangle([0, ih-6, iw, ih], fill=(40, 40, 40, 200))
            draw.rectangle([0, ih-6, bar_w, ih], fill=(255, int(255*(1-pct)), 0, 220))

        out = io.BytesIO()
        img.save(out, format='JPEG', quality=85)
        return out.getvalue()
    except Exception as e:
        print(f"[OVERLAY] Error cam{cam_idx}: {e}")
        return frame_bytes

# ─────────────────── COMBINE FRAMES ─────────────────
def combine_frames(front_bytes, rear_bytes):
    """Combine front and rear into single frame"""
    try:
        front = Image.open(io.BytesIO(front_bytes)).convert('RGB').resize((CAM_WIDTH, CAM_HEIGHT))
        rear = Image.open(io.BytesIO(rear_bytes)).convert('RGB').resize((CAM_WIDTH, CAM_HEIGHT))
        canvas = Image.new('RGB', (COMBINED_W, COMBINED_H + 28), (10, 10, 10))
        canvas.paste(front, (0, 28))
        canvas.paste(rear, (CAM_WIDTH, 28))
        
        draw = ImageDraw.Draw(canvas)
        try:
            hfont = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 16)
        except:
            hfont = ImageFont.load_default()
        
        draw.rectangle([0, 0, COMBINED_W, 28], fill=(10, 10, 10))
        draw.text((8, 6), "◀ FRONT", fill=(0, 200, 255), font=hfont)
        draw.text((CAM_WIDTH + 8, 6), "REAR ▶", fill=(255, 120, 0), font=hfont)
        draw.line([(CAM_WIDTH, 0), (CAM_WIDTH, COMBINED_H+28)], fill=(40, 40, 40), width=2)
        
        ts = time.strftime("%Y-%m-%d  %H:%M:%S")
        draw.text((COMBINED_W//2 - 80, 6), ts, fill=(180, 180, 180), font=hfont)
        
        out = io.BytesIO()
        canvas.save(out, format='JPEG', quality=82)
        return out.getvalue()
    except Exception as e:
        print(f"[COMBINE] Error: {e}")
        return front_bytes

# ─────────────────── CAMERA THREAD ──────────────────
def kill_existing_cameras():
    try:
        subprocess.run(['pkill', '-f', 'rpicam-vid'], capture_output=True)
        time.sleep(1)
    except:
        pass

def camera_thread(cam_idx):
    cam = cameras[cam_idx]
    consecutive_failures = 0
    
    while True:
        process = None
        frames_captured = 0
        set_cam_status(cam_idx, "Starting...")
        
        try:
            process = subprocess.Popen(
                ['rpicam-vid', '-t', '0',
                 '--camera', str(cam_idx),
                 '--width', str(CAM_WIDTH),
                 '--height', str(CAM_HEIGHT),
                 '--framerate', str(CAM_FPS),
                 '--codec', 'mjpeg', '--quality', '50',
                 '--inline', '--nopreview', '--denoise', 'off',
                 '--flush', '1', '-o', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            
            SOI = b'\xff\xd8'
            EOI = b'\xff\xd9'
            buffer = b''
            last_frame_time = time.time()
            set_cam_status(cam_idx, "Waiting for first frame...")
            
            while True:
                if time.time() - last_frame_time > 10:
                    set_cam_status(cam_idx, "Watchdog: no frame 10s, restarting...")
                    break
                
                try:
                    chunk = process.stdout.read(8192)
                except Exception as e:
                    set_cam_status(cam_idx, f"Read error: {e}")
                    break
                
                if not chunk:
                    err = process.stderr.read(2048).decode(errors='replace').strip()
                    set_cam_status(cam_idx, f"Process ended. {err or '(none)'}")
                    with cam["frame_lock"]:
                        cam["latest_frame"] = None
                    break
                
                buffer += chunk
                if len(buffer) > 4*1024*1024:
                    buffer = b''
                    continue
                
                while True:
                    soi = buffer.find(SOI)
                    if soi == -1: break
                    eoi = buffer.find(EOI, soi+2)
                    if eoi == -1: break
                    
                    frame = buffer[soi:eoi+2]
                    buffer = buffer[eoi+2:]
                    
                    if len(frame) < 1024: continue
                    
                    last_frame_time = time.time()
                    frames_captured += 1
                    
                    with cam["frame_lock"]:
                        cam["latest_frame"] = frame
                        if not cam["frame_ready"].is_set():
                            cam["frame_ready"].set()
                            set_cam_status(cam_idx, "Streaming!")
        
        except FileNotFoundError:
            set_cam_status(cam_idx, "ERROR: rpicam-vid not found!")
            time.sleep(10)
            continue
        except Exception as e:
            set_cam_status(cam_idx, f"Error: {e}")
        
        finally:
            if process:
                try:
                    process.terminate()
                    process.wait(timeout=3)
                except:
                    try: process.kill()
                    except: pass
        
        consecutive_failures = 0 if frames_captured > 0 else consecutive_failures + 1
        delay = min(15, 2 * (consecutive_failures + 1))
        set_cam_status(cam_idx, f"Restarting in {delay}s...")
        time.sleep(delay)

# ─────────────────── WORKERS ─────────────────────────
def overlay_worker():
    last = {0: None, 1: None}
    while True:
        time.sleep(0.033)
        
        for idx in (0, 1):
            cam = cameras[idx]
            with cam["frame_lock"]:
                frame = cam["latest_frame"]
            
            if frame is None or frame is last[idx]: continue
            last[idx] = frame
            
            rendered = add_overlay(idx, frame)
            with cam["overlay_lock"]:
                cam["overlay_frame"] = rendered
        
        def get_best(idx):
            with cameras[idx]["overlay_lock"]:
                f = cameras[idx]["overlay_frame"]
            if f is None:
                with cameras[idx]["frame_lock"]:
                    f = cameras[idx]["latest_frame"]
            return f
        
        f0 = get_best(0)
        f1 = get_best(1)
        
        if f0 and f1:
            combined = combine_frames(f0, f1)
            with combined_frame_lock:
                global combined_frame
                combined_frame = combined
            if not combined_ready.is_set():
                combined_ready.set()

def ml_worker():
    last = {0: None, 1: None}
    while True:
        time.sleep(0.033)
        
        if not ml_detection_enabled:
            with confirm_lock:
                for idx in (0, 1):
                    confirm_state[idx].update({
                        "first_seen": None, "elapsed": 0.0,
                        "confirmed": False, "confirmed_at": None, "boxes": []
                    })
            continue
        
        for idx in (0, 1):
            with cameras[idx]["frame_lock"]:
                frame = cameras[idx]["latest_frame"]
            
            if frame is None or frame is last[idx]: continue
            last[idx] = frame
            
            detect_accidents_in_frame(idx, frame)

def cloud_sender():
    if not CLOUD_ENABLED: return
    
    session = req_lib.Session()
    session.headers.update({"X-Secret": PUSH_SECRET})
    
    lf = ls = lg = lm = 0
    last_sent = None
    
    print(f"[CLOUD] Sender started → {CLOUD_URL}")
    
    while True:
        now = time.time()
        
        # Send combined frame every 1 second
        if now - lf >= 1.0:
            with combined_frame_lock:
                frame = combined_frame
            
            if frame and frame is not last_sent:
                try:
                    r = session.post(
                        f"{CLOUD_URL}/push/frame",
                        data=frame,
                        headers={"Content-Type": "image/jpeg"},
                        timeout=8
                    )
                    if r.status_code == 200:
                        last_sent = frame
                        lf = now
                    elif r.status_code == 401:
                        print("[CLOUD] ❌ Wrong secret")
                except Exception as e:
                    print(f"[CLOUD] Frame: {e}")
        
        # Send ML results every 500ms
        if now - lm >= 0.5:
            with confirm_lock:
                payload = {
                    str(idx): {
                        "confirmed": confirm_state[idx]["confirmed"],
                        "elapsed": round(confirm_state[idx]["elapsed"], 1),
                        "boxes": confirm_state[idx]["boxes"]
                    }
                    for idx in (0, 1)
                }
            
            try:
                session.post(f"{CLOUD_URL}/push/ml", json=payload, timeout=5)
                lm = now
            except Exception as e:
                pass
        
        # Send status every 10 seconds
        if now - ls >= 10.0:
            s = {str(idx): cameras[idx]["status"] for idx in (0, 1)}
            try:
                session.post(f"{CLOUD_URL}/push/status", json=s, timeout=5)
                ls = now
            except Exception:
                pass
        
        # Send GPS every 10 seconds
        if now - lg >= 10.0:
            with gps_state_lock:
                gps = gps_state.copy()
            
            if gps['lat'] and gps['lon']:
                try:
                    session.post(f"{CLOUD_URL}/push/gps", json=gps, timeout=5)
                    lg = now
                except Exception:
                    pass
        
        time.sleep(0.5)

def gps_worker():
    """GPS worker — either physical or static"""
    def push(lat, lon, speed):
        with gps_state_lock:
            gps_state.update({"lat": lat, "lon": lon, "speed": speed})
    
    try:
        if GPS_ENABLED:
            import serial
            import pynmea2
            
            while True:
                try:
                    with serial.Serial(GPS_PORT, GPS_BAUD, timeout=1) as ser:
                        while True:
                            line = ser.readline().decode('ascii', errors='replace').strip()
                            if line.startswith(('$GPRMC', '$GNRMC')):
                                try:
                                    msg = pynmea2.parse(line)
                                    if msg.status == 'A':
                                        push(float(msg.latitude), float(msg.longitude),
                                             float(msg.spd_over_grnd) * 1.852)
                                except:
                                    pass
                except Exception as e:
                    print(f"[GPS] {e}, retry 10s")
                    time.sleep(10)
        else:
            # Static GPS
            while True:
                push(STATIC_LAT, STATIC_LON, 0)
                time.sleep(30)
    except ImportError:
        print("[GPS] No serial/pynmea2 available")

# ─────────────────── ROUTES ─────────────────────────
@app.route('/stream')
def stream_combined():
    def generate():
        combined_ready.wait(timeout=20)
        last = None
        stall = 0
        while True:
            with combined_frame_lock:
                frame = combined_frame
            
            if frame is None or frame is last:
                stall += 1
                if stall > 450: return
                time.sleep(0.033)
                continue
            
            stall = 0
            last = frame
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
    
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/front')
def stream_front():
    def generate():
        cameras[0]["frame_ready"].wait(timeout=15)
        last = None
        stall = 0
        while True:
            with cameras[0]["overlay_lock"]:
                frame = cameras[0]["overlay_frame"]
            if frame is None:
                with cameras[0]["frame_lock"]:
                    frame = cameras[0]["latest_frame"]
            
            if frame is None or frame is last:
                stall += 1
                if stall > 450: return
                time.sleep(0.033)
                continue
            
            stall = 0
            last = frame
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
    
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/rear')
def stream_rear():
    def generate():
        cameras[1]["frame_ready"].wait(timeout=15)
        last = None
        stall = 0
        while True:
            with cameras[1]["overlay_lock"]:
                frame = cameras[1]["overlay_frame"]
            if frame is None:
                with cameras[1]["frame_lock"]:
                    frame = cameras[1]["latest_frame"]
            
            if frame is None or frame is last:
                stall += 1
                if stall > 450: return
                time.sleep(0.033)
                continue
            
            stall = 0
            last = frame
            yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame + b'\r\n'
    
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snapshot.jpg')
def snapshot():
    with combined_frame_lock:
        frame = combined_frame
    if frame is None:
        return "No frame", 503
    return Response(frame, mimetype='image/jpeg')

@app.route('/ml_results')
def get_ml_results():
    """Return confirmation state for each camera"""
    with confirm_lock:
        cs = {
            idx: {
                "confirmed": confirm_state[idx]["confirmed"],
                "elapsed": round(confirm_state[idx]["elapsed"], 2),
                "first_seen": confirm_state[idx]["first_seen"] is not None,
                "boxes": confirm_state[idx]["boxes"],
                "confirm_secs": CRASH_CONFIRM_SECONDS,
            }
            for idx in (0, 1)
        }
    return jsonify({
        "0": cs[0],
        "1": cs[1],
    })

@app.route('/status')
def get_status():
    return jsonify({
        '0': cameras[0]["status"],
        '1': cameras[1]["status"]
    })

@app.route('/gps')
def get_gps():
    with gps_state_lock:
        return jsonify(gps_state)

# ─────────────────── MAIN ───────────────────────────
if __name__ == "__main__":
    print("Loading ML model...")
    model = YOLO("accident_model_latest.pt")
    print("Model loaded!")

    ports = [5000, 5001, 8000, 8080]
    selected_port = next((p for p in ports if check_port(p)), None)
    
    if not selected_port:
        print("No port available!")
        exit(1)

    local_ip = get_local_ip()
    kill_existing_cameras()

    for idx in (0, 1):
        threading.Thread(target=camera_thread, args=(idx,), daemon=True).start()

    threading.Thread(target=overlay_worker, daemon=True).start()
    threading.Thread(target=ml_worker, daemon=True).start()
    threading.Thread(target=cloud_sender, daemon=True).start()
    threading.Thread(target=gps_worker, daemon=True).start()

    cameras[0]["frame_ready"].wait(timeout=15)
    cameras[1]["frame_ready"].wait(timeout=5)

    import logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    print(f"\n{'='*55}")
    print(f"  Pi Dual Camera with Crash Confirmation")
    print(f"  UI:         http://{local_ip}:{selected_port}/")
    print(f"  Combined:   http://{local_ip}:{selected_port}/stream")
    print(f"  Front:      http://{local_ip}:{selected_port}/stream/front")
    print(f"  Rear:       http://{local_ip}:{selected_port}/stream/rear")
    print(f"  Confirm:    {CRASH_CONFIRM_SECONDS}s hold required")
    print(f"  Cooldown:   {CRASH_COOLDOWN_SECONDS}s after confirmed")
    print(f"  Cloud:      {CLOUD_URL}")
    print(f"{'='*55}\n")

    app.run(host='0.0.0.0', port=selected_port, threaded=True)
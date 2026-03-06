#!/usr/bin/env python3
"""
Pi Dual CSI Camera — Lite 360 Style
Front cam: camera index 0
Rear cam:  camera index 1

CRASH CONFIRMATION: A detection must persist for CRASH_CONFIRM_SECONDS
consecutive seconds before it is treated as a real alert (not a false alarm).

SHARPNESS FIXES:
- rpicam-vid --sharpness 2.0 --contrast 1.1 --awb auto
- PIL LANCZOS resize (was default)
- CSS max-width fixed to 1280px (was 2560px causing stretch blur)
- Resolution 640x480 per cam, combined 1280x480
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
CLOUD_URL     = "https://pistream-cloud.onrender.com"
PUSH_SECRET   = "Rafhael@1"
CLOUD_ENABLED = True

# ─────────────────── GPS CONFIG ─────────────────────
STATIC_LAT  = None
STATIC_LON  = None
GPS_PORT    = "/dev/ttyAMA0"
GPS_BAUD    = 9600
GPS_ENABLED = False

# ─────────────────── CAMERA CONFIG ──────────────────
CAM_WIDTH  = 640
CAM_HEIGHT = 480
CAM_FPS    = 15
COMBINED_W = 1280   # 2 × CAM_WIDTH
COMBINED_H = 480

# ─────────────────── CRASH CONFIRMATION CONFIG ───────
CRASH_LABEL            = 'motor_crash'
CRASH_CONF_THRESHOLD   = 0.90
CRASH_CONFIRM_SECONDS  = 3
CRASH_COOLDOWN_SECONDS = 10

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

color_detection_enabled = False
detection_mode  = 'center'
detected_colors = {0: [], 1: []}
detection_lock  = threading.Lock()

ml_detection_enabled = True
ml_lock    = threading.Lock()
ml_results = {0: [], 1: []}

confirm_lock  = threading.Lock()
confirm_state = {
    0: {"first_seen": None, "elapsed": 0.0, "confirmed": False,
        "confirmed_at": None, "cooldown_until": 0.0, "boxes": []},
    1: {"first_seen": None, "elapsed": 0.0, "confirmed": False,
        "confirmed_at": None, "cooldown_until": 0.0, "boxes": []},
}

model      = None

gps_state      = {"lat": None, "lon": None, "speed": None}
gps_state_lock = threading.Lock()

ml_queue     = queue.Queue(maxsize=5)
status_queue = queue.Queue(maxsize=5)
gps_queue    = queue.Queue(maxsize=5)

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

def rgb_to_color_name(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
    h=h*360; s=s*100; v=v*100
    if s < 10:
        if v < 20: return "Black"
        elif v > 80: return "White"
        else: return "Gray"
    if v < 20: return "Black"
    if h < 15 or h >= 345: return "Red"
    elif h < 45: return "Orange"
    elif h < 75: return "Yellow"
    elif h < 155: return "Green"
    elif h < 185: return "Cyan"
    elif h < 250: return "Blue"
    elif h < 290: return "Purple"
    elif h < 345: return "Magenta"
    return "Unknown"

# ─────────────────── COLOR DETECTION ────────────────
def detect_colors_in_frame(cam_idx, frame_bytes, mode='center'):
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        arr = np.array(img)
        h, w = arr.shape[:2]
        colors = []
        if mode == 'center':
            cx, cy = w//2, h//2
            sample = arr[max(0,cy-30):cy+30, max(0,cx-30):cx+30]
            r, g, b = sample.mean(axis=(0,1)).astype(int)
            colors.append({'position':'center','rgb':f'rgb({r},{g},{b})',
                'rgba':f'rgba({r},{g},{b},1)','hex':f'#{r:02x}{g:02x}{b:02x}',
                'name':rgb_to_color_name(r,g,b),'coords':(cx,cy),
                'r':int(r),'g':int(g),'b':int(b)})
        elif mode == 'grid':
            for i in range(3):
                for j in range(3):
                    x=int(w*(j+0.5)/3); y=int(h*(i+0.5)/3)
                    sample=arr[max(0,y-20):y+20,max(0,x-20):x+20]
                    r,g,b=sample.mean(axis=(0,1)).astype(int)
                    colors.append({'position':f'grid_{i}_{j}','rgb':f'rgb({r},{g},{b})',
                        'rgba':f'rgba({r},{g},{b},1)','hex':f'#{r:02x}{g:02x}{b:02x}',
                        'name':rgb_to_color_name(r,g,b),'coords':(x,y),
                        'r':int(r),'g':int(g),'b':int(b)})
        with detection_lock:
            detected_colors[cam_idx] = colors
    except Exception as e:
        print(f"Color detection error cam{cam_idx}: {e}")

# ─────────────────── ML DETECTION + CONFIRMATION ────
def detect_accidents_in_frame(cam_idx, frame_bytes):
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        results = model.predict(source=np.array(img), imgsz=640, conf=0.5, verbose=False)
        boxes = []
        w, h = img.size
        for r in results:
            if len(r.boxes) == 0: continue
            for box, conf, cls in zip(r.boxes.xyxy, r.boxes.conf, r.boxes.cls):
                label = model.names[int(cls)]
                if label != CRASH_LABEL: continue
                if float(conf) < CRASH_CONF_THRESHOLD: continue
                x1,y1,x2,y2 = map(int, box.tolist())
                cx = (x1+x2)//2
                side = 'Left' if cx < w/3 else ('Center' if cx < w*2/3 else 'Right')
                boxes.append({'box':[x1,y1,x2,y2],'label':label,
                              'conf':float(conf),'side':side,'cam':cam_idx})

        with ml_lock:
            ml_results[cam_idx] = boxes

        now = time.time()
        with confirm_lock:
            cs = confirm_state[cam_idx]
            if boxes:
                if cs["first_seen"] is None:
                    cs["first_seen"]   = now
                    cs["elapsed"]      = 0.0
                    cs["confirmed"]    = False
                    cs["confirmed_at"] = None
                cs["boxes"]   = boxes
                cs["elapsed"] = now - cs["first_seen"]
                if (not cs["confirmed"]
                        and cs["elapsed"] >= CRASH_CONFIRM_SECONDS
                        and now >= cs["cooldown_until"]):
                    cs["confirmed"]      = True
                    cs["confirmed_at"]   = now
                    cs["cooldown_until"] = now + CRASH_COOLDOWN_SECONDS
                    print(f"[CAM{cam_idx}] ✅ CRASH CONFIRMED after {cs['elapsed']:.1f}s")
            else:
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
        print(f"ML detection error cam{cam_idx}: {e}")

# ─────────────────── OVERLAY ─────────────────────────
def add_overlay(cam_idx, frame_bytes, mode='center'):
    try:
        img  = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        draw = ImageDraw.Draw(img, 'RGBA')
        try:
            font  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 18)
            sfont = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 14)
        except Exception:
            font  = ImageFont.load_default()
            sfont = font

        badge_col = (0, 200, 255, 220) if cam_idx == 0 else (255, 100, 0, 220)
        draw.rectangle([4, 4, 120, 28], fill=badge_col)
        draw.text((8, 6), cameras[cam_idx]["label"], fill=(0,0,0,255), font=font)

        with detection_lock:
            colors = detected_colors[cam_idx].copy()
        for color in colors:
            x, y = color['coords']
            if mode == 'center':
                draw.line([(x-20,y),(x+20,y)], fill=(0,255,0,255), width=2)
                draw.line([(x,y-20),(x,y+20)], fill=(0,255,0,255), width=2)
            elif mode == 'grid':
                draw.ellipse([x-10,y-10,x+10,y+10],
                             fill=(color['r'],color['g'],color['b'],255))

        with confirm_lock:
            cs = confirm_state[cam_idx].copy()
        with ml_lock:
            boxes = ml_results[cam_idx].copy()

        now = time.time()
        for b in boxes:
            x1,y1,x2,y2 = b['box']
            if cs["confirmed"] and cs["confirmed_at"] and \
               now - cs["confirmed_at"] < CRASH_COOLDOWN_SECONDS:
                box_color  = (255, 0, 0, 255)
                text_bg    = (200, 0, 0, 220)
                status_tag = "CONFIRMED"
            elif cs["first_seen"] is not None and not cs["confirmed"]:
                pct  = min(1.0, cs["elapsed"] / CRASH_CONFIRM_SECONDS)
                fill = int(pct * 255)
                box_color  = (255, fill, 0, 255)
                text_bg    = (160, 100, 0, 200)
                status_tag = f"VERIFYING {cs['elapsed']:.1f}/{CRASH_CONFIRM_SECONDS}s"
            else:
                continue
            draw.rectangle([x1,y1,x2,y2], outline=box_color, width=3)
            text = f"{b['label']} {b['conf']*100:.0f}% {b['side']} | {status_tag}"
            tw   = len(text) * 8
            draw.rectangle([x1, max(0,y1-22), x1+tw, y1], fill=text_bg)
            draw.text((x1+2, max(0,y1-20)), text, fill=(255,255,255,255), font=sfont)

        if cs["first_seen"] is not None and not cs["confirmed"]:
            pct  = min(1.0, cs["elapsed"] / CRASH_CONFIRM_SECONDS)
            iw, ih = img.size
            bar_w = int(iw * pct)
            draw.rectangle([0, ih-8, iw, ih], fill=(40,40,40,200))
            draw.rectangle([0, ih-8, bar_w, ih], fill=(255, int(255*(1-pct)), 0, 220))

        out = io.BytesIO()
        img.save(out, format='JPEG', quality=92)
        return out.getvalue()
    except Exception as e:
        print(f"Overlay error cam{cam_idx}: {e}")
        return frame_bytes

# ─────────────────── COMBINE FRAMES ─────────────────
def combine_frames(front_bytes, rear_bytes):
    try:
        # ← LANCZOS for sharp resize (was default bilinear)
        front = Image.open(io.BytesIO(front_bytes)).convert('RGB').resize((CAM_WIDTH, CAM_HEIGHT), Image.LANCZOS)
        rear  = Image.open(io.BytesIO(rear_bytes)).convert('RGB').resize((CAM_WIDTH, CAM_HEIGHT), Image.LANCZOS)
        canvas = Image.new('RGB', (COMBINED_W, COMBINED_H + 32), (10, 10, 10))
        canvas.paste(front, (0, 32))
        canvas.paste(rear,  (CAM_WIDTH, 32))
        draw = ImageDraw.Draw(canvas)
        try:
            hfont = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 18)
        except Exception:
            hfont = ImageFont.load_default()
        draw.rectangle([0, 0, COMBINED_W, 32], fill=(10, 10, 10))
        draw.text((8, 8),            "◀ FRONT", fill=(0, 200, 255), font=hfont)
        draw.text((CAM_WIDTH + 8, 8),"REAR ▶",  fill=(255, 120, 0), font=hfont)
        draw.line([(CAM_WIDTH,0),(CAM_WIDTH,COMBINED_H+32)], fill=(40,40,40), width=2)
        ts = time.strftime("%Y-%m-%d  %H:%M:%S")
        draw.text((COMBINED_W//2 - 90, 8), ts, fill=(180,180,180), font=hfont)
        out = io.BytesIO()
        canvas.save(out, format='JPEG', quality=92)
        return out.getvalue()
    except Exception as e:
        print(f"Combine error: {e}"); return front_bytes

# ─────────────────── CAMERA THREAD ──────────────────
def kill_existing_cameras():
    try:
        subprocess.run(['pkill','-f','rpicam-vid'], capture_output=True)
        time.sleep(1)
    except Exception: pass

def camera_thread(cam_idx):
    cam = cameras[cam_idx]
    consecutive_failures = 0
    while True:
        process = None; frames_captured = 0
        set_cam_status(cam_idx, "Starting...")
        try:
            process = subprocess.Popen(
                ['rpicam-vid', '-t', '0',
                 '--camera',    str(cam_idx),
                 '--width',     str(CAM_WIDTH),
                 '--height',    str(CAM_HEIGHT),
                 '--framerate', str(CAM_FPS),
                 '--codec',     'mjpeg',
                 '--quality',   '90',
                 '--sharpness', '2.0',   # ← sharp!
                 '--contrast',  '1.1',   # ← pop
                 '--awb',       'auto',  # ← correct white balance
                 '--inline', '--nopreview', '--denoise', 'off',
                 '--flush', '1', '-o', '-'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            SOI=b'\xff\xd8'; EOI=b'\xff\xd9'; buffer=b''
            last_frame_time=time.time()
            set_cam_status(cam_idx,"Waiting for first frame...")
            while True:
                if time.time()-last_frame_time>10:
                    set_cam_status(cam_idx,"Watchdog: no frame 10s, restarting..."); break
                try: chunk=process.stdout.read(8192)
                except Exception as e: set_cam_status(cam_idx,f"Read error: {e}"); break
                if not chunk:
                    err=process.stderr.read(2048).decode(errors='replace').strip()
                    set_cam_status(cam_idx,f"Process ended. {err or '(none)'}");
                    with cam["frame_lock"]: cam["latest_frame"]=None; break
                buffer+=chunk
                if len(buffer)>4*1024*1024: buffer=b''; continue
                while True:
                    soi=buffer.find(SOI)
                    if soi==-1: break
                    eoi=buffer.find(EOI,soi+2)
                    if eoi==-1: break
                    frame=buffer[soi:eoi+2]; buffer=buffer[eoi+2:]
                    if len(frame)<1024: continue
                    last_frame_time=time.time(); frames_captured+=1
                    with cam["frame_lock"]:
                        cam["latest_frame"]=frame
                        if not cam["frame_ready"].is_set():
                            cam["frame_ready"].set()
                            set_cam_status(cam_idx,"Streaming!")
        except FileNotFoundError:
            set_cam_status(cam_idx,"ERROR: rpicam-vid not found!"); time.sleep(10); continue
        except Exception as e: set_cam_status(cam_idx,f"Error: {e}")
        finally:
            if process:
                try: process.terminate(); process.wait(timeout=3)
                except Exception:
                    try: process.kill()
                    except Exception: pass
        consecutive_failures = 0 if frames_captured>0 else consecutive_failures+1
        delay=min(15,2*(consecutive_failures+1))
        set_cam_status(cam_idx,f"Restarting in {delay}s...")
        time.sleep(delay)

# ─────────────────── WORKERS ─────────────────────────
def overlay_worker():
    last={0:None,1:None}; skip={0:0,1:0}
    while True:
        time.sleep(0.033)
        for idx in (0,1):
            cam=cameras[idx]
            with cam["frame_lock"]: frame=cam["latest_frame"]
            if frame is None or frame is last[idx]: continue
            last[idx]=frame
            if color_detection_enabled and skip[idx]%2==0:
                detect_colors_in_frame(idx,frame,detection_mode)
            skip[idx]+=1
            rendered=add_overlay(idx,frame,detection_mode)
            with cam["overlay_lock"]: cam["overlay_frame"]=rendered
        def get_best(idx):
            with cameras[idx]["overlay_lock"]: f=cameras[idx]["overlay_frame"]
            if f is None:
                with cameras[idx]["frame_lock"]: f=cameras[idx]["latest_frame"]
            return f
        f0=get_best(0); f1=get_best(1)
        if f0 and f1:
            combined=combine_frames(f0,f1)
            with combined_frame_lock:
                global combined_frame; combined_frame=combined
            if not combined_ready.is_set(): combined_ready.set()

def ml_worker():
    last={0:None,1:None}
    while True:
        time.sleep(0.033)
        if not ml_detection_enabled:
            with confirm_lock:
                for idx in (0,1):
                    confirm_state[idx].update({
                        "first_seen":None,"elapsed":0.0,
                        "confirmed":False,"confirmed_at":None,"boxes":[]})
            continue
        for idx in (0,1):
            with cameras[idx]["frame_lock"]: frame=cameras[idx]["latest_frame"]
            if frame is None or frame is last[idx]: continue
            last[idx]=frame
            detect_accidents_in_frame(idx,frame)

# ─────────────────── CLOUD SENDER ───────────────────
def cloud_sender():
    if not CLOUD_ENABLED: return
    session=req_lib.Session()
    session.headers.update({"X-Secret":PUSH_SECRET})
    lf=ls=lg=lm=0; last_sent=None
    print(f"[CLOUD] Sender started → {CLOUD_URL}")
    while True:
        now=time.time()
        if now-lf>=1.0:
            with combined_frame_lock: frame=combined_frame
            if frame and frame is not last_sent:
                try:
                    r=session.post(f"{CLOUD_URL}/push/frame",data=frame,
                                   headers={"Content-Type":"image/jpeg"},timeout=8)
                    if r.status_code==200: last_sent=frame; lf=now
                    elif r.status_code==401: print("[CLOUD] ❌ Wrong secret")
                except Exception as e: print(f"[CLOUD] Frame: {e}")
        if now-lm>=0.5:
            with confirm_lock:
                payload={str(idx):{
                    "confirmed": confirm_state[idx]["confirmed"],
                    "elapsed":   round(confirm_state[idx]["elapsed"],1),
                    "boxes":     confirm_state[idx]["boxes"]
                } for idx in (0,1)}
            try: session.post(f"{CLOUD_URL}/push/ml",json=payload,timeout=5); lm=now
            except Exception: pass
        if now-ls>=10.0:
            s={str(idx):cameras[idx]["status"] for idx in (0,1)}
            try: session.post(f"{CLOUD_URL}/push/status",json=s,timeout=5); ls=now
            except Exception: pass
        if now-lg>=10.0:
            with gps_state_lock: gps=gps_state.copy()
            if gps['lat'] and gps['lon']:
                try: session.post(f"{CLOUD_URL}/push/gps",json=gps,timeout=5); lg=now
                except Exception: pass
        time.sleep(0.5)

# ─────────────────── GPS WORKER ─────────────────────
def gps_worker():
    def push(lat,lon,speed):
        with gps_state_lock: gps_state.update({"lat":lat,"lon":lon,"speed":speed})
    try:
        import serial,pynmea2
        while True:
            try:
                with serial.Serial(GPS_PORT,GPS_BAUD,timeout=1) as ser:
                    while True:
                        line=ser.readline().decode('ascii',errors='replace').strip()
                        if line.startswith(('$GPRMC','$GNRMC')):
                            try:
                                msg=pynmea2.parse(line)
                                if msg.status=='A':
                                    push(float(msg.latitude),float(msg.longitude),
                                         float(msg.spd_over_grnd)*1.852)
                            except Exception: pass
            except Exception as e: print(f"[GPS] {e}, retry 10s"); time.sleep(10)
    except ImportError:
        if STATIC_LAT and STATIC_LON:
            while True: push(STATIC_LAT,STATIC_LON,0); time.sleep(30)
        else: print("[GPS] No GPS configured")

# ─────────────────── FRAME GENERATORS ────────────────
def _gen_single(cam_idx):
    cameras[cam_idx]["frame_ready"].wait(timeout=15)
    last=None; stall=0
    while True:
        with cameras[cam_idx]["overlay_lock"]: frame=cameras[cam_idx]["overlay_frame"]
        if frame is None:
            with cameras[cam_idx]["frame_lock"]: frame=cameras[cam_idx]["latest_frame"]
        if frame is None or frame is last:
            stall+=1
            if stall>450: return
            time.sleep(0.033); continue
        stall=0; last=frame
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+frame+b'\r\n'

def generate_combined():
    combined_ready.wait(timeout=20)
    last=None; stall=0
    while True:
        with combined_frame_lock: frame=combined_frame
        if frame is None or frame is last:
            stall+=1
            if stall>450: return
            time.sleep(0.033); continue
        stall=0; last=frame
        yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n'+frame+b'\r\n'

# ─────────────────── ROUTES ─────────────────────────
@app.route('/stream')
def stream_combined():
    return Response(generate_combined(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/front')
def stream_front():
    return Response(_gen_single(0), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/stream/rear')
def stream_rear():
    return Response(_gen_single(1), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/snapshot.jpg')
def snapshot():
    with combined_frame_lock: frame=combined_frame
    if frame is None: return "No frame",503
    return Response(frame,mimetype='image/jpeg')

@app.route('/snapshot/<int:cam_idx>.jpg')
def snapshot_cam(cam_idx):
    if cam_idx not in cameras: return "Invalid camera",404
    with cameras[cam_idx]["overlay_lock"]: frame=cameras[cam_idx]["overlay_frame"]
    if frame is None:
        with cameras[cam_idx]["frame_lock"]: frame=cameras[cam_idx]["latest_frame"]
    if frame is None: return "No frame",503
    return Response(frame,mimetype='image/jpeg')

@app.route('/detection',methods=['GET','POST'])
def detection():
    global color_detection_enabled,detection_mode
    if request.method=='POST':
        d=request.get_json()
        color_detection_enabled=d.get('enabled',False)
        detection_mode=d.get('mode','center')
        return jsonify({'success':True,'enabled':color_detection_enabled,'mode':detection_mode})
    return jsonify({'enabled':color_detection_enabled,'mode':detection_mode})

@app.route('/ml',methods=['GET','POST'])
def ml_toggle():
    global ml_detection_enabled
    if request.method=='POST':
        ml_detection_enabled=request.get_json().get('enabled',False)
        return jsonify({'success':True,'enabled':ml_detection_enabled})
    return jsonify({'enabled':ml_detection_enabled})

@app.route('/colors')
def get_colors():
    with detection_lock:
        return jsonify({'front':detected_colors[0],'rear':detected_colors[1]})

@app.route('/ml_results')
def get_ml_results():
    with confirm_lock:
        cs = {
            idx: {
                "confirmed":    confirm_state[idx]["confirmed"],
                "elapsed":      round(confirm_state[idx]["elapsed"], 2),
                "first_seen":   confirm_state[idx]["first_seen"] is not None,
                "boxes":        confirm_state[idx]["boxes"],
                "confirm_secs": CRASH_CONFIRM_SECONDS,
            }
            for idx in (0, 1)
        }
    return jsonify({"front": cs[0], "rear": cs[1]})

@app.route('/status')
def get_status():
    return jsonify({'front':cameras[0]["status"],'rear':cameras[1]["status"]})

@app.route('/gps')
def get_gps():
    with gps_state_lock: return jsonify(gps_state)

@app.route('/ping')
def ping():
    return 'pong', 200

# ─────────────────── HTML UI ────────────────────────
@app.route('/')
def index():
    html = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pi Dual Cam — 360 View</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@400;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root {
  --cyan:#00e5ff; --orange:#ff6d00; --green:#00e676;
  --red:#ff1744;  --yellow:#ffd600;
  --bg:#050a0e;   --panel:#0a1520; --border:#1a2e40;
  --text:#c8d8e8; --dim:#4a6070;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;min-height:100vh}

.header{display:flex;align-items:center;justify-content:space-between;
  padding:10px 20px;
  background:linear-gradient(135deg,#050a0e 60%,#0a1a28);
  border-bottom:1px solid var(--border)}
.header-title{font-family:'Rajdhani',sans-serif;font-weight:700;font-size:22px;
  letter-spacing:3px;color:var(--cyan);text-shadow:0 0 12px rgba(0,229,255,.5)}
.header-title span{color:var(--orange)}
.header-status{display:flex;gap:16px;font-size:11px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block;
  margin-right:4px;box-shadow:0 0 4px currentColor;transition:background .3s}
.dot.live{background:var(--green)}.dot.warn{background:var(--orange)}

.view-bar{display:flex;justify-content:center;gap:8px;padding:10px 20px;
  background:var(--panel);border-bottom:1px solid var(--border)}
.vbtn{font-family:'Rajdhani',sans-serif;font-weight:600;font-size:13px;letter-spacing:1px;
  padding:6px 20px;border:1px solid var(--border);background:transparent;
  color:var(--dim);cursor:pointer;transition:all .2s}
.vbtn:hover{border-color:var(--cyan);color:var(--cyan)}
.vbtn.active{background:var(--cyan);color:#000;border-color:var(--cyan)}

.stream-wrapper{position:relative;background:#000;display:flex;justify-content:center;
  border-bottom:2px solid var(--border);overflow:hidden}

/* ← FIXED: was 2560px causing stretch blur */
.stream-combined{width:100%;max-width:1280px;display:block;image-rendering:crisp-edges}
.stream-single{width:100%;max-width:640px;display:block;image-rendering:crisp-edges}
.stream-split{display:flex;width:100%;max-width:1280px}
.stream-split img{width:50%;display:block;image-rendering:crisp-edges}

.split-divider{width:3px;background:linear-gradient(to bottom,var(--cyan),var(--orange));flex-shrink:0;z-index:2}
.cam-label{position:absolute;top:8px;font-size:12px;font-family:'Rajdhani',sans-serif;
  font-weight:700;letter-spacing:2px;padding:3px 10px;pointer-events:none}
.cam-label-front{left:10px;background:rgba(0,229,255,.2);color:var(--cyan);border:1px solid var(--cyan)}
.cam-label-rear{right:10px;background:rgba(255,109,0,.2);color:var(--orange);border:1px solid var(--orange)}
#reconnect-msg{text-align:center;color:var(--orange);font-size:12px;min-height:18px;padding:4px}

.bottom-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;padding:14px 20px}
@media(max-width:900px){.bottom-grid{grid-template-columns:1fr 1fr}}
@media(max-width:580px){.bottom-grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);padding:12px 14px}
.panel-title{font-family:'Rajdhani',sans-serif;font-weight:600;font-size:12px;
  letter-spacing:2px;color:var(--dim);margin-bottom:10px;text-transform:uppercase;
  border-bottom:1px solid var(--border);padding-bottom:6px}

.ctrl-row{display:flex;gap:8px;flex-wrap:wrap}
.btn{font-family:'Rajdhani',sans-serif;font-weight:700;font-size:13px;letter-spacing:1px;
  padding:8px 14px;border:1px solid var(--border);background:transparent;
  color:var(--text);cursor:pointer;transition:all .2s;flex:1;text-align:center}
.btn:hover{border-color:var(--cyan);color:var(--cyan)}
.btn.active-cyan{background:rgba(0,229,255,.15);color:var(--cyan);border-color:var(--cyan);
  box-shadow:0 0 8px rgba(0,229,255,.3)}
.btn.active-orange{background:rgba(255,109,0,.15);color:var(--orange);border-color:var(--orange);
  box-shadow:0 0 8px rgba(255,109,0,.3)}

.ml-cam-section{margin-bottom:10px}
.ml-cam-header{font-size:11px;letter-spacing:2px;margin-bottom:6px;padding:3px 8px;display:inline-block}
.ml-cam-front-h{background:rgba(0,229,255,.15);color:var(--cyan);border:1px solid var(--cyan)}
.ml-cam-rear-h{background:rgba(255,109,0,.15);color:var(--orange);border:1px solid var(--orange)}

.confirm-bar-wrap{background:#0d1e2e;border:1px solid var(--border);
  border-radius:2px;height:10px;margin:6px 0;overflow:hidden;position:relative}
.confirm-bar-fill{height:100%;transition:width .4s linear;border-radius:2px}
.confirm-bar-label{font-size:10px;color:var(--dim);margin-top:2px}

.alert-confirmed{border:2px solid var(--red);background:rgba(255,23,68,.1);
  padding:8px 10px;margin-bottom:6px;animation:pulseAlert 1s infinite alternate}
@keyframes pulseAlert{from{box-shadow:0 0 4px var(--red)}to{box-shadow:0 0 16px var(--red)}}
.alert-title{font-family:'Rajdhani',sans-serif;font-weight:700;font-size:15px;
  color:var(--red);letter-spacing:2px}
.alert-detail{font-size:11px;color:#ffb3b3;margin-top:3px}

.ml-none{color:var(--dim);font-size:12px}
.stat-row{display:flex;justify-content:space-between;font-size:11px;padding:3px 0}
.stat-key{color:var(--dim)}
.stat-val{color:var(--text);max-width:180px;text-align:right;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.stat-val.ok{color:var(--green)}.stat-val.err{color:var(--red)}

.gps-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.gps-val{font-size:18px;font-weight:bold;color:var(--green);font-family:'Rajdhani',sans-serif}
.gps-key{font-size:10px;color:var(--dim);letter-spacing:1px}
#map{height:220px;border:1px solid var(--border);margin-top:8px}
#maplink{font-size:11px;color:var(--cyan);margin-top:4px;display:none;text-decoration:none}
#maplink:hover{text-decoration:underline}
#color-panel{font-size:11px}

@keyframes recblink{0%,100%{opacity:1}50%{opacity:0}}
.rec-dot{display:inline-block;width:8px;height:8px;border-radius:50%;
  background:var(--red);animation:recblink 1.2s infinite;margin-right:4px}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:var(--border)}
</style>
</head>
<body>

<div class="header">
  <div class="header-title">PI<span>CAM</span> 360</div>
  <div class="header-status">
    <div><span class="dot" id="dot-f"></span>FRONT</div>
    <div><span class="dot" id="dot-r"></span>REAR</div>
    <div><span class="dot" id="dot-cloud"></span>CLOUD</div>
    <div style="color:var(--cyan)" id="hdr-time"></div>
  </div>
</div>

<div class="view-bar">
  <button class="vbtn active" id="vbtn-360"   onclick="setView('360')">◈ 360 VIEW</button>
  <button class="vbtn"        id="vbtn-front"  onclick="setView('front')">◀ FRONT</button>
  <button class="vbtn"        id="vbtn-rear"   onclick="setView('rear')">REAR ▶</button>
  <button class="vbtn"        id="vbtn-split"  onclick="setView('split')">⊞ SPLIT</button>
</div>

<div class="stream-wrapper" id="stream-wrapper">
  <div id="view-360" style="width:100%;max-width:1280px">
    <img class="stream-combined" id="stream-360" src="/stream">
    <span class="cam-label cam-label-front">◀ FRONT</span>
    <span class="cam-label cam-label-rear">REAR ▶</span>
  </div>
  <img class="stream-single" id="view-front" src="/stream/front" style="display:none">
  <img class="stream-single" id="view-rear"  src="/stream/rear"  style="display:none">
  <div class="stream-split" id="view-split" style="display:none">
    <img id="split-front" src="/stream/front" style="width:50%">
    <div class="split-divider"></div>
    <img id="split-rear"  src="/stream/rear"  style="width:50%">
  </div>
</div>
<div id="reconnect-msg"></div>

<div class="bottom-grid">

  <div class="panel">
    <div class="panel-title"><span class="rec-dot"></span>Controls</div>
    <div class="ctrl-row" style="margin-bottom:8px">
      <button class="btn" id="btn-color" onclick="toggleColor()">Color Detect</button>
      <button class="btn" id="btn-ml"    onclick="toggleML()">ML Detect</button>
    </div>
    <div class="ctrl-row">
      <button class="btn" id="btn-mode-c" onclick="setMode('center')" style="font-size:11px">Center</button>
      <button class="btn" id="btn-mode-g" onclick="setMode('grid')"   style="font-size:11px">Grid</button>
      <button class="btn" onclick="window.open('/snapshot.jpg','_blank')" style="font-size:11px">Snapshot</button>
    </div>
    <div style="margin-top:12px;font-size:10px;color:var(--dim);border-top:1px solid var(--border);padding-top:8px">
      Confirm threshold: <span style="color:var(--yellow)" id="lbl-threshold">3s</span><br>
      Cooldown after alert: <span style="color:var(--yellow)">10s</span><br>
      Resolution: <span style="color:var(--cyan)">640×480 per cam</span><br>
      Quality: <span style="color:var(--cyan)">JPEG 90/92 · Sharp 2.0</span>
    </div>
  </div>

  <div class="panel" style="grid-column:span 2">
    <div class="panel-title">⚠ Accident Detection</div>
    <div id="ml-panel">
      <div class="ml-none">ML Detection: OFF — enable to begin monitoring</div>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">◉ Camera Status</div>
    <div class="stat-row"><span class="stat-key">FRONT</span><span class="stat-val" id="stat-front">—</span></div>
    <div class="stat-row"><span class="stat-key">REAR</span> <span class="stat-val" id="stat-rear">—</span></div>
    <div class="stat-row" style="margin-top:4px">
      <span class="stat-key">CLOUD</span><span class="stat-val" id="stat-cloud">—</span></div>
  </div>

  <div class="panel" style="grid-column:span 2">
    <div class="panel-title">◎ GPS Location</div>
    <div class="gps-grid">
      <div><div class="gps-key">LATITUDE</div><div class="gps-val" id="gps-lat">—</div></div>
      <div><div class="gps-key">LONGITUDE</div><div class="gps-val" id="gps-lon">—</div></div>
      <div><div class="gps-key">SPEED</div><div class="gps-val" id="gps-spd">—</div></div>
      <div><div class="gps-key">FIX</div><div class="gps-val" id="gps-fix" style="color:var(--dim)">NO FIX</div></div>
    </div>
    <div id="map"></div>
    <a id="maplink" href="#" target="_blank">📌 Open in Google Maps</a>
  </div>

  <div class="panel">
    <div class="panel-title">◐ Color Readings</div>
    <div id="color-panel" style="color:var(--dim);font-size:11px">Color detection: OFF</div>
  </div>

</div>

<div id="crash-modal" style="display:none;position:fixed;inset:0;z-index:9999;
  background:rgba(0,0,0,.75);align-items:center;justify-content:center">
  <div style="background:#0d0608;border:2px solid var(--red);max-width:460px;width:90%;
              padding:28px 30px;box-shadow:0 0 40px rgba(255,23,68,.5)">
    <div style="font-family:'Rajdhani',sans-serif;font-weight:700;font-size:26px;
                color:var(--red);letter-spacing:4px;margin-bottom:6px">🚨 CRASH CONFIRMED</div>
    <div style="font-size:13px;color:#ffb3b3;margin-bottom:18px" id="modal-detail">—</div>
    <div style="font-size:11px;color:var(--dim);margin-bottom:18px">
      Detection persisted for ≥ <span style="color:var(--yellow)" id="modal-secs">3</span>s
      — classified as a <strong style="color:var(--red)">real event</strong>.
    </div>
    <div style="display:flex;gap:10px">
      <button onclick="dismissModal(false)"
        style="flex:1;padding:10px;font-family:'Rajdhani',sans-serif;font-weight:700;
               font-size:14px;letter-spacing:2px;border:1px solid var(--dim);
               background:transparent;color:var(--dim);cursor:pointer">DISMISS</button>
      <button onclick="dismissModal(true)"
        style="flex:2;padding:10px;font-family:'Rajdhani',sans-serif;font-weight:700;
               font-size:14px;letter-spacing:2px;border:1px solid var(--red);
               background:rgba(255,23,68,.15);color:var(--red);cursor:pointer">
        ACKNOWLEDGE &amp; REPORT</button>
    </div>
  </div>
</div>

<script>
let colorOn=false, mlOn=false, curMode='center';
let mlInterval=null, curView='360', streamRetries={};
let map=null, marker=null;
let modalShownFor={0:false,1:false};
let confirmSecs=3;

function updateTime(){
  document.getElementById('hdr-time').innerText=new Date().toTimeString().slice(0,8);
}
setInterval(updateTime,1000); updateTime();

function setView(v){
  curView=v;
  ['360','front','rear','split'].forEach(x=>{
    const el=document.getElementById('view-'+x);
    if(el) el.style.display=x===v?(x==='split'?'flex':'block'):'none';
    document.getElementById('vbtn-'+x).classList.toggle('active',x===v);
  });
}

function watchStream(el,src){
  el.onerror=function(){
    let r=(streamRetries[src]||0)+1; streamRetries[src]=r;
    const d=Math.min(10000,r*1500);
    document.getElementById('reconnect-msg').innerText=
      'Stream lost. Reconnect in '+(d/1000).toFixed(1)+'s...';
    setTimeout(()=>{el.src=src+'?t='+Date.now();
      document.getElementById('reconnect-msg').innerText='';},d);
  };
  el.onload=()=>{streamRetries[src]=0;document.getElementById('reconnect-msg').innerText='';};
}
['stream-360','view-front','view-rear','split-front','split-rear'].forEach(id=>{
  const el=document.getElementById(id);
  if(el) watchStream(el, el.getAttribute('src').split('?')[0]);
});

function toggleColor(){
  colorOn=!colorOn;
  fetch('/detection',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:colorOn,mode:curMode})});
  document.getElementById('btn-color').className='btn'+(colorOn?' active-cyan':'');
  if(!colorOn) document.getElementById('color-panel').innerHTML=
    '<span style="color:var(--dim)">Color detection: OFF</span>';
}
function toggleML(){
  mlOn=!mlOn;
  fetch('/ml',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:mlOn})});
  document.getElementById('btn-ml').className='btn'+(mlOn?' active-orange':'');
  if(!mlOn){
    clearInterval(mlInterval);
    document.getElementById('ml-panel').innerHTML=
      '<div class="ml-none">ML Detection: OFF — enable to begin monitoring</div>';
    modalShownFor={0:false,1:false};
  } else { mlInterval=setInterval(updateML,300); }
}
function setMode(m){
  curMode=m;
  if(colorOn) fetch('/detection',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({enabled:true,mode:m})});
  document.getElementById('btn-mode-c').className='btn'+(m==='center'?' active-cyan':'');
  document.getElementById('btn-mode-g').className='btn'+(m==='grid'?' active-cyan':'');
}

function showModal(camLabel, boxes, secs){
  const best=boxes.reduce((a,b)=>a.conf>b.conf?a:b, boxes[0]);
  document.getElementById('modal-detail').innerText=
    'Camera: '+camLabel+' | Side: '+best.side+' | Confidence: '+(best.conf*100).toFixed(1)+'%';
  document.getElementById('modal-secs').innerText=secs.toFixed(1);
  document.getElementById('crash-modal').style.display='flex';
  try{ const a=new AudioContext(); const o=a.createOscillator();
    o.connect(a.destination); o.frequency.value=880;
    o.start(); setTimeout(()=>o.stop(),300); }catch(e){}
}
function dismissModal(report){
  document.getElementById('crash-modal').style.display='none';
  if(report) console.log('Reported crash to operator');
}

function updateML(){
  fetch('/ml_results').then(r=>r.json()).then(data=>{
    confirmSecs = (data.front?.confirm_secs || data.rear?.confirm_secs || 3);
    document.getElementById('lbl-threshold').innerText = confirmSecs+'s';
    let html='';
    ['front','rear'].forEach(key=>{
      const d=data[key], idx=key==='front'?0:1;
      const label=key==='front'?'FRONT':'REAR';
      const hdrCls=key==='front'?'ml-cam-front-h':'ml-cam-rear-h';
      html+=`<div class="ml-cam-section"><span class="ml-cam-header ${hdrCls}">${label} CAM</span>`;
      if(!d||(!d.first_seen&&!d.confirmed)){
        html+='<div class="ml-none" style="margin:4px 0 8px 0">No detections</div>';
      } else if(d.confirmed){
        html+=`<div class="alert-confirmed"><div class="alert-title">🚨 CRASH CONFIRMED</div>
          <div class="alert-detail">`;
        if(d.boxes&&d.boxes.length){
          const best=d.boxes.reduce((a,b)=>a.conf>b.conf?a:b,d.boxes[0]);
          html+=`Side: ${best.side} &nbsp;|&nbsp; Conf: ${(best.conf*100).toFixed(1)}%`;
        }
        html+=`</div></div>`;
        if(!modalShownFor[idx]&&d.boxes&&d.boxes.length){
          modalShownFor[idx]=true;
          showModal(label,d.boxes,d.elapsed);
        }
      } else if(d.first_seen){
        const pct=Math.min(1,d.elapsed/confirmSecs);
        const pctPx=(pct*100).toFixed(1);
        const r=Math.round(255*pct), g=Math.round(255*(1-pct));
        html+=`<div style="font-size:11px;color:var(--yellow);margin:4px 0">
          ⏱ VERIFYING — ${d.elapsed.toFixed(1)}s / ${confirmSecs}s</div>
          <div class="confirm-bar-wrap">
            <div class="confirm-bar-fill" style="width:${pctPx}%;background:rgb(${r},${g},0)"></div>
          </div>
          <div class="confirm-bar-label">Holding for ${confirmSecs}s to confirm real crash…</div>`;
        if(d.boxes&&d.boxes.length){
          const best=d.boxes.reduce((a,b)=>a.conf>b.conf?a:b,d.boxes[0]);
          html+=`<div style="font-size:11px;color:var(--dim);margin-top:4px">
            ${best.label} · ${best.side} · ${(best.conf*100).toFixed(1)}%</div>`;
        }
        modalShownFor[idx]=false;
      } else { modalShownFor[idx]=false; }
      html+='</div>';
    });
    document.getElementById('ml-panel').innerHTML=html;
  }).catch(()=>{});
}

function updateStatus(){
  fetch('/status').then(r=>r.json()).then(d=>{
    function el(id,txt){
      const e=document.getElementById(id); e.innerText=txt;
      e.className='stat-val'+(
        txt.includes('Streaming')||txt.includes('Running')?' ok':
        txt.includes('ERROR')||txt.includes('error')?' err':'');
    }
    el('stat-front',d.front||'—'); el('stat-rear',d.rear||'—');
    document.getElementById('dot-f').className='dot'+
      (d.front&&d.front.includes('Streaming')?' live':' warn');
    document.getElementById('dot-r').className='dot'+
      (d.rear&&d.rear.includes('Streaming')?' live':' warn');
    document.getElementById('stat-cloud').innerText='ENABLED → Render';
    document.getElementById('dot-cloud').className='dot live';
  }).catch(()=>{});
}
setInterval(updateStatus,2000); updateStatus();

function updateGPS(){
  fetch('/gps').then(r=>r.json()).then(d=>{
    if(d.lat&&d.lon){
      document.getElementById('gps-lat').innerText=d.lat.toFixed(6)+'°';
      document.getElementById('gps-lon').innerText=d.lon.toFixed(6)+'°';
      document.getElementById('gps-spd').innerText=d.speed!=null?d.speed.toFixed(1)+' km/h':'—';
      document.getElementById('gps-fix').innerText='ACTIVE';
      document.getElementById('gps-fix').style.color='var(--green)';
      const ml=document.getElementById('maplink');
      ml.style.display='block'; ml.href='https://maps.google.com/?q='+d.lat+','+d.lon;
      if(!map){
        map=L.map('map').setView([d.lat,d.lon],16);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
          {attribution:'© OSM'}).addTo(map);
        marker=L.marker([d.lat,d.lon]).addTo(map)
          .bindPopup('<b>Pi Camera</b><br>'+d.lat.toFixed(5)+', '+d.lon.toFixed(5))
          .openPopup();
      } else { marker.setLatLng([d.lat,d.lon]); map.setView([d.lat,d.lon]); }
    }
  }).catch(()=>{});
}
setInterval(updateGPS,3000); updateGPS();

function updateColors(){
  fetch('/colors').then(r=>r.json()).then(d=>{
    const all=[...(d.front||[]).map(x=>({...x,cam:'FRONT'})),
               ...(d.rear||[]).map(x=>({...x,cam:'REAR'}))];
    if(!all.length){
      document.getElementById('color-panel').innerHTML='<span style="color:var(--dim)">No readings</span>';
      return;
    }
    document.getElementById('color-panel').innerHTML=all.map(c=>`
      <div style="display:flex;align-items:center;gap:8px;margin:3px 0">
        <div style="width:16px;height:16px;background:${c.hex};border:1px solid #333;flex-shrink:0"></div>
        <span style="color:var(--dim);font-size:10px">${c.cam}</span>
        <span>${c.name}</span>
        <span style="color:var(--dim);font-size:10px">${c.hex}</span>
      </div>`).join('');
  }).catch(()=>{});
}

document.getElementById('btn-mode-c').className='btn active-cyan';
</script>
</body>
</html>'''
    return render_template_string(html)

# ─────────────────── MAIN ───────────────────────────
if __name__ == "__main__":
    print("Loading ML model...")
    model = YOLO("accident_model_latest.pt")
    print("Model loaded!")

    ports = [5000, 5001, 8000, 8080]
    selected_port = next((p for p in ports if check_port(p)), None)
    if not selected_port:
        print("No port available!"); exit(1)

    local_ip = get_local_ip()
    kill_existing_cameras()

    for idx in (0, 1):
        threading.Thread(target=camera_thread, args=(idx,), daemon=True).start()

    threading.Thread(target=overlay_worker, daemon=True).start()
    threading.Thread(target=ml_worker,      daemon=True).start()
    threading.Thread(target=cloud_sender,   daemon=True).start()
    threading.Thread(target=gps_worker,     daemon=True).start()

    cameras[0]["frame_ready"].wait(timeout=15)
    cameras[1]["frame_ready"].wait(timeout=5)

    import logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    print(f"\n{'='*55}")
    print(f"  Pi Dual Camera — 360 Style + Crash Confirmation")
    print(f"  UI:         http://{local_ip}:{selected_port}/")
    print(f"  Combined:   http://{local_ip}:{selected_port}/stream")
    print(f"  Front:      http://{local_ip}:{selected_port}/stream/front")
    print(f"  Rear:       http://{local_ip}:{selected_port}/stream/rear")
    print(f"  Resolution: {CAM_WIDTH}x{CAM_HEIGHT} per camera")
    print(f"  Quality:    rpicam=90, PIL=92, Sharpness=2.0")
    print(f"  Confirm:    {CRASH_CONFIRM_SECONDS}s hold required")
    print(f"  Cooldown:   {CRASH_COOLDOWN_SECONDS}s after confirmed alert")
    print(f"{'='*55}\n")

    app.run(host='0.0.0.0', port=selected_port, threaded=True)

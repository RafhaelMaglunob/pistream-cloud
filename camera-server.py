#!/usr/bin/env python3
"""
Pi Dual CSI Camera — HD 360° + Crash Recording + Pi-side Email + Firebase Upload
- 10s pre/post ring buffer recording
- ffmpeg MP4 compilation
- 10s cancel window (phone polls /crash_alert, calls /crash_cancel)
- Pi sends email via Gmail SMTP using smtplib (no cloud, no phone)
- Queues email if offline, retries every 15s until internet returns
- Emergency contact fetched from Firebase Firestore per rider
- Uploads crash video + snapshot to Firebase Storage after cancel window
- Writes CrashEvents Firestore document (triggers serverless email tomorrow)
- OWNER_EMAIL hardcoded — change with: nano app.py then restart
"""

from flask import Flask, Response, request, jsonify, render_template_string
import threading, time, socket, io, subprocess, hashlib
import collections, os, glob, shutil
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import colorsys
import torch
from ultralytics import YOLO
import requests as req_lib
from datetime import datetime
from functools import wraps
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# ═════════════════════════════════════════════════════
# OWNER CONFIG
# ═════════════════════════════════════════════════════
OWNER_EMAIL = "rafhaelmaglunob02@gmail.com"

# ─────────────────── FIREBASE CONFIG ────────────────
FIREBASE_PROJECT_ID  = "motospherebsit3b"
FIREBASE_API_KEY     = "AIzaSyDllJ3djkebxHZxHlcp6w54goiDMsXiaS8"
FIREBASE_STORAGE_BUCKET = "motospherebsit3b.firebasestorage.app"

RIDERS_COLLECTION      = "Riders"
CRASH_EVENTS_COLLECTION = "CrashEvents"

# ___________________   RENDER    ____________________
RELAY_URL = "https://pistream-cloud.onrender.com/"

# ─────────────────── SMTP CONFIG ────────────────────
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = "motosphere.smart@gmail.com"
SMTP_PASSWORD = "evidjfvmdlpudgam"

# ─────────────────── GPS CONFIG ─────────────────────
STATIC_LAT = None
STATIC_LON = None
GPS_PORT   = "/dev/ttyAMA0"
GPS_BAUD   = 9600

# ─────────────────── CAMERA CONFIG ──────────────────
CAM_WIDTH  = 600
CAM_HEIGHT = 320
CAM_FPS    = 15
COMBINED_W = 1280
COMBINED_H = 480

# ─────────────────── CRASH CONFIG ───────────────────
CRASH_LABEL            = 'motor_crash'
CRASH_CONF_THRESHOLD   = 0.90
CRASH_CONFIRM_SECONDS  = 3
CRASH_COOLDOWN_SECONDS = 10

# ─────────────────── RECORDING CONFIG ───────────────
RECORD_PRE_SECONDS  = 10
RECORD_POST_SECONDS = 10
RECORD_FPS          = 10
RECORD_DIR          = "/tmp/piCam_crashes"
RING_MAXLEN         = RECORD_PRE_SECONDS * RECORD_FPS

os.makedirs(RECORD_DIR, exist_ok=True)

# ─────────────────── DEVICE ID ──────────────────────
import uuid as _uuid
DEVICE_ID = hashlib.sha256(str(_uuid.getnode()).encode()).hexdigest()[:16]

# ─────────────────── CAMERA GLOBALS ─────────────────
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

model          = None
gps_state      = {"lat": None, "lon": None, "speed": None}
gps_state_lock = threading.Lock()

ring_buffer      = collections.deque(maxlen=RING_MAXLEN)
ring_buffer_lock = threading.Lock()

crash_record_lock  = threading.Lock()
crash_record_state = {
    "active":       False,
    "session_id":   None,
    "cancel_event": None,
    "confirmed_at": None,
    "cam_idx":      None,
    "seconds_left": 0.0,
}

_email_queue      = []
_email_queue_lock = threading.Lock()

# ═════════════════════════════════════════════════════
# UTILS
# ═════════════════════════════════════════════════════

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "0.0.0.0"

def check_port(port):
    sock   = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', port)); sock.close(); return result != 0

def set_cam_status(cam_idx, msg):
    with cameras[cam_idx]["status_lock"]:
        cameras[cam_idx]["status"] = msg
    print(f"[CAM{cam_idx}] {msg}")

def has_internet() -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("8.8.8.8", 53))
        s.close()
        return True
    except Exception:
        return False

def rgb_to_color_name(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
    h=h*360; s=s*100; v=v*100
    if s < 10:
        if v < 20: return "Black"
        elif v > 80: return "White"
        else: return "Gray"
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

# ═════════════════════════════════════════════════════
# AUTH
# ═════════════════════════════════════════════════════

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        return f(*args, **kwargs)
    return decorated

# ═════════════════════════════════════════════════════
# FIREBASE — EMERGENCY CONTACT (TRUSTEDCONTACT)
# ═════════════════════════════════════════════════════

def get_emergency_contact(rider_email: str) -> str | None:
    """
    Fetch emergency contact from TrustedContact collection.
    Query: contactEmail == rider_email AND status == 'accepted'
    Return: email field (the trusted contact's email)
    """
    try:
        url = (
            f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
            f"/databases/(default)/documents:runQuery"
            f"?key={FIREBASE_API_KEY}"
        )
        
        body = {
            "structuredQuery": {
                "from": [{"collectionId": "TrustedContact"}],
                "where": {
                    "compositeFilter": {
                        "op": "AND",
                        "filters": [
                            {
                                "fieldFilter": {
                                    "field": {"fieldPath": "contactEmail"},
                                    "op": "EQUAL",
                                    "value": {"stringValue": rider_email}
                                }
                            },
                            {
                                "fieldFilter": {
                                    "field": {"fieldPath": "status"},
                                    "op": "EQUAL",
                                    "value": {"stringValue": "accepted"}
                                }
                            }
                        ]
                    }
                }
            }
        }
        
        r = req_lib.post(url, json=body, timeout=6)
        
        if r.status_code == 200:
            results = r.json()
            for item in results:
                if "document" in item:
                    fields = item["document"].get("fields", {})
                    contact_email = fields.get("email", {}).get("stringValue")
                    if contact_email:
                        print(f"[FIREBASE] Found TrustedContact for {rider_email}: {contact_email}")
                        return contact_email
            print(f"[FIREBASE] No accepted TrustedContact found for {rider_email}")
        else:
            print(f"[FIREBASE] Query failed ({r.status_code}): {r.text[:200]}")
    except Exception as e:
        print(f"[FIREBASE] get_emergency_contact error: {e}")
    return None

# ═════════════════════════════════════════════════════
# FIREBASE STORAGE — UPLOAD
# ═════════════════════════════════════════════════════

def _firebase_storage_upload(local_path: str, remote_path: str,
                              content_type: str) -> str | None:
    try:
        import urllib.parse
        encoded = urllib.parse.quote(remote_path, safe='')
        url = (
            f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_STORAGE_BUCKET}"
            f"/o?uploadType=media&name={encoded}&key={FIREBASE_API_KEY}"
        )
        with open(local_path, 'rb') as f:
            data = f.read()

        r = req_lib.post(url, data=data,
                         headers={"Content-Type": content_type},
                         timeout=120)

        if r.status_code in (200, 201):
            token = r.json().get("downloadTokens", "")
            download_url = (
                f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_STORAGE_BUCKET}"
                f"/o/{encoded}?alt=media&token={token}"
            )
            print(f"[STORAGE] ✅ Uploaded → {remote_path}")
            return download_url
        else:
            print(f"[STORAGE] ❌ Upload failed ({r.status_code}): {r.text[:200]}")
            return None
    except Exception as e:
        print(f"[STORAGE] ❌ Upload error: {e}")
        return None


def upload_crash_to_firebase(session_id: str,
                              snapshot_path: str | None,
                              video_path: str | None) -> dict:
    result = {"snapshot_url": None, "video_url": None}

    if snapshot_path and os.path.exists(snapshot_path):
        remote = f"crashes/{session_id}/snapshot.jpg"
        result["snapshot_url"] = _firebase_storage_upload(
            snapshot_path, remote, "image/jpeg")

    if video_path and os.path.exists(video_path):
        remote = f"crashes/{session_id}/crash_clip.mp4"
        result["video_url"] = _firebase_storage_upload(
            video_path, remote, "video/mp4")

    return result

# ═════════════════════════════════════════════════════
# FIRESTORE — CRASH EVENT DOCUMENT
# ═════════════════════════════════════════════════════

def write_crash_event(session_id: str,
                      rider_email: str,
                      emergency_email: str | None,
                      cam_label: str,
                      location_str: str,
                      speed_str: str,
                      snapshot_url: str | None,
                      video_url: str | None) -> bool:
    try:
        url = (
            f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
            f"/databases/(default)/documents/{CRASH_EVENTS_COLLECTION}/{session_id}"
            f"?key={FIREBASE_API_KEY}"
        )

        def _str(v):  return {"stringValue": v or ""}
        def _null():  return {"nullValue": None}

        body = {
            "fields": {
                "session_id":      _str(session_id),
                "device_id":       _str(DEVICE_ID),
                "rider_email":     _str(rider_email),
                "emergency_email": _str(emergency_email) if emergency_email else _null(),
                "cam_label":       _str(cam_label),
                "location":        _str(location_str),
                "speed":           _str(speed_str),
                "time":            _str(datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                "snapshot_url":    _str(snapshot_url) if snapshot_url else _null(),
                "video_url":       _str(video_url)    if video_url    else _null(),
                "status":          _str("pending"),
            }
        }

        r = req_lib.patch(url, json=body, timeout=10)

        if r.status_code in (200, 201):
            print(f"[FIRESTORE] ✅ CrashEvent written → {session_id}")
            return True
        else:
            print(f"[FIRESTORE] ❌ Write failed ({r.status_code}): {r.text[:200]}")
            return False

    except Exception as e:
        print(f"[FIRESTORE] ❌ Error: {e}")
        return False

# ═════════════════════════════════════════════════════
# COLOR DETECTION
# ═════════════════════════════════════════════════════

def detect_colors_in_frame(cam_idx, frame_bytes, mode='center'):
    try:
        img = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        arr = np.array(img)
        h, w = arr.shape[:2]
        colors = []
        if mode == 'center':
            cx, cy  = w//2, h//2
            sample  = arr[max(0,cy-40):cy+40, max(0,cx-40):cx+40]
            r, g, b = sample.mean(axis=(0,1)).astype(int)
            colors.append({'position':'center','rgb':f'rgb({r},{g},{b})',
                'rgba':f'rgba({r},{g},{b},1)','hex':f'#{r:02x}{g:02x}{b:02x}',
                'name':rgb_to_color_name(r,g,b),'coords':(cx,cy),
                'r':int(r),'g':int(g),'b':int(b)})
        elif mode == 'grid':
            for i in range(3):
                for j in range(3):
                    x=int(w*(j+0.5)/3); y=int(h*(i+0.5)/3)
                    sample=arr[max(0,y-30):y+30,max(0,x-30):x+30]
                    r,g,b=sample.mean(axis=(0,1)).astype(int)
                    colors.append({'position':f'grid_{i}_{j}','rgb':f'rgb({r},{g},{b})',
                        'rgba':f'rgba({r},{g},{b},1)','hex':f'#{r:02x}{g:02x}{b:02x}',
                        'name':rgb_to_color_name(r,g,b),'coords':(x,y),
                        'r':int(r),'g':int(g),'b':int(b)})
        with detection_lock:
            detected_colors[cam_idx] = colors
    except Exception as e:
        print(f"Color detection error cam{cam_idx}: {e}")

# ═════════════════════════════════════════════════════
# ML DETECTION + CRASH CONFIRMATION
# ═════════════════════════════════════════════════════

def detect_accidents_in_frame(cam_idx, frame_bytes):
    try:
        img     = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        results = model.predict(source=np.array(img), imgsz=640, conf=0.5, verbose=False)
        boxes   = []
        w, h    = img.size

        for r in results:
            if len(r.boxes) == 0: continue
            for box, conf, cls in zip(r.boxes.xyxy, r.boxes.conf, r.boxes.cls):
                label = model.names[int(cls)]
                if label != CRASH_LABEL: continue
                if float(conf) < CRASH_CONF_THRESHOLD: continue
                x1,y1,x2,y2 = map(int, box.tolist())
                cx   = (x1+x2)//2
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
                    cs["first_seen"] = now
                    cs["elapsed"]    = 0.0
                    cs["confirmed"]  = False
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
                    threading.Thread(
                        target=crash_pipeline,
                        args=(cam_idx, now),
                        daemon=True
                    ).start()
            else:
                if cs["first_seen"] is not None and not cs["confirmed"]:
                    print(f"[CAM{cam_idx}] ❌ False alarm cleared after {now - cs['first_seen']:.1f}s")
                cs.update({"first_seen": None, "elapsed": 0.0,
                           "confirmed": False, "confirmed_at": None, "boxes": []})
    except Exception as e:
        print(f"ML detection error cam{cam_idx}: {e}")

# ═════════════════════════════════════════════════════
# CRASH PIPELINE
# ═════════════════════════════════════════════════════

def crash_pipeline(cam_idx: int, confirmed_at: float):
    session_id  = f"crash_{int(confirmed_at)}_cam{cam_idx}"
    session_dir = os.path.join(RECORD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    cancel_event = threading.Event()

    with crash_record_lock:
        crash_record_state.update({
            "active":       True,
            "session_id":   session_id,
            "cancel_event": cancel_event,
            "confirmed_at": confirmed_at,
            "cam_idx":      cam_idx,
            "seconds_left": float(CRASH_COOLDOWN_SECONDS),
        })

    print(f"[CRASH] Pipeline → {session_id}")

    with ring_buffer_lock:
        pre_frames = list(ring_buffer)

    pre_paths = []
    for i, (ts, frame_bytes) in enumerate(pre_frames):
        path = os.path.join(session_dir, f"frame_{i:05d}.jpg")
        with open(path, 'wb') as f:
            f.write(frame_bytes)
        pre_paths.append(path)
    print(f"[CRASH] Saved {len(pre_paths)} pre-crash frames")

    post_paths     = []
    frame_idx      = len(pre_paths)
    deadline       = time.time() + RECORD_POST_SECONDS
    frame_interval = 1.0 / RECORD_FPS

    while time.time() < deadline and not cancel_event.is_set():
        with combined_frame_lock:
            frame = combined_frame
        if frame:
            path = os.path.join(session_dir, f"frame_{frame_idx:05d}.jpg")
            with open(path, 'wb') as f:
                f.write(frame)
            post_paths.append(path)
            frame_idx += 1
        time.sleep(frame_interval)
    print(f"[CRASH] Saved {len(post_paths)} post-crash frames")

    cancel_deadline = confirmed_at + CRASH_COOLDOWN_SECONDS
    while time.time() < cancel_deadline and not cancel_event.is_set():
        with crash_record_lock:
            crash_record_state["seconds_left"] = max(0.0, cancel_deadline - time.time())
        time.sleep(0.25)

    with crash_record_lock:
        crash_record_state["active"]       = False
        crash_record_state["seconds_left"] = 0.0

    if cancel_event.is_set():
        print(f"[CRASH] Cancelled — removing session {session_id}")
        shutil.rmtree(session_dir, ignore_errors=True)
        return

    print(f"[CRASH] Cancel window expired — compiling + uploading")

    snapshot_path = pre_paths[-1] if pre_paths else (post_paths[0] if post_paths else None)
    video_path    = os.path.join(session_dir, "crash_clip.mp4")
    compiled      = _compile_video(session_dir, video_path)

    with gps_state_lock:
        lat   = gps_state.get("lat")
        lon   = gps_state.get("lon")
        speed = gps_state.get("speed")

    location_str = f"{lat:.5f}, {lon:.5f}" if lat and lon else "Unknown"
    speed_str    = f"{speed:.1f} km/h"     if speed      else "Unknown"
    time_str     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cam_label    = "FRONT" if cam_idx == 0 else "REAR"

    firebase_urls = {"snapshot_url": None, "video_url": None}

    if has_internet():
        print(f"[CRASH] Uploading to Firebase Storage…")
        firebase_urls = upload_crash_to_firebase(
            session_id    = session_id,
            snapshot_path = snapshot_path,
            video_path    = video_path if compiled else None,
        )
    else:
        print(f"[CRASH] No internet — skipping Firebase upload, Pi email only")

    emergency_email = get_emergency_contact(OWNER_EMAIL)

    if has_internet():
        write_crash_event(
            session_id      = session_id,
            rider_email     = OWNER_EMAIL,
            emergency_email = emergency_email,
            cam_label       = cam_label,
            location_str    = location_str,
            speed_str       = speed_str,
            snapshot_url    = firebase_urls["snapshot_url"],
            video_url       = firebase_urls["video_url"],
        )

    if emergency_email:
        subject = f"🚨 CRASH ALERT — PiCAM [{time_str}]"
        body    = (
            f"CRASH ALERT — PiCAM 360\n\n"
            f"Time:     {time_str}\n"
            f"Camera:   {cam_label}\n"
            f"Location: {location_str}\n"
            f"Speed:    {speed_str}\n\n"
            f"A crash was detected and was NOT cancelled within "
            f"{CRASH_COOLDOWN_SECONDS} seconds.\n\n"
        )
        if firebase_urls["video_url"]:
            body += f"Video: {firebase_urls['video_url']}\n"
        if firebase_urls["snapshot_url"]:
            body += f"Snapshot: {firebase_urls['snapshot_url']}\n"
        body += (
            f"\n{'A local 20s video clip and snapshot are also attached.' if compiled else 'A snapshot is attached (video compilation failed).'}\n\n"
            f"— PiCAM automatic alert system"
        )
        _enqueue_email(
            to          = emergency_email,
            subject     = subject,
            body        = body,
            image_path  = snapshot_path,
            video_path  = video_path if compiled else None,
            session_dir = session_dir,
        )
    else:
        print("[EMAIL] No emergency contact found — no fallback email queued")
        if firebase_urls["video_url"] or firebase_urls["snapshot_url"]:
            shutil.rmtree(session_dir, ignore_errors=True)


def _compile_video(session_dir: str, output_path: str) -> bool:
    try:
        all_jpgs  = sorted(glob.glob(os.path.join(session_dir, "frame_*.jpg")))
        list_path = os.path.join(session_dir, "frames.txt")
        with open(list_path, 'w') as f:
            for p in all_jpgs:
                f.write(f"file '{p}'\n")
                f.write(f"duration {1.0 / RECORD_FPS}\n")

        result = subprocess.run([
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
            '-i',       list_path,
            '-vf',      f'scale={COMBINED_W}:{COMBINED_H}',
            '-c:v',     'libx264',
            '-preset',  'fast',
            '-crf',     '28',
            '-pix_fmt', 'yuv420p',
            output_path
        ], capture_output=True, timeout=90)

        if result.returncode == 0:
            print(f"[VIDEO] Compiled → {output_path}")
            return True
        print(f"[VIDEO] ffmpeg error: {result.stderr.decode()}")
        return False
    except Exception as e:
        print(f"[VIDEO] Compile error: {e}")
        return False

# ═════════════════════════════════════════════════════
# EMAIL QUEUE + SENDER
# ═════════════════════════════════════════════════════

def _enqueue_email(to, subject, body, image_path, video_path, session_dir):
    with _email_queue_lock:
        _email_queue.append({
            "to":          to,
            "subject":     subject,
            "body":        body,
            "image_path":  image_path,
            "video_path":  video_path,
            "session_dir": session_dir,
            "attempts":    0,
        })
    print(f"[EMAIL] Queued → {to}  (total queued: {len(_email_queue)})")


def email_sender_worker():
    print("[EMAIL] Sender worker started")
    while True:
        with _email_queue_lock:
            pending = list(_email_queue)

        if not pending:
            time.sleep(5); continue

        if not has_internet():
            print(f"[EMAIL] No internet — {len(pending)} email(s) queued, waiting…")
            time.sleep(15); continue

        sent_indices = []
        for i, item in enumerate(pending):
            ok = _send_email(
                to         = item["to"],
                subject    = item["subject"],
                body       = item["body"],
                image_path = item.get("image_path"),
                video_path = item.get("video_path"),
            )
            if ok:
                sent_indices.append(i)
                session_dir = item.get("session_dir")
                if session_dir and os.path.isdir(session_dir):
                    shutil.rmtree(session_dir, ignore_errors=True)
                    print(f"[EMAIL] Cleaned up session: {session_dir}")
            else:
                item["attempts"] += 1
                print(f"[EMAIL] Retry #{item['attempts']} for {item['to']}")

        if sent_indices:
            with _email_queue_lock:
                for i in sorted(sent_indices, reverse=True):
                    if i < len(_email_queue):
                        _email_queue.pop(i)

        time.sleep(10)


def _send_email(to: str, subject: str, body: str,
                image_path: str | None = None,
                video_path: str | None = None) -> bool:
    try:
        msg            = MIMEMultipart()
        msg['From']    = SMTP_USER
        msg['To']      = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        if image_path and os.path.exists(image_path):
            with open(image_path, 'rb') as f:
                part = MIMEBase('image', 'jpeg')
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment; filename="crash_snapshot.jpg"')
            msg.attach(part)

        if video_path and os.path.exists(video_path):
            with open(video_path, 'rb') as f:
                part = MIMEBase('video', 'mp4')
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', 'attachment; filename="crash_clip.mp4"')
            msg.attach(part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo(); server.starttls(); server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to, msg.as_string())

        print(f"[EMAIL] ✅ Sent to {to}")
        return True
    except Exception as e:
        print(f"[EMAIL] ❌ Failed ({to}): {e}")
        return False

# ═════════════════════════════════════════════════════
# RING BUFFER WORKER
# ═════════════════════════════════════════════════════

def ring_buffer_worker():
    interval   = 1.0 / RECORD_FPS
    last_frame = None
    print("[RING] Buffer worker started")
    while True:
        time.sleep(interval)
        with combined_frame_lock:
            frame = combined_frame
        if frame is None or frame is last_frame:
            continue
        last_frame = frame
        with ring_buffer_lock:
            ring_buffer.append((time.time(), frame))

# ═════════════════════════════════════════════════════
# OVERLAY
# ═════════════════════════════════════════════════════

def add_overlay(cam_idx, frame_bytes, mode='center'):
    try:
        img  = Image.open(io.BytesIO(frame_bytes)).convert('RGB')
        draw = ImageDraw.Draw(img, 'RGBA')
        try:
            font  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 16)
            sfont = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 13)
        except Exception:
            font = sfont = ImageFont.load_default()

        badge_col = (0, 200, 255, 220) if cam_idx == 0 else (255, 100, 0, 220)
        draw.rectangle([4, 4, 110, 26], fill=badge_col)
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
                pct        = min(1.0, cs["elapsed"] / CRASH_CONFIRM_SECONDS)
                fill       = int(pct * 255)
                box_color  = (255, fill, 0, 255)
                text_bg    = (160, 100, 0, 200)
                status_tag = f"VERIFYING {cs['elapsed']:.1f}/{CRASH_CONFIRM_SECONDS}s"
            else:
                continue
            draw.rectangle([x1,y1,x2,y2], outline=box_color, width=3)
            text = f"{b['label']} {b['conf']*100:.0f}% {b['side']} | {status_tag}"
            tw   = len(text) * 7
            draw.rectangle([x1, max(0,y1-20), x1+tw, y1], fill=text_bg)
            draw.text((x1+2, max(0,y1-18)), text, fill=(255,255,255,255), font=sfont)

        if cs["first_seen"] is not None and not cs["confirmed"]:
            pct    = min(1.0, cs["elapsed"] / CRASH_CONFIRM_SECONDS)
            iw, ih = img.size
            bar_w  = int(iw * pct)
            draw.rectangle([0, ih-8, iw, ih], fill=(40,40,40,200))
            draw.rectangle([0, ih-8, bar_w, ih], fill=(255, int(255*(1-pct)), 0, 220))

        out = io.BytesIO()
        img.save(out, format='JPEG', quality=70)
        return out.getvalue()
    except Exception as e:
        print(f"Overlay error cam{cam_idx}: {e}")
        return frame_bytes

# ═════════════════════════════════════════════════════
# COMBINE FRAMES (360°)
# ═════════════════════════════════════════════════════

def combine_frames(front_bytes, rear_bytes):
    try:
        front  = Image.open(io.BytesIO(front_bytes)).convert('RGB').resize((CAM_WIDTH, CAM_HEIGHT), Image.LANCZOS)
        rear   = Image.open(io.BytesIO(rear_bytes)).convert('RGB').resize((CAM_WIDTH, CAM_HEIGHT), Image.LANCZOS)
        canvas = Image.new('RGB', (COMBINED_W, COMBINED_H + 30), (10, 10, 10))
        canvas.paste(front, (0, 30))
        canvas.paste(rear,  (CAM_WIDTH, 30))
        draw  = ImageDraw.Draw(canvas)
        try:
            hfont = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", 16)
        except Exception:
            hfont = ImageFont.load_default()
        draw.rectangle([0, 0, COMBINED_W, 30], fill=(10, 10, 10))
        draw.text((8, 7),             "◀ FRONT", fill=(0, 200, 255), font=hfont)
        draw.text((CAM_WIDTH + 8, 7), "REAR ▶",  fill=(255, 120, 0),  font=hfont)
        draw.line([(CAM_WIDTH,0),(CAM_WIDTH,COMBINED_H+30)], fill=(40,40,40), width=2)
        ts = time.strftime("%Y-%m-%d  %H:%M:%S")
        draw.text((COMBINED_W//2 - 90, 7), ts, fill=(180,180,180), font=hfont)
        out = io.BytesIO()
        canvas.save(out, format='JPEG', quality=85)
        return out.getvalue()
    except Exception as e:
        print(f"Combine error: {e}"); return front_bytes

# ═════════════════════════════════════════════════════
# CAMERA THREAD
# ═════════════════════════════════════════════════════

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
                 '--quality',   '85',
                 '--sharpness', '1.0',
                 '--contrast',  '1.0',
                 '--brightness','0.1',
                 '--awb',       'auto',
                 '--metering',  'average',
                 '--ev',        '1.5',
                 '--gain',      '4.0',
                 '--denoise',   'off',
                 '--inline', '--nopreview',
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
                    if len(frame)<2048: continue
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

# ═════════════════════════════════════════════════════
# WORKERS
# ═════════════════════════════════════════════════════

def overlay_worker():
    last={0:None,1:None}; skip={0:0,1:0}
    while True:
        time.sleep(0.033)
        for idx in (0,1):
            cam=cameras[idx]
            with cam["frame_lock"]: frame=cam["latest_frame"]
            if frame is None or frame is last[idx]: continue
            last[idx]=frame
            if color_detection_enabled and skip[idx]%3==0:
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
        else: print("[GPS] No GPS module configured")

# ═════════════════════════════════════════════════════
# FRAME GENERATORS
# ═════════════════════════════════════════════════════

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

# ═════════════════════════════════════════════════════
# FLASK ROUTES
# ═════════════════════════════════════════════════════

@app.before_request
def handle_cors():
    if request.method == 'OPTIONS':
        r = app.make_default_options_response()
        r.headers['Access-Control-Allow-Origin']  = '*'
        r.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        r.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return r

@app.after_request
def after_request(response):
    response.headers['Access-Control-Allow-Origin']  = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.route('/crash_alert')
def crash_alert():
    with crash_record_lock:
        state = dict(crash_record_state)
    if not state["active"]:
        return jsonify({"active": False})
    return jsonify({
        "active":       True,
        "session_id":   state["session_id"],
        "seconds_left": round(state["seconds_left"], 1),
        "confirmed_at": state["confirmed_at"],
        "cam_idx":      state["cam_idx"],
    })

@app.route('/crash_cancel', methods=['POST'])
def crash_cancel():
    with crash_record_lock:
        event  = crash_record_state.get("cancel_event")
        active = crash_record_state.get("active", False)
        session_id = crash_record_state.get("session_id")
    if not active or event is None:
        return jsonify({"error": "No active crash alert"}), 400
    event.set()
    print("[CRASH] Cancel received")
    
    # NEW: Notify Relay to cancel the alert
    if session_id:
        try:
            resp = req_lib.post(
                f"{RELAY_URL}/api/crash/cancel",
                json={"session_id": session_id},
                timeout=5
            )
            print(f"[RELAY] Cancel notification sent")
        except Exception as e:
            print(f"[RELAY] Cancel notification failed: {e}")
    
    return jsonify({"success": True, "message": "Cancelled. Recording deleted."})
    
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
    if frame is None: return "No frame", 503
    return Response(frame, mimetype='image/jpeg')

@app.route('/snapshot/<int:cam_idx>.jpg')
def snapshot_cam(cam_idx):
    if cam_idx not in cameras: return "Invalid camera", 404
    with cameras[cam_idx]["overlay_lock"]: frame=cameras[cam_idx]["overlay_frame"]
    if frame is None:
        with cameras[cam_idx]["frame_lock"]: frame=cameras[cam_idx]["latest_frame"]
    if frame is None: return "No frame", 503
    return Response(frame, mimetype='image/jpeg')

@app.route('/detection', methods=['GET','POST'])
def detection():
    global color_detection_enabled, detection_mode
    if request.method == 'POST':
        d = request.get_json()
        color_detection_enabled = d.get('enabled', False)
        detection_mode          = d.get('mode', 'center')
        return jsonify({'success':True,'enabled':color_detection_enabled,'mode':detection_mode})
    return jsonify({'enabled':color_detection_enabled,'mode':detection_mode})

@app.route('/ml', methods=['GET','POST'])
def ml_toggle():
    global ml_detection_enabled
    if request.method == 'POST':
        ml_detection_enabled = request.get_json().get('enabled', False)
        return jsonify({'success':True,'enabled':ml_detection_enabled})
    return jsonify({'enabled':ml_detection_enabled})

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

@app.route('/device/info')
def device_info():
    return jsonify({
        "device_id":           DEVICE_ID,
        "owner_email":         OWNER_EMAIL,
        "resolution":          f"{CAM_WIDTH}x{CAM_HEIGHT}",
        "fps":                 CAM_FPS,
        "combined_resolution": f"{COMBINED_W}x{COMBINED_H}",
        "crash_confirm_secs":  CRASH_CONFIRM_SECONDS,
        "cancel_window_secs":  CRASH_COOLDOWN_SECONDS,
        "record_pre_secs":     RECORD_PRE_SECONDS,
        "record_post_secs":    RECORD_POST_SECONDS,
    })

@app.route('/email_queue_status')
def email_queue_status():
    with _email_queue_lock:
        items = [{"to": i["to"], "attempts": i["attempts"]} for i in _email_queue]
    return jsonify({"queued": len(items), "items": items, "internet": has_internet()})

@app.route('/ping')
def ping():
    return 'pong', 200

@app.route('/')
def index():
    html = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PiCAM 360</title>
<style>
:root{--cyan:#00e5ff;--orange:#ff6d00;--green:#00e676;--red:#ff1744;--yellow:#ffd600;--bg:#050a0e;--panel:#0a1520;--border:#1a2e40;--text:#c8d8e8;--dim:#4a6070}
*{box-sizing:border-box;margin:0;padding:0}body{background:var(--bg);color:var(--text);font-family:monospace;min-height:100vh}
.header{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;background:#050a0e;border-bottom:1px solid var(--border)}
.logo{font-weight:700;font-size:20px;letter-spacing:3px;color:var(--cyan)}.logo span{color:var(--orange)}
.hdr-right{display:flex;gap:14px;font-size:11px;align-items:center}
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim);display:inline-block;margin-right:4px;transition:background .3s}
.dot.live{background:var(--green)}.dot.warn{background:var(--orange)}
.view-bar{display:flex;justify-content:center;gap:6px;padding:8px 16px;background:var(--panel);border-bottom:1px solid var(--border);flex-wrap:wrap}
.vbtn{font-size:11px;letter-spacing:1px;padding:6px 14px;border:1px solid var(--border);background:transparent;color:var(--dim);cursor:pointer;transition:all .2s}
.vbtn:hover{border-color:var(--cyan);color:var(--cyan)}.vbtn.active{background:var(--cyan);color:#000;border-color:var(--cyan)}
.stream-wrapper{background:#000;display:flex;justify-content:center;align-items:center;border-bottom:2px solid var(--border);overflow:hidden;position:relative;max-height:56vh}
.stream-360{max-width:100%;max-height:56vh;width:auto;height:auto;display:block;object-fit:contain}
.stream-single{max-width:100%;max-height:56vh;width:auto;height:auto;display:block}
.stream-split{display:flex;width:100%;max-height:56vh}.stream-split img{width:50%;max-height:56vh;object-fit:contain;display:block}
.split-div{width:2px;background:linear-gradient(to bottom,var(--cyan),var(--orange));flex-shrink:0}
.cam-lbl{position:absolute;top:8px;font-size:10px;font-weight:700;letter-spacing:2px;padding:3px 8px;pointer-events:none}
.cam-lbl-f{left:10px;background:rgba(0,229,255,.2);color:var(--cyan);border:1px solid var(--cyan)}
.cam-lbl-r{right:10px;background:rgba(255,109,0,.2);color:var(--orange);border:1px solid var(--orange)}
#crash-banner{display:none;padding:10px 16px;background:#1a0000;border-bottom:3px solid var(--red);align-items:center;justify-content:space-between}
.crash-info{color:var(--red);font-weight:700;font-size:14px;letter-spacing:1px}.crash-secs{color:var(--orange);font-weight:700}
.cancel-btn{background:#065f46;border:1px solid var(--green);color:var(--green);padding:7px 16px;cursor:pointer;font-weight:700;font-size:11px;letter-spacing:1px}
.cancel-btn:hover{background:#0a7a58}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;padding:12px 16px}
@media(max-width:1200px){.grid{grid-template-columns:1fr 1fr}}@media(max-width:600px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--border);padding:10px 12px}
.ptitle{font-size:10px;letter-spacing:2px;color:var(--dim);margin-bottom:8px;text-transform:uppercase;border-bottom:1px solid var(--border);padding-bottom:5px}
.ctrl-row{display:flex;gap:6px;flex-wrap:wrap}
.btn{font-size:11px;letter-spacing:1px;padding:7px 10px;border:1px solid var(--border);background:transparent;color:var(--text);cursor:pointer;transition:all .2s;flex:1;text-align:center}
.btn:hover{border-color:var(--cyan);color:var(--cyan)}.btn.on-org{background:rgba(255,109,0,.15);color:var(--orange);border-color:var(--orange)}
.ml-sec{margin-bottom:8px}.ml-hdr{font-size:10px;letter-spacing:2px;margin-bottom:5px;padding:3px 7px;display:inline-block}
.ml-hdr-f{background:rgba(0,229,255,.15);color:var(--cyan);border:1px solid var(--cyan)}
.ml-hdr-r{background:rgba(255,109,0,.15);color:var(--orange);border:1px solid var(--orange)}
.bar-wrap{background:#0d1e2e;border:1px solid var(--border);height:8px;margin:5px 0;overflow:hidden;border-radius:2px}
.bar-fill{height:100%;transition:width .4s linear;border-radius:2px}
.alert-box{border:2px solid var(--red);background:rgba(255,23,68,.1);padding:6px 8px;margin-bottom:5px;animation:pulse 1s infinite alternate}
@keyframes pulse{from{box-shadow:0 0 4px var(--red)}to{box-shadow:0 0 14px var(--red)}}
.alert-lbl{font-weight:700;font-size:13px;color:var(--red);letter-spacing:2px}
.srow{display:flex;justify-content:space-between;font-size:11px;padding:3px 0}
.sk{color:var(--dim)}.sv{color:var(--text);text-align:right}
.gps-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.gv{font-size:15px;font-weight:bold;color:var(--green)}.gk{font-size:10px;color:var(--dim);letter-spacing:1px}
@keyframes blink{0%,100%{opacity:1}50%{opacity:0}}.rdot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--red);animation:blink 1.2s infinite;margin-right:4px}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-thumb{background:var(--border)}
</style>
</head>
<body>
<div class="header"><div class="logo">PI<span>CAM</span> 360</div><div class="hdr-right"><div><span class="dot" id="df"></span>FRONT</div><div><span class="dot" id="dr"></span>REAR</div><div style="color:var(--cyan)" id="clk"></div></div></div>
<div class="view-bar"><button class="vbtn active" id="vb360" onclick="sv('360')">◈ 360°</button><button class="vbtn" id="vbfr" onclick="sv('front')">◀ FRONT</button><button class="vbtn" id="vbre" onclick="sv('rear')">REAR ▶</button><button class="vbtn" id="vbsp" onclick="sv('split')">⊞ SPLIT</button></div>
<div id="crash-banner"><div><span class="crash-info">🚨 CRASH DETECTED — </span><span style="color:var(--dim);font-size:11px">Uploading + email sends in <span class="crash-secs" id="csecs">10s</span></span></div><button class="cancel-btn" onclick="doCancel()">✕ FALSE ALARM — CANCEL</button></div>
<div class="stream-wrapper" id="sw"><div id="v360" style="position:relative;display:flex;justify-content:center;width:100%"><img class="stream-360" id="s360" src="/stream"><span class="cam-lbl cam-lbl-f">◀ FRONT</span><span class="cam-lbl cam-lbl-r">REAR ▶</span></div><div id="vfr" style="display:none;width:100%"><img class="stream-single" id="sfr" src="/stream/front"></div><div id="vre" style="display:none"><img class="stream-single" id="sre" src="/stream/rear"></div><div class="stream-split" id="vsp" style="display:none"><img id="spf" src="/stream/front"><div class="split-div"></div><img id="spr" src="/stream/rear"></div></div>
<div class="grid"><div class="panel"><div class="ptitle"><span class="rdot"></span>Controls</div><div class="ctrl-row"><button class="btn" id="bml" onclick="toggleML()">ML Detect</button></div><div style="margin-top:8px;font-size:10px;color:var(--dim);border-top:1px solid var(--border);padding-top:6px">Resolution: <span style="color:var(--cyan)">640×480</span><br>Combined: <span style="color:var(--cyan)">1280×480</span><br>FPS: <span style="color:var(--cyan)">30</span></div></div>
<div class="panel" style="grid-column:span 2"><div class="ptitle">⚠ Accident Detection</div><div id="mlp" style="font-size:11px;color:var(--dim)">ML Detection: OFF</div></div>
<div class="panel"><div class="ptitle">◉ Camera Status</div><div class="srow"><span class="sk">FRONT</span><span class="sv" id="stf">—</span></div><div class="srow"><span class="sk">REAR</span> <span class="sv" id="str">—</span></div></div>
<div class="panel" style="grid-column:span 2"><div class="ptitle">◎ GPS Location</div><div class="gps-grid"><div><div class="gk">LAT</div><div class="gv" id="glat">—</div></div><div><div class="gk">LON</div><div class="gv" id="glon">—</div></div><div><div class="gk">SPEED</div><div class="gv" id="gspd">—</div></div><div><div class="gk">FIX</div><div class="gv" id="gfix" style="color:var(--dim)">NO FIX</div></div></div></div>
<div class="panel"><div class="ptitle">☁ Firebase</div><div class="srow"><span class="sk">DEVICE ID</span><span class="sv" id="did" style="font-size:9px">—</span></div><div class="srow"><span class="sk">OWNER</span><span class="sv" id="dem" style="font-size:9px">—</span></div><div class="srow"><span class="sk">EMAIL QUEUE</span><span class="sv" id="eq" style="font-size:9px">—</span></div></div></div>
<script>let mlOn=false,crashActive=false;async function toggleML(){mlOn=!mlOn;await fetch('/ml',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:mlOn})});document.getElementById('bml').classList.toggle('on-org',mlOn);if(mlOn)setInterval(pollML,500);else document.getElementById('mlp').innerHTML='<span style="color:var(--dim)">ML Detection: OFF</span>';}async function pollML(){if(!mlOn)return;try{const d=await(await fetch('/ml_results')).json();let h='';['front','rear'].forEach(k=>{const v=d[k];const lbl=k==='front'?'FRONT':'REAR';const cls=k==='front'?'ml-hdr-f':'ml-hdr-r';h+=`<div class="ml-sec"><span class="ml-hdr ${cls}">${lbl}</span>`;if(!v||(!v.first_seen&&!v.confirmed))h+='<div style="font-size:10px;color:var(--dim);margin:3px 0">No detections</div>';else if(v.confirmed)h+='<div class="alert-box"><div class="alert-lbl">🚨 CRASH CONFIRMED</div></div>';else if(v.first_seen){const p=Math.min(1,v.elapsed/v.confirm_secs);h+=`<div style="font-size:10px;color:var(--yellow)">⏱ ${v.elapsed.toFixed(1)}/${v.confirm_secs}s</div><div class="bar-wrap"><div class="bar-fill" style="width:${p*100}%;background:rgb(${Math.round(255*p)},${Math.round(255*(1-p))},0)"></div></div>`;}h+='</div>';});document.getElementById('mlp').innerHTML=h;}catch(e){}}async function pollCrash(){try{const d=await(await fetch('/crash_alert')).json();const banner=document.getElementById('crash-banner');if(d.active){banner.style.display='flex';document.getElementById('csecs').innerText=Math.ceil(d.seconds_left)+'s';crashActive=true;}else if(crashActive){banner.style.display='none';crashActive=false;}}catch(e){}}async function doCancel(){try{const res=await fetch('/crash_cancel',{method:'POST'});const d=await res.json();if(d.success){crashActive=false;document.getElementById('crash-banner').style.display='none';}}catch(e){alert('Could not cancel — check Pi connection');}}async function pollStatus(){try{const d=await(await fetch('/status')).json();document.getElementById('stf').innerText=d.front||'—';document.getElementById('str').innerText=d.rear||'—';document.getElementById('df').className='dot'+(d.front&&d.front.includes('Streaming')?'live':' warn');document.getElementById('dr').className='dot'+(d.rear&&d.rear.includes('Streaming')?'live':' warn');}catch(e){}}async function pollGPS(){try{const d=await(await fetch('/gps')).json();if(d.lat&&d.lon){document.getElementById('glat').innerText=d.lat.toFixed(5)+'°';document.getElementById('glon').innerText=d.lon.toFixed(5)+'°';document.getElementById('gspd').innerText=d.speed?d.speed.toFixed(1)+' km/h':'—';document.getElementById('gfix').innerText='ACTIVE';document.getElementById('gfix').style.color='var(--green)';};}catch(e){}}async function pollQueue(){try{const d=await(await fetch('/email_queue_status')).json();const el=document.getElementById('eq');if(d.queued===0)el.innerText='None pending';else el.innerText=d.queued+' pending '+(d.internet?'(sending…)':'(offline, waiting)');}catch(e){}}async function loadInfo(){try{const d=await(await fetch('/device/info')).json();document.getElementById('did').innerText=d.device_id||'—';document.getElementById('dem').innerText=d.owner_email||'—';}catch(e){}}function sv(v){const ids={360:'v360',front:'vfr',rear:'vre',split:'vsp'};const btns={360:'vb360',front:'vbfr',rear:'vbre',split:'vbsp'};Object.keys(ids).forEach(x=>{const el=document.getElementById(ids[x]);el.style.display=x===v?(x==='split'?'flex':'block'):'none';document.getElementById(btns[x]).classList.toggle('active',x===v);});}function watchStreams(){[{id:'s360',src:'/stream'},{id:'sfr',src:'/stream/front'},{id:'sre',src:'/stream/rear'},{id:'spf',src:'/stream/front'},{id:'spr',src:'/stream/rear'}].forEach(s=>{const el=document.getElementById(s.id);if(!el)return;el.onerror=()=>setTimeout(()=>{el.src=s.src+'?t='+Date.now();},3000);});}function tick(){document.getElementById('clk').innerText=new Date().toTimeString().slice(0,8);}loadInfo();watchStreams();setInterval(tick,1000);setInterval(pollStatus,2000);setInterval(pollGPS,3000);setInterval(pollCrash,1000);setInterval(pollQueue,5000);
</script>
</body>
</html>'''
    return render_template_string(html)

if __name__ == "__main__":
    print("="*60)
    print("  Pi Dual Camera — 640x480 + Firebase Storage + Crash Email")
    print("="*60)
    print(f"  Owner: {OWNER_EMAIL}")
    print(f"  Device ID: {DEVICE_ID}")
    print(f"  Firebase project: {FIREBASE_PROJECT_ID}")
    print(f"  Storage bucket:   {FIREBASE_STORAGE_BUCKET}")
    print(f"  CrashEvents collection: {CRASH_EVENTS_COLLECTION}")

    print("\nLoading ML model…")
    model = YOLO("accident_model_latest.pt")
    print("✓ Model loaded!")

    ports         = [5000, 5001, 8000, 8080]
    selected_port = next((p for p in ports if check_port(p)), None)
    if not selected_port:
        print("✗ No port available!"); exit(1)

    local_ip = get_local_ip()
    kill_existing_cameras()

    for idx in (0, 1):
        threading.Thread(target=camera_thread,   args=(idx,), daemon=True).start()

    threading.Thread(target=overlay_worker,      daemon=True).start()
    threading.Thread(target=ml_worker,           daemon=True).start()
    threading.Thread(target=gps_worker,          daemon=True).start()
    threading.Thread(target=ring_buffer_worker,  daemon=True).start()
    threading.Thread(target=email_sender_worker, daemon=True).start()

    cameras[0]["frame_ready"].wait(timeout=15)
    cameras[1]["frame_ready"].wait(timeout=5)

    import logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    print(f"\n{'='*60}")
    print(f"  🎥 UI:            http://{local_ip}:{selected_port}/")
    print(f"  📡 Stream:        http://{local_ip}:{selected_port}/stream")
    print(f"  🚨 Crash alert:   http://{local_ip}:{selected_port}/crash_alert")
    print(f"  ✋  Cancel:        http://{local_ip}:{selected_port}/crash_cancel [POST]")
    print(f"  ☁  Firebase:      crashes/{{session_id}}/  in Storage")
    print(f"  📋 Firestore:     {CRASH_EVENTS_COLLECTION}/{{session_id}}")
    print(f"  📧 SMTP backup:   {SMTP_USER} via {SMTP_HOST}:{SMTP_PORT}")
    print(f"{'='*60}\n")

    app.run(host='0.0.0.0', port=selected_port, threaded=True)

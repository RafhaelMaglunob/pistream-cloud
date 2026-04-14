"""
Render Cloud Backend — Crash Alert Handler with Hardware Failure Detection
- Receives crash alerts from Pi WITH VIDEO/SNAPSHOT DATA
- Stores in Firebase Storage + Firestore
- Manages 10s cancel window
- Auto-sends email if Pi doesn't cancel OR if Pi dies (heartbeat timeout)
- Backup: If Pi stops heartbeat, auto-send alert anyway (hardware failure protection)
"""

from flask import Flask, request, jsonify
import os
import firebase_admin
from firebase_admin import credentials, firestore, storage
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import threading
import time
from datetime import datetime, timedelta
import json
import base64

app = Flask(__name__)

# ═════════════════════════════════════════════════════
# FIREBASE SETUP
# ═════════════════════════════════════════════════════
firebase_creds = os.getenv('FIREBASE_CREDENTIALS_JSON')
if firebase_creds:
    creds_dict = json.loads(firebase_creds)
    cred = credentials.Certificate(creds_dict)
else:
    cred = credentials.Certificate('firebase-key.json')

firebase_admin.initialize_app(cred, {
    'storageBucket': 'motospherebsit3b.appspot.com'
})

db = firestore.client()
bucket = storage.bucket()

# ─────────────────── SMTP CONFIG ────────────────────
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = os.getenv('SMTP_USER', 'motosphere.smart@gmail.com')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', 'evidjfvmdlpudgam')

# ─────────────────── CRASH STATE ────────────────────
# Track active crashes: {session_id: {
#   "confirmed_at": time,
#   "cancelled": bool,
#   "timer": thread,
#   "device_id": str,
#   "rider_email": str,
#   "emergency_email": str,
#   "cam_label": str,
#   "location_str": str,
#   "speed_str": str,
#   "snapshot_url": str,
#   "video_url": str,
#   "last_heartbeat": time,  <-- HARDWARE FAILURE DETECTION
# }}
active_crashes = {}
crash_lock = threading.Lock()

# Device heartbeat tracker: {device_id: {"last_heartbeat": time, "session_id": str}}
device_heartbeats = {}
heartbeat_lock = threading.Lock()

# ─────────────────── CONFIG ─────────────────────────
CRASH_CANCEL_WINDOW = 10  # seconds
HEARTBEAT_TIMEOUT = 15    # seconds — if Pi doesn't heartbeat in 15s, assume dead
HEARTBEAT_CHECK_INTERVAL = 2  # check for dead devices every 2s

# ═════════════════════════════════════════════════════
# UTILS
# ═════════════════════════════════════════════════════

def get_emergency_contact(rider_email: str) -> str | None:
    """Fetch emergency contact from TrustedContact collection"""
    try:
        firebase_api_key = os.getenv('FIREBASE_API_KEY', 'AIzaSyDllJ3djkebxHZxHlcp6w54goiDMsXiaS8')
        firebase_project_id = os.getenv('FIREBASE_PROJECT_ID', 'motospherebsit3b')
        
        url = (
            f"https://firestore.googleapis.com/v1/projects/{firebase_project_id}"
            f"/databases/(default)/documents:runQuery"
            f"?key={firebase_api_key}"
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
        
        r = requests.post(url, json=body, timeout=6)
        
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
    except Exception as e:
        print(f"[FIREBASE] Error fetching contact: {e}")
    
    return None


def send_email(to: str, subject: str, body: str, 
               snapshot_url: str | None = None,
               video_url: str | None = None) -> bool:
    """Send email via Gmail SMTP with links to cloud files"""
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = to
        msg['Subject'] = subject
        
        # Build body with links
        full_body = body
        if video_url:
            full_body += f"\n\n📹 Video: {video_url}"
        if snapshot_url:
            full_body += f"\n📷 Snapshot: {snapshot_url}"
        
        msg.attach(MIMEText(full_body, 'plain'))
        
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to, msg.as_string())
        
        print(f"[EMAIL] ✅ Sent to {to}")
        return True
    except Exception as e:
        print(f"[EMAIL] ❌ Failed: {e}")
        return False


def upload_to_firebase_storage(local_path: str, remote_path: str, 
                               content_type: str) -> str | None:
    """Upload file to Firebase Storage and return download URL"""
    try:
        blob = bucket.blob(remote_path)
        blob.upload_from_filename(local_path, content_type=content_type)
        blob.make_public()
        return blob.public_url
    except Exception as e:
        print(f"[STORAGE] Upload error: {e}")
        return None


def auto_send_alert(session_id: str, device_id: str, rider_email: str, 
                   cam_label: str, location_str: str, speed_str: str,
                   snapshot_url: str | None, video_url: str | None):
    """Auto-send alert if Pi doesn't cancel within window OR if heartbeat dies"""
    
    def _wait_and_send():
        # Wait for cancel window
        start_time = time.time()
        
        while time.time() - start_time < CRASH_CANCEL_WINDOW:
            time.sleep(0.5)
            
            with crash_lock:
                if session_id in active_crashes and active_crashes[session_id]["cancelled"]:
                    # Cancelled by user — exit without sending
                    print(f"[CRASH] Cancel received for {session_id} — no alert sent")
                    return
        
        # Cancel window expired — send alert
        print(f"[CRASH] Cancel window expired for {session_id} — sending alert")
        
        emergency_email = get_emergency_contact(rider_email)
        
        if emergency_email:
            time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            subject = f"🚨 CRASH ALERT — PiCAM [{time_str}]"
            body = (
                f"CRASH ALERT — PiCAM 360\n\n"
                f"Time:     {time_str}\n"
                f"Camera:   {cam_label}\n"
                f"Location: {location_str}\n"
                f"Speed:    {speed_str}\n"
                f"Device:   {device_id}\n\n"
                f"A crash was detected and was NOT cancelled within {CRASH_CANCEL_WINDOW} seconds.\n"
            )
            
            send_email(emergency_email, subject, body, snapshot_url, video_url)
        
        # Update Firestore status
        try:
            db.collection('CrashEvents').document(session_id).update({
                'status': 'sent',
                'sent_at': datetime.now(),
                'reason': 'cancel_window_expired'
            })
        except Exception as e:
            print(f"[FIRESTORE] Error updating status: {e}")
        
        # Clean up
        with crash_lock:
            if session_id in active_crashes:
                del active_crashes[session_id]
    
    # Start timer in background
    timer = threading.Thread(target=_wait_and_send, daemon=True)
    timer.start()
    
    with crash_lock:
        active_crashes[session_id] = {
            "confirmed_at": time.time(),
            "cancelled": False,
            "timer": timer,
            "device_id": device_id,
            "rider_email": rider_email,
            "emergency_email": None,
            "cam_label": cam_label,
            "location_str": location_str,
            "speed_str": speed_str,
            "snapshot_url": snapshot_url,
            "video_url": video_url,
            "last_heartbeat": time.time(),
        }


def check_heartbeats_worker():
    """
    Periodically check if devices are alive.
    If device misses heartbeat for >HEARTBEAT_TIMEOUT, assume hardware failure
    and send alert immediately (don't wait for cancel window).
    """
    print("[HEARTBEAT] Monitor worker started")
    while True:
        time.sleep(HEARTBEAT_CHECK_INTERVAL)
        
        now = time.time()
        dead_sessions = []
        
        with crash_lock:
            for session_id, crash_data in list(active_crashes.items()):
                if crash_data.get("cancelled"):
                    continue
                
                last_hb = crash_data.get("last_heartbeat", now)
                time_since_hb = now - last_hb
                
                # If no heartbeat for HEARTBEAT_TIMEOUT, device is dead
                if time_since_hb > HEARTBEAT_TIMEOUT:
                    print(f"[HEARTBEAT] 💀 Device {crash_data['device_id']} dead for {time_since_hb:.1f}s — sending emergency alert")
                    dead_sessions.append((session_id, crash_data))
        
        # Send alerts for dead devices OUTSIDE the lock
        for session_id, crash_data in dead_sessions:
            emergency_email = get_emergency_contact(crash_data["rider_email"])
            
            if emergency_email:
                time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                subject = f"🚨 HARDWARE FAILURE — CRASH ALERT — PiCAM [{time_str}]"
                body = (
                    f"⚠️ HARDWARE FAILURE DETECTED\n\n"
                    f"Crash was detected but the Pi camera stopped responding.\n"
                    f"The device may have lost power or suffered hardware failure.\n\n"
                    f"Time:     {time_str}\n"
                    f"Camera:   {crash_data['cam_label']}\n"
                    f"Location: {crash_data['location_str']}\n"
                    f"Speed:    {crash_data['speed_str']}\n"
                    f"Device:   {crash_data['device_id']}\n\n"
                    f"⚠️ EMERGENCY: This alert was sent because the Pi lost connection.\n"
                )
                
                send_email(emergency_email, subject, body, 
                          crash_data["snapshot_url"], crash_data["video_url"])
            
            # Update Firestore
            try:
                db.collection('CrashEvents').document(session_id).update({
                    'status': 'sent',
                    'sent_at': datetime.now(),
                    'reason': 'hardware_failure_no_heartbeat'
                })
            except Exception as e:
                print(f"[FIRESTORE] Error updating status: {e}")
            
            # Remove from active crashes
            with crash_lock:
                if session_id in active_crashes:
                    del active_crashes[session_id]


# ═════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════

@app.route('/api/crash/alert', methods=['POST'])
def crash_alert():
    """
    Pi sends crash alert WITH video/snapshot data (base64)
    
    Body: {
        "session_id": "crash_123456_cam0",
        "device_id": "abcd1234",
        "rider_email": "user@example.com",
        "cam_label": "FRONT",
        "location": "14.5995, 120.9842",
        "speed": "45.2",
        "snapshot_base64": "iVBORw0KGgo...",  <-- base64 encoded
        "video_base64": "iVBORw0KGgo...",      <-- base64 encoded
    }
    """
    try:
        data = request.get_json()
        
        session_id = data.get('session_id')
        device_id = data.get('device_id')
        rider_email = data.get('rider_email')
        cam_label = data.get('cam_label')
        location = data.get('location', 'Unknown')
        speed = data.get('speed', 'Unknown')
        snapshot_b64 = data.get('snapshot_base64')
        video_b64 = data.get('video_base64')
        
        print(f"[CRASH] Alert received: {session_id} from device {device_id}")
        
        # Upload to Firebase Storage if provided
        snapshot_url = None
        video_url = None
        
        if snapshot_b64:
            try:
                snapshot_bytes = base64.b64decode(snapshot_b64)
                # Save to temp for upload
                temp_snap = f"/tmp/{session_id}_snapshot.jpg"
                with open(temp_snap, 'wb') as f:
                    f.write(snapshot_bytes)
                remote_snap = f"crashes/{session_id}/snapshot.jpg"
                snapshot_url = upload_to_firebase_storage(temp_snap, remote_snap, "image/jpeg")
                print(f"[STORAGE] Snapshot uploaded: {snapshot_url}")
            except Exception as e:
                print(f"[STORAGE] Snapshot error: {e}")
        
        if video_b64:
            try:
                video_bytes = base64.b64decode(video_b64)
                temp_vid = f"/tmp/{session_id}_crash.mp4"
                with open(temp_vid, 'wb') as f:
                    f.write(video_bytes)
                remote_vid = f"crashes/{session_id}/crash_clip.mp4"
                video_url = upload_to_firebase_storage(temp_vid, remote_vid, "video/mp4")
                print(f"[STORAGE] Video uploaded: {video_url}")
            except Exception as e:
                print(f"[STORAGE] Video error: {e}")
        
        # Store in Firestore
        try:
            db.collection('CrashEvents').document(session_id).set({
                'session_id': session_id,
                'device_id': device_id,
                'rider_email': rider_email,
                'cam_label': cam_label,
                'location': location,
                'speed': speed,
                'snapshot_url': snapshot_url,
                'video_url': video_url,
                'status': 'pending',
                'received_at': datetime.now(),
            })
            print(f"[FIRESTORE] Crash event stored: {session_id}")
        except Exception as e:
            print(f"[FIRESTORE] Error storing crash: {e}")
            return jsonify({"error": "Failed to store crash"}), 500
        
        # Start auto-send timer (with heartbeat monitoring)
        auto_send_alert(
            session_id=session_id,
            device_id=device_id,
            rider_email=rider_email,
            cam_label=cam_label,
            location_str=location,
            speed_str=speed,
            snapshot_url=snapshot_url,
            video_url=video_url
        )
        
        # Register heartbeat for this device
        with heartbeat_lock:
            device_heartbeats[device_id] = {
                "last_heartbeat": time.time(),
                "session_id": session_id
            }
        
        return jsonify({
            "success": True,
            "message": "Crash alert received. You have 10s to cancel.",
            "session_id": session_id
        }), 200
    
    except Exception as e:
        print(f"[API] Error in crash_alert: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/heartbeat', methods=['POST'])
def crash_heartbeat():
    """
    Pi sends heartbeat to keep crash alive (proves hardware is still running)
    
    Body: {"session_id": "crash_123456_cam0", "device_id": "abcd1234"}
    """
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        device_id = data.get('device_id')
        
        with crash_lock:
            if session_id in active_crashes:
                active_crashes[session_id]["last_heartbeat"] = time.time()
                print(f"[HEARTBEAT] ❤️ {device_id} — alive")
        
        with heartbeat_lock:
            if device_id in device_heartbeats:
                device_heartbeats[device_id]["last_heartbeat"] = time.time()
        
        return jsonify({"success": True}), 200
    
    except Exception as e:
        print(f"[API] Error in heartbeat: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/cancel', methods=['POST'])
def crash_cancel():
    """
    Pi cancels crash alert
    Body: {"session_id": "crash_123456_cam0"}
    """
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        
        print(f"[CRASH] Cancel received: {session_id}")
        
        with crash_lock:
            if session_id in active_crashes:
                active_crashes[session_id]["cancelled"] = True
                del active_crashes[session_id]
        
        # Update Firestore
        try:
            db.collection('CrashEvents').document(session_id).update({
                'status': 'cancelled',
                'cancelled_at': datetime.now()
            })
            print(f"[FIRESTORE] Crash cancelled: {session_id}")
        except Exception as e:
            print(f"[FIRESTORE] Error updating cancel: {e}")
        
        return jsonify({
            "success": True,
            "message": "Crash cancelled. Recording deleted."
        }), 200
    
    except Exception as e:
        print(f"[API] Error in crash_cancel: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/status/<session_id>', methods=['GET'])
def crash_status(session_id):
    """Check crash status"""
    try:
        doc = db.collection('CrashEvents').document(session_id).get()
        
        if not doc.exists:
            return jsonify({"error": "Crash not found"}), 404
        
        data = doc.to_dict()
        return jsonify(data), 200
    
    except Exception as e:
        print(f"[API] Error in crash_status: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "PiCAM Cloud Crash Handler v2 (with hardware failure detection)",
        "version": "2.0",
        "endpoints": {
            "POST /api/crash/alert": "Pi sends crash alert with video/snapshot",
            "POST /api/crash/heartbeat": "Pi sends heartbeat (proves device alive)",
            "POST /api/crash/cancel": "Pi cancels crash",
            "GET /api/crash/status/<session_id>": "Check crash status",
            "GET /health": "Health check"
        }
    }), 200


if __name__ == '__main__':
    # Start heartbeat monitor worker
    threading.Thread(target=check_heartbeats_worker, daemon=True).start()
    
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

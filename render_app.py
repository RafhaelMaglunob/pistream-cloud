"""
Render Cloud Backend — Crash Alert Handler
- Receives crash alerts from Pi
- Stores in Firebase Firestore
- Manages cancel window (10s)
- Auto-sends email if Pi dies before cancel
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

app = Flask(__name__)

# ═════════════════════════════════════════════════════
# FIREBASE SETUP
# ═════════════════════════════════════════════════════
# Use environment variable for Firebase credentials JSON
firebase_creds = os.getenv('FIREBASE_CREDENTIALS_JSON')
if firebase_creds:
    import json
    creds_dict = json.loads(firebase_creds)
    cred = credentials.Certificate(creds_dict)
else:
    # Or use file if in development
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
# Track active crashes: {session_id: {"confirmed_at": time, "cancelled": bool, "timer": thread}}
active_crashes = {}
crash_lock = threading.Lock()

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
               snapshot_path: str | None = None,
               video_path: str | None = None) -> bool:
    """Send email via Gmail SMTP"""
    try:
        msg = MIMEMultipart()
        msg['From'] = SMTP_USER
        msg['To'] = to
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        
        # Attach snapshot if provided
        if snapshot_path:
            try:
                with open(snapshot_path, 'rb') as f:
                    part = MIMEBase('image', 'jpeg')
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', 'attachment; filename="crash_snapshot.jpg"')
                msg.attach(part)
            except Exception as e:
                print(f"[EMAIL] Error attaching snapshot: {e}")
        
        # Attach video if provided
        if video_path:
            try:
                with open(video_path, 'rb') as f:
                    part = MIMEBase('video', 'mp4')
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', 'attachment; filename="crash_clip.mp4"')
                msg.attach(part)
            except Exception as e:
                print(f"[EMAIL] Error attaching video: {e}")
        
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


def auto_send_alert(session_id: str, rider_email: str, 
                   cam_label: str, location_str: str, speed_str: str,
                   snapshot_url: str | None, video_url: str | None):
    """Auto-send alert if Pi doesn't cancel in time"""
    def _wait_and_send():
        # Wait 10 seconds for cancellation
        time.sleep(10)
        
        with crash_lock:
            if session_id in active_crashes and not active_crashes[session_id]["cancelled"]:
                # Not cancelled — send alert
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
                        f"Speed:    {speed_str}\n\n"
                        f"A crash was detected and was NOT cancelled within 10 seconds.\n\n"
                    )
                    
                    if video_url:
                        body += f"Video: {video_url}\n"
                    if snapshot_url:
                        body += f"Snapshot: {snapshot_url}\n"
                    
                    body += f"\n— PiCAM automatic alert system (Render)"
                    
                    send_email(emergency_email, subject, body)
                
                # Update Firestore status
                try:
                    db.collection('CrashEvents').document(session_id).update({
                        'status': 'sent',
                        'sent_at': datetime.now()
                    })
                except Exception as e:
                    print(f"[FIRESTORE] Error updating status: {e}")
                
                # Clean up
                if session_id in active_crashes:
                    del active_crashes[session_id]
    
    # Start timer in background
    timer = threading.Thread(target=_wait_and_send, daemon=True)
    timer.start()
    
    with crash_lock:
        active_crashes[session_id] = {
            "confirmed_at": time.time(),
            "cancelled": False,
            "timer": timer
        }

# ═════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════

@app.route('/api/crash/alert', methods=['POST'])
def crash_alert():
    """
    Pi sends crash alert
    Body: {
        "session_id": "crash_123456_cam0",
        "rider_email": "user@example.com",
        "cam_label": "FRONT",
        "location": "14.5995, 120.9842",
        "speed": "45.2",
        "snapshot_url": "https://...",
        "video_url": "https://...",
        "device_id": "abcd1234"
    }
    """
    try:
        data = request.get_json()
        
        session_id = data.get('session_id')
        rider_email = data.get('rider_email')
        cam_label = data.get('cam_label')
        location = data.get('location', 'Unknown')
        speed = data.get('speed', 'Unknown')
        snapshot_url = data.get('snapshot_url')
        video_url = data.get('video_url')
        device_id = data.get('device_id')
        
        print(f"[CRASH] Alert received: {session_id}")
        
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
        
        # Start auto-send timer
        auto_send_alert(
            session_id=session_id,
            rider_email=rider_email,
            cam_label=cam_label,
            location_str=location,
            speed_str=speed,
            snapshot_url=snapshot_url,
            video_url=video_url
        )
        
        return jsonify({
            "success": True,
            "message": "Crash alert received. You have 10s to cancel.",
            "session_id": session_id
        }), 200
    
    except Exception as e:
        print(f"[API] Error in crash_alert: {e}")
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
        "service": "PiCAM Cloud Crash Handler",
        "version": "1.0",
        "endpoints": {
            "POST /api/crash/alert": "Pi sends crash alert",
            "POST /api/crash/cancel": "Pi cancels crash",
            "GET /api/crash/status/<session_id>": "Check crash status",
            "GET /health": "Health check"
        }
    }), 200


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
"""
Render Cloud Backend — Crash Alert Handler with Hardware Failure Detection
- Receives crash alerts from Pi WITH VIDEO/SNAPSHOT DATA
- Stores in Firebase Storage + Firestore
- Manages 10s cancel window
- Auto-sends email if Pi doesn't cancel OR if Pi dies (heartbeat timeout)
- Backup: If Pi stops heartbeat, auto-send alert anyway (hardware failure protection)

FIXES IN THIS VERSION:
  1. Debug logging on every stage (payload, rider_email, Firestore query, SMTP)
  2. Emergency contact lookup is pre-fetched at crash time, not after 10s window
  3. send_email() now logs full SMTP errors
  4. All silent failure paths now print warnings
"""

from flask import Flask, request, jsonify
import os
import firebase_admin
from firebase_admin import credentials, firestore, storage
import requests
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import threading
import time
from datetime import datetime
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
active_crashes = {}
crash_lock = threading.Lock()

device_heartbeats = {}
heartbeat_lock = threading.Lock()

# ─────────────────── CONFIG ─────────────────────────
CRASH_CANCEL_WINDOW    = 10   # seconds
HEARTBEAT_TIMEOUT      = 15   # seconds
HEARTBEAT_CHECK_INTERVAL = 2  # seconds


# ═════════════════════════════════════════════════════
# UTILS
# ═════════════════════════════════════════════════════

def get_emergency_contact(rider_email: str) -> str | None:
    """
    Fetch the emergency contact email from TrustedContact collection.
    Queries where contactEmail == rider_email AND status == 'accepted',
    then returns the 'email' field (the trusted contact's actual email).
    """
    print(f"[CONTACT] Looking up TrustedContact for rider_email='{rider_email}'")

    if not rider_email:
        print("[CONTACT] ⚠️  rider_email is empty — cannot query Firestore")
        return None

    try:
        firebase_api_key    = os.getenv('FIREBASE_API_KEY', 'AIzaSyDllJ3djkebxHZxHlcp6w54goiDMsXiaS8')
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

        print(f"[CONTACT] Sending Firestore query for contactEmail='{rider_email}' AND status='accepted'")
        r = requests.post(url, json=body, timeout=6)
        print(f"[CONTACT] Firestore HTTP status: {r.status_code}")

        if r.status_code != 200:
            print(f"[CONTACT] ❌ Firestore error response: {r.text}")
            return None

        results = r.json()
        print(f"[CONTACT] Firestore returned {len(results)} result(s)")

        for i, item in enumerate(results):
            print(f"[CONTACT] Result[{i}] keys: {list(item.keys())}")

            if "document" in item:
                fields = item["document"].get("fields", {})
                print(f"[CONTACT] Document fields: {list(fields.keys())}")
                print(f"[CONTACT] Full fields dump: {json.dumps(fields, indent=2)}")

                contact_email = fields.get("email", {}).get("stringValue")
                contact_email_field = fields.get("contactEmail", {}).get("stringValue")

                print(f"[CONTACT]   contactEmail field value : '{contact_email_field}'")
                print(f"[CONTACT]   email field value        : '{contact_email}'")
                print(f"[CONTACT]   status field value       : '{fields.get('status', {}).get('stringValue')}'")

                if contact_email:
                    print(f"[CONTACT] ✅ Found emergency contact: {contact_email}")
                    return contact_email
                else:
                    print(f"[CONTACT] ⚠️  'email' field missing or empty in document")
            else:
                print(f"[CONTACT] Result[{i}] has no 'document' key — likely empty result")

        print(f"[CONTACT] ❌ No accepted TrustedContact found for '{rider_email}'")

    except Exception as e:
        print(f"[CONTACT] ❌ Exception: {e}")

    return None


def send_email(to: str, subject: str, body: str,
               snapshot_url: str | None = None,
               video_url: str | None = None) -> bool:
    """Send email via Gmail SMTP with links to cloud files."""
    print(f"[EMAIL] Preparing to send to='{to}' subject='{subject}'")
    print(f"[EMAIL] SMTP_USER='{SMTP_USER}' SMTP_HOST='{SMTP_HOST}:{SMTP_PORT}'")

    if not to:
        print("[EMAIL] ❌ Recipient address is empty — cannot send")
        return False

    try:
        msg = MIMEMultipart()
        msg['From']    = SMTP_USER
        msg['To']      = to
        msg['Subject'] = subject

        full_body = body
        if video_url:
            full_body += f"\n\n📹 Video: {video_url}"
        if snapshot_url:
            full_body += f"\n📷 Snapshot: {snapshot_url}"

        msg.attach(MIMEText(full_body, 'plain'))

        print(f"[EMAIL] Connecting to SMTP server...")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.set_debuglevel(1)   # prints every SMTP command/response
            server.ehlo()
            server.starttls()
            server.ehlo()
            print(f"[EMAIL] Logging in as {SMTP_USER}...")
            server.login(SMTP_USER, SMTP_PASSWORD)
            print(f"[EMAIL] Sending message...")
            server.sendmail(SMTP_USER, to, msg.as_string())

        print(f"[EMAIL] ✅ Successfully sent to {to}")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"[EMAIL] ❌ SMTP Authentication failed — check App Password: {e}")
    except smtplib.SMTPRecipientsRefused as e:
        print(f"[EMAIL] ❌ Recipient refused: {e}")
    except smtplib.SMTPException as e:
        print(f"[EMAIL] ❌ SMTP error: {e}")
    except Exception as e:
        print(f"[EMAIL] ❌ Unexpected error: {e}")

    return False


def upload_to_firebase_storage(local_path: str, remote_path: str,
                               content_type: str) -> str | None:
    """Upload file to Firebase Storage and return public download URL."""
    try:
        blob = bucket.blob(remote_path)
        blob.upload_from_filename(local_path, content_type=content_type)
        blob.make_public()
        print(f"[STORAGE] ✅ Uploaded {local_path} → {blob.public_url}")
        return blob.public_url
    except Exception as e:
        print(f"[STORAGE] ❌ Upload error for {local_path}: {e}")
        return None


def auto_send_alert(session_id: str, device_id: str, rider_email: str,
                    cam_label: str, location_str: str, speed_str: str,
                    snapshot_url: str | None, video_url: str | None,
                    emergency_email: str | None):
    """
    Waits CRASH_CANCEL_WINDOW seconds, then sends alert unless cancelled.
    emergency_email is pre-fetched before this call to avoid delay inside the timer.
    """

    def _wait_and_send():
        print(f"[CRASH] ⏳ Timer started for session={session_id} ({CRASH_CANCEL_WINDOW}s window)")
        start_time = time.time()

        while time.time() - start_time < CRASH_CANCEL_WINDOW:
            time.sleep(0.5)
            with crash_lock:
                if session_id in active_crashes and active_crashes[session_id]["cancelled"]:
                    print(f"[CRASH] ✅ Cancelled before window expired: {session_id}")
                    return

        print(f"[CRASH] ⌛ Cancel window expired for {session_id} — preparing to send alert")

        if not emergency_email:
            print(f"[CRASH] ⚠️  No emergency contact found for rider '{rider_email}' — cannot send alert")
        else:
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
            print(f"[FIRESTORE] Error updating status for {session_id}: {e}")

        with crash_lock:
            if session_id in active_crashes:
                del active_crashes[session_id]

    timer = threading.Thread(target=_wait_and_send, daemon=True)
    timer.start()

    with crash_lock:
        active_crashes[session_id] = {
            "confirmed_at":   time.time(),
            "cancelled":      False,
            "timer":          timer,
            "device_id":      device_id,
            "rider_email":    rider_email,
            "emergency_email": emergency_email,
            "cam_label":      cam_label,
            "location_str":   location_str,
            "speed_str":      speed_str,
            "snapshot_url":   snapshot_url,
            "video_url":      video_url,
            "last_heartbeat": time.time(),
        }


def check_heartbeats_worker():
    """
    Background worker: if a device misses heartbeats for >HEARTBEAT_TIMEOUT,
    assume hardware failure and send an emergency alert immediately.
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
                if time_since_hb > HEARTBEAT_TIMEOUT:
                    print(
                        f"[HEARTBEAT] 💀 Device {crash_data['device_id']} "
                        f"has not heartbeated for {time_since_hb:.1f}s — sending hardware failure alert"
                    )
                    dead_sessions.append((session_id, crash_data))

        for session_id, crash_data in dead_sessions:
            emergency_email = crash_data.get("emergency_email") or get_emergency_contact(crash_data["rider_email"])

            if not emergency_email:
                print(f"[HEARTBEAT] ⚠️  No emergency contact for device {crash_data['device_id']} — skipping alert")
            else:
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
                send_email(
                    emergency_email, subject, body,
                    crash_data["snapshot_url"], crash_data["video_url"]
                )

            try:
                db.collection('CrashEvents').document(session_id).update({
                    'status': 'sent',
                    'sent_at': datetime.now(),
                    'reason': 'hardware_failure_no_heartbeat'
                })
            except Exception as e:
                print(f"[FIRESTORE] Error updating hardware failure status: {e}")

            with crash_lock:
                if session_id in active_crashes:
                    del active_crashes[session_id]


# ═════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════

@app.route('/api/crash/alert', methods=['POST'])
def crash_alert():
    """
    Pi sends crash alert WITH video/snapshot data (base64).

    Body: {
        "session_id":       "crash_123456_cam0",
        "device_id":        "abcd1234",
        "rider_email":      "user@example.com",
        "cam_label":        "FRONT",
        "location":         "14.5995, 120.9842",
        "speed":            "45.2",
        "snapshot_base64":  "iVBORw0KGgo...",
        "video_base64":     "iVBORw0KGgo..."
    }
    """
    try:
        data = request.get_json()

        # ── DEBUG: log entire payload ──────────────────────────────────────
        print(f"[ALERT] ═══ Incoming crash alert ═══")
        safe_log = {k: v for k, v in data.items() if k not in ('snapshot_base64', 'video_base64')}
        print(f"[ALERT] Payload (excluding base64 fields): {json.dumps(safe_log, indent=2)}")

        session_id   = data.get('session_id')
        device_id    = data.get('device_id')
        rider_email  = data.get('rider_email')
        cam_label    = data.get('cam_label')
        location     = data.get('location', 'Unknown')
        speed        = data.get('speed', 'Unknown')
        snapshot_b64 = data.get('snapshot_base64')
        video_b64    = data.get('video_base64')

        print(f"[ALERT] session_id  = '{session_id}'")
        print(f"[ALERT] device_id   = '{device_id}'")
        print(f"[ALERT] rider_email = '{rider_email}'")
        print(f"[ALERT] cam_label   = '{cam_label}'")
        print(f"[ALERT] location    = '{location}'")
        print(f"[ALERT] speed       = '{speed}'")
        print(f"[ALERT] snapshot_b64 present = {bool(snapshot_b64)}")
        print(f"[ALERT] video_b64 present    = {bool(video_b64)}")

        # ── Pre-fetch emergency contact immediately ───────────────────────
        # (done here so we're not waiting inside the 10s timer)
        emergency_email = get_emergency_contact(rider_email)
        print(f"[ALERT] emergency_email resolved to: '{emergency_email}'")

        if not emergency_email:
            print(f"[ALERT] ⚠️  WARNING: No emergency contact found — alert will NOT be emailed if crash is confirmed")

        # ── Upload media to Firebase Storage ──────────────────────────────
        snapshot_url = None
        video_url    = None

        if snapshot_b64:
            try:
                snapshot_bytes = base64.b64decode(snapshot_b64)
                temp_snap = f"/tmp/{session_id}_snapshot.jpg"
                with open(temp_snap, 'wb') as f:
                    f.write(snapshot_bytes)
                snapshot_url = upload_to_firebase_storage(
                    temp_snap, f"crashes/{session_id}/snapshot.jpg", "image/jpeg"
                )
            except Exception as e:
                print(f"[STORAGE] ❌ Snapshot upload failed: {e}")

        if video_b64:
            try:
                video_bytes = base64.b64decode(video_b64)
                temp_vid = f"/tmp/{session_id}_crash.mp4"
                with open(temp_vid, 'wb') as f:
                    f.write(video_bytes)
                video_url = upload_to_firebase_storage(
                    temp_vid, f"crashes/{session_id}/crash_clip.mp4", "video/mp4"
                )
            except Exception as e:
                print(f"[STORAGE] ❌ Video upload failed: {e}")

        # ── Store in Firestore ────────────────────────────────────────────
        try:
            db.collection('CrashEvents').document(session_id).set({
                'session_id':      session_id,
                'device_id':       device_id,
                'rider_email':     rider_email,
                'emergency_email': emergency_email,
                'cam_label':       cam_label,
                'location':        location,
                'speed':           speed,
                'snapshot_url':    snapshot_url,
                'video_url':       video_url,
                'status':          'pending',
                'received_at':     datetime.now(),
            })
            print(f"[FIRESTORE] ✅ CrashEvent stored: {session_id}")
        except Exception as e:
            print(f"[FIRESTORE] ❌ Failed to store crash: {e}")
            return jsonify({"error": "Failed to store crash"}), 500

        # ── Start cancel-window timer ─────────────────────────────────────
        auto_send_alert(
            session_id=session_id,
            device_id=device_id,
            rider_email=rider_email,
            cam_label=cam_label,
            location_str=location,
            speed_str=speed,
            snapshot_url=snapshot_url,
            video_url=video_url,
            emergency_email=emergency_email,
        )

        # ── Register heartbeat ────────────────────────────────────────────
        with heartbeat_lock:
            device_heartbeats[device_id] = {
                "last_heartbeat": time.time(),
                "session_id":     session_id
            }

        return jsonify({
            "success": True,
            "message": f"Crash alert received. You have {CRASH_CANCEL_WINDOW}s to cancel.",
            "session_id": session_id,
            "emergency_email_found": bool(emergency_email),
        }), 200

    except Exception as e:
        print(f"[ALERT] ❌ Unhandled exception: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/heartbeat', methods=['POST'])
def crash_heartbeat():
    """Pi sends heartbeat to prove hardware is still alive."""
    try:
        data       = request.get_json()
        session_id = data.get('session_id')
        device_id  = data.get('device_id')

        with crash_lock:
            if session_id in active_crashes:
                active_crashes[session_id]["last_heartbeat"] = time.time()
                print(f"[HEARTBEAT] ❤️  {device_id} / {session_id} — alive")
            else:
                print(f"[HEARTBEAT] ⚠️  Heartbeat for unknown session '{session_id}' — already resolved?")

        with heartbeat_lock:
            if device_id in device_heartbeats:
                device_heartbeats[device_id]["last_heartbeat"] = time.time()

        return jsonify({"success": True}), 200

    except Exception as e:
        print(f"[HEARTBEAT] ❌ Error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/cancel', methods=['POST'])
def crash_cancel():
    """Pi or app cancels the crash alert within the cancel window."""
    try:
        data       = request.get_json()
        session_id = data.get('session_id')

        print(f"[CANCEL] Cancel request for session_id='{session_id}'")

        with crash_lock:
            if session_id in active_crashes:
                active_crashes[session_id]["cancelled"] = True
                del active_crashes[session_id]
                print(f"[CANCEL] ✅ Cancelled: {session_id}")
            else:
                print(f"[CANCEL] ⚠️  Session '{session_id}' not in active_crashes — may have already expired")

        try:
            db.collection('CrashEvents').document(session_id).update({
                'status':       'cancelled',
                'cancelled_at': datetime.now()
            })
            print(f"[FIRESTORE] ✅ CrashEvent marked cancelled: {session_id}")
        except Exception as e:
            print(f"[FIRESTORE] ❌ Error updating cancel: {e}")

        return jsonify({
            "success": True,
            "message": "Crash cancelled successfully."
        }), 200

    except Exception as e:
        print(f"[CANCEL] ❌ Error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/status/<session_id>', methods=['GET'])
def crash_status(session_id):
    """Check status of a crash event."""
    try:
        doc = db.collection('CrashEvents').document(session_id).get()
        if not doc.exists:
            return jsonify({"error": "Crash not found"}), 404
        return jsonify(doc.to_dict()), 200
    except Exception as e:
        print(f"[STATUS] ❌ Error: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/debug/contact/<rider_email>', methods=['GET'])
def debug_contact(rider_email):
    """
    DEBUG ENDPOINT — test emergency contact lookup directly.
    Call: GET /api/debug/contact/rafhaelmaglunob02@gmail.com
    Remove this route before going to production.
    """
    print(f"[DEBUG ENDPOINT] Testing contact lookup for '{rider_email}'")
    contact = get_emergency_contact(rider_email)
    return jsonify({
        "rider_email":     rider_email,
        "emergency_email": contact,
        "found":           bool(contact)
    }), 200


@app.route('/api/debug/smtp', methods=['GET'])
def debug_smtp():
    """
    DEBUG ENDPOINT — send a test email to the configured SMTP_USER.
    Call: GET /api/debug/smtp
    Remove this route before going to production.
    """
    result = send_email(
        to=SMTP_USER,
        subject="MotoSphere SMTP Test",
        body="If you see this, SMTP is working correctly.",
    )
    return jsonify({"smtp_ok": result, "sent_to": SMTP_USER}), 200


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"}), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "PiCAM Cloud Crash Handler v2 (with hardware failure detection)",
        "version": "2.1",
        "endpoints": {
            "POST /api/crash/alert":              "Pi sends crash alert with video/snapshot",
            "POST /api/crash/heartbeat":          "Pi sends heartbeat (proves device alive)",
            "POST /api/crash/cancel":             "Cancel a crash within the window",
            "GET  /api/crash/status/<session_id>":"Check crash status",
            "GET  /api/debug/contact/<email>":    "DEBUG: test TrustedContact lookup",
            "GET  /api/debug/smtp":               "DEBUG: send test email via SMTP",
            "GET  /health":                       "Health check"
        }
    }), 200


if __name__ == '__main__':
    threading.Thread(target=check_heartbeats_worker, daemon=True).start()
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

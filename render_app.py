"""
Render Cloud Backend — Crash Alert Handler with Hardware Failure Detection
FIXED VERSION:
  - Relay starts cancel window IMMEDIATELY on receiving alert (correct)
  - Heartbeats accepted during the Pi's full cancel window
  - Emergency contact pre-fetched and cached immediately
  - Full debug logging on every path
  - /api/debug/contact and /api/debug/smtp endpoints for testing
  - SMTP errors fully logged
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

db     = firestore.client()
bucket = storage.bucket()

# ─────────────────── SMTP CONFIG ────────────────────
SMTP_HOST     = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USER     = os.getenv('SMTP_USER',     'motosphere.smart@gmail.com')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', 'evidjfvmdlpudgam')

# ─────────────────── CRASH STATE ────────────────────
active_crashes  = {}
crash_lock      = threading.Lock()
device_heartbeats = {}
heartbeat_lock  = threading.Lock()

# ─────────────────── CONFIG ─────────────────────────
CRASH_CANCEL_WINDOW      = 10   # seconds Pi has to cancel
HEARTBEAT_TIMEOUT        = 30   # seconds — generous, Pi sends every 2s
HEARTBEAT_CHECK_INTERVAL = 2    # seconds between dead-device checks


# ═════════════════════════════════════════════════════
# UTILS
# ═════════════════════════════════════════════════════

def get_emergency_contact(rider_email: str) -> str | None:
    """
    Query TrustedContact where contactEmail == rider_email AND status == 'accepted'.
    Returns the 'email' field value (the trusted contact's email address).
    """
    print(f"[CONTACT] Querying TrustedContact for rider_email='{rider_email}'")

    if not rider_email:
        print("[CONTACT] ⚠️  rider_email is empty — aborting lookup")
        return None

    try:
        firebase_api_key    = os.getenv('FIREBASE_API_KEY',    'AIzaSyDllJ3djkebxHZxHlcp6w54goiDMsXiaS8')
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
                                    "field":  {"fieldPath": "contactEmail"},
                                    "op":     "EQUAL",
                                    "value":  {"stringValue": rider_email}
                                }
                            },
                            {
                                "fieldFilter": {
                                    "field": {"fieldPath": "status"},
                                    "op":    "EQUAL",
                                    "value": {"stringValue": "accepted"}
                                }
                            }
                        ]
                    }
                }
            }
        }

        r = requests.post(url, json=body, timeout=8)
        print(f"[CONTACT] Firestore HTTP {r.status_code}")

        if r.status_code != 200:
            print(f"[CONTACT] ❌ Firestore error: {r.text[:300]}")
            return None

        results = r.json()
        print(f"[CONTACT] Got {len(results)} result block(s)")

        for i, item in enumerate(results):
            print(f"[CONTACT] Block[{i}] keys: {list(item.keys())}")
            if "document" not in item:
                print(f"[CONTACT] Block[{i}] — no 'document' key (empty result)")
                continue

            fields = item["document"].get("fields", {})
            print(f"[CONTACT] Document field names: {list(fields.keys())}")
            print(f"[CONTACT] Full fields: {json.dumps(fields, indent=2)}")

            contact_email_raw = fields.get("contactEmail", {}).get("stringValue", "")
            status_raw        = fields.get("status",       {}).get("stringValue", "")
            email_raw         = fields.get("email",        {}).get("stringValue", "")

            print(f"[CONTACT]   contactEmail = '{contact_email_raw}'")
            print(f"[CONTACT]   status       = '{status_raw}'")
            print(f"[CONTACT]   email        = '{email_raw}'")

            if email_raw:
                print(f"[CONTACT] ✅ Emergency contact resolved: '{email_raw}'")
                return email_raw
            else:
                print(f"[CONTACT] ⚠️  'email' field is empty in this document")

        print(f"[CONTACT] ❌ No usable TrustedContact found for '{rider_email}'")

    except Exception as e:
        print(f"[CONTACT] ❌ Exception during lookup: {e}")

    return None


def send_email(to: str, subject: str, body: str,
               snapshot_url: str | None = None,
               video_url:    str | None = None) -> bool:
    """Send crash alert email via Gmail SMTP."""
    print(f"[EMAIL] ─── Attempting send ───")
    print(f"[EMAIL]   To      : '{to}'")
    print(f"[EMAIL]   Subject : '{subject}'")
    print(f"[EMAIL]   SMTP    : {SMTP_USER} → {SMTP_HOST}:{SMTP_PORT}")

    if not to:
        print("[EMAIL] ❌ Empty recipient — aborting")
        return False

    try:
        msg            = MIMEMultipart()
        msg['From']    = SMTP_USER
        msg['To']      = to
        msg['Subject'] = subject

        full_body = body
        if video_url:
            full_body += f"\n\n📹 Video clip: {video_url}"
        if snapshot_url:
            full_body += f"\n📷 Snapshot:   {snapshot_url}"

        msg.attach(MIMEText(full_body, 'plain'))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.set_debuglevel(1)
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, to, msg.as_string())

        print(f"[EMAIL] ✅ Sent to '{to}'")
        return True

    except smtplib.SMTPAuthenticationError as e:
        print(f"[EMAIL] ❌ Auth failed — regenerate App Password in Google Account: {e}")
    except smtplib.SMTPRecipientsRefused as e:
        print(f"[EMAIL] ❌ Recipient refused: {e}")
    except smtplib.SMTPConnectError as e:
        print(f"[EMAIL] ❌ Could not connect to SMTP server: {e}")
    except smtplib.SMTPException as e:
        print(f"[EMAIL] ❌ SMTP error: {e}")
    except Exception as e:
        print(f"[EMAIL] ❌ Unexpected error: {e}")

    return False


def upload_to_firebase_storage(local_path: str, remote_path: str,
                               content_type: str) -> str | None:
    try:
        blob = bucket.blob(remote_path)
        blob.upload_from_filename(local_path, content_type=content_type)
        blob.make_public()
        print(f"[STORAGE] ✅ {local_path} → {blob.public_url}")
        return blob.public_url
    except Exception as e:
        print(f"[STORAGE] ❌ Upload error: {e}")
        return None


def _do_send_alert(session_id: str, device_id: str, rider_email: str,
                   cam_label: str, location_str: str, speed_str: str,
                   snapshot_url: str | None, video_url: str | None,
                   emergency_email: str | None, reason: str):
    """Common send logic used by both cancel-window expiry and hardware failure."""
    if not emergency_email:
        print(f"[ALERT] ⚠️  No emergency_email for '{rider_email}' — cannot send")
        return

    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if reason == "cancel_window_expired":
        subject = f"🚨 CRASH ALERT — PiCAM [{time_str}]"
        body = (
            f"CRASH ALERT — PiCAM 360\n\n"
            f"Time:     {time_str}\n"
            f"Camera:   {cam_label}\n"
            f"Location: {location_str}\n"
            f"Speed:    {speed_str}\n"
            f"Device:   {device_id}\n\n"
            f"A crash was detected and NOT cancelled within {CRASH_CANCEL_WINDOW} seconds.\n"
        )
    else:
        subject = f"🚨 HARDWARE FAILURE — CRASH ALERT — PiCAM [{time_str}]"
        body = (
            f"⚠️ HARDWARE FAILURE DETECTED\n\n"
            f"Crash detected but the Pi stopped responding (possible power loss).\n\n"
            f"Time:     {time_str}\n"
            f"Camera:   {cam_label}\n"
            f"Location: {location_str}\n"
            f"Speed:    {speed_str}\n"
            f"Device:   {device_id}\n\n"
            f"Alert sent automatically because the Pi lost connection.\n"
        )

    send_email(emergency_email, subject, body, snapshot_url, video_url)

    try:
        db.collection('CrashEvents').document(session_id).update({
            'status':   'sent',
            'sent_at':  datetime.now(),
            'reason':   reason,
            'sent_to':  emergency_email,
        })
    except Exception as e:
        print(f"[FIRESTORE] Error updating status: {e}")


def auto_send_alert(session_id: str, device_id: str, rider_email: str,
                    cam_label: str, location_str: str, speed_str: str,
                    snapshot_url: str | None, video_url: str | None,
                    emergency_email: str | None):
    """
    Starts the 10s cancel window in a background thread.
    emergency_email is already resolved before this is called.
    """

    def _wait_and_send():
        print(f"[CRASH] ⏳ {session_id} — {CRASH_CANCEL_WINDOW}s cancel window started")
        start = time.time()

        while time.time() - start < CRASH_CANCEL_WINDOW:
            time.sleep(0.3)
            with crash_lock:
                entry = active_crashes.get(session_id)
                if entry and entry["cancelled"]:
                    print(f"[CRASH] ✅ {session_id} cancelled within window")
                    return
                if not entry:
                    # Already removed (e.g. by hardware failure handler)
                    print(f"[CRASH] ⚠️  {session_id} disappeared from active_crashes")
                    return

        print(f"[CRASH] ⌛ {session_id} — cancel window expired, sending alert")
        _do_send_alert(session_id, device_id, rider_email, cam_label,
                       location_str, speed_str, snapshot_url, video_url,
                       emergency_email, "cancel_window_expired")

        with crash_lock:
            active_crashes.pop(session_id, None)

    t = threading.Thread(target=_wait_and_send, daemon=True)
    t.start()

    with crash_lock:
        active_crashes[session_id] = {
            "confirmed_at":   time.time(),
            "cancelled":      False,
            "timer":          t,
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
    print(f"[CRASH] {session_id} registered in active_crashes")


def check_heartbeats_worker():
    """Kill-switch: if Pi stops heartbeating, assume hardware failure and send alert."""
    print("[HEARTBEAT] Monitor started")
    while True:
        time.sleep(HEARTBEAT_CHECK_INTERVAL)
        now          = time.time()
        dead_sessions = []

        with crash_lock:
            for sid, data in list(active_crashes.items()):
                if data.get("cancelled"):
                    continue
                since_hb = now - data.get("last_heartbeat", now)
                if since_hb > HEARTBEAT_TIMEOUT:
                    print(f"[HEARTBEAT] 💀 {data['device_id']} silent for {since_hb:.1f}s")
                    dead_sessions.append((sid, dict(data)))

        for sid, data in dead_sessions:
            _do_send_alert(
                sid, data["device_id"], data["rider_email"],
                data["cam_label"], data["location_str"], data["speed_str"],
                data["snapshot_url"], data["video_url"],
                data.get("emergency_email"),
                "hardware_failure_no_heartbeat"
            )
            with crash_lock:
                active_crashes.pop(sid, None)


# ═════════════════════════════════════════════════════
# ROUTES
# ═════════════════════════════════════════════════════

@app.route('/api/crash/alert', methods=['POST'])
def crash_alert():
    """
    Pi sends crash alert WITH video/snapshot (base64).
    Immediately starts the 10s cancel window on the Relay side.
    Pi continues sending heartbeats during this window.
    """
    try:
        data = request.get_json()

        # Log payload (skip large base64 fields)
        safe = {k: v for k, v in data.items()
                if k not in ('snapshot_base64', 'video_base64')}
        print(f"[ALERT] ═══ Incoming alert ═══")
        print(f"[ALERT] {json.dumps(safe, indent=2)}")

        session_id   = data.get('session_id')
        device_id    = data.get('device_id')
        rider_email  = data.get('rider_email')
        cam_label    = data.get('cam_label',  'UNKNOWN')
        location     = data.get('location',   'Unknown')
        speed        = data.get('speed',      'Unknown')
        snapshot_b64 = data.get('snapshot_base64')
        video_b64    = data.get('video_base64')

        print(f"[ALERT] rider_email     = '{rider_email}'")
        print(f"[ALERT] snapshot present = {bool(snapshot_b64)}")
        print(f"[ALERT] video present    = {bool(video_b64)}")

        # ── Resolve emergency contact right now ────────────────────────────
        emergency_email = get_emergency_contact(rider_email)
        print(f"[ALERT] emergency_email  = '{emergency_email}'")
        if not emergency_email:
            print(f"[ALERT] ⚠️  WARNING — no emergency contact found, email will NOT send")

        # ── Upload media ───────────────────────────────────────────────────
        snapshot_url = None
        video_url    = None

        if snapshot_b64:
            try:
                data_bytes = base64.b64decode(snapshot_b64)
                tmp = f"/tmp/{session_id}_snapshot.jpg"
                with open(tmp, 'wb') as f:
                    f.write(data_bytes)
                snapshot_url = upload_to_firebase_storage(
                    tmp, f"crashes/{session_id}/snapshot.jpg", "image/jpeg")
            except Exception as e:
                print(f"[STORAGE] ❌ Snapshot error: {e}")

        if video_b64:
            try:
                data_bytes = base64.b64decode(video_b64)
                tmp = f"/tmp/{session_id}_crash.mp4"
                with open(tmp, 'wb') as f:
                    f.write(data_bytes)
                video_url = upload_to_firebase_storage(
                    tmp, f"crashes/{session_id}/crash_clip.mp4", "video/mp4")
            except Exception as e:
                print(f"[STORAGE] ❌ Video error: {e}")

        # ── Store in Firestore ─────────────────────────────────────────────
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
            print(f"[FIRESTORE] ❌ Store error: {e}")
            return jsonify({"error": "Failed to store crash event"}), 500

        # ── Start cancel window (Pi must heartbeat + may cancel within 10s) ─
        auto_send_alert(
            session_id=session_id, device_id=device_id,
            rider_email=rider_email, cam_label=cam_label,
            location_str=location, speed_str=speed,
            snapshot_url=snapshot_url, video_url=video_url,
            emergency_email=emergency_email,
        )

        # ── Register heartbeat ─────────────────────────────────────────────
        with heartbeat_lock:
            device_heartbeats[device_id] = {
                "last_heartbeat": time.time(),
                "session_id":     session_id,
            }

        return jsonify({
            "success":               True,
            "message":               f"Alert received. {CRASH_CANCEL_WINDOW}s cancel window started.",
            "session_id":            session_id,
            "emergency_email_found": bool(emergency_email),
        }), 200

    except Exception as e:
        print(f"[ALERT] ❌ Unhandled exception: {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/heartbeat', methods=['POST'])
def crash_heartbeat():
    """Pi proves it is still alive by calling this every 2s."""
    try:
        data       = request.get_json()
        session_id = data.get('session_id')
        device_id  = data.get('device_id')

        with crash_lock:
            if session_id in active_crashes:
                active_crashes[session_id]["last_heartbeat"] = time.time()
                print(f"[HEARTBEAT] ❤️  {device_id}/{session_id}")
            else:
                print(f"[HEARTBEAT] ⚠️  Unknown session '{session_id}' (resolved already?)")

        with heartbeat_lock:
            if device_id in device_heartbeats:
                device_heartbeats[device_id]["last_heartbeat"] = time.time()

        return jsonify({"success": True}), 200

    except Exception as e:
        print(f"[HEARTBEAT] ❌ {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/cancel', methods=['POST'])
def crash_cancel():
    """Pi or app cancels within the cancel window."""
    try:
        data       = request.get_json()
        session_id = data.get('session_id')

        print(f"[CANCEL] Request for '{session_id}'")

        with crash_lock:
            if session_id in active_crashes:
                active_crashes[session_id]["cancelled"] = True
                del active_crashes[session_id]
                print(f"[CANCEL] ✅ {session_id} cancelled")
            else:
                print(f"[CANCEL] ⚠️  '{session_id}' not in active_crashes — window may have expired")

        try:
            db.collection('CrashEvents').document(session_id).update({
                'status':       'cancelled',
                'cancelled_at': datetime.now(),
            })
        except Exception as e:
            print(f"[FIRESTORE] ❌ Cancel update error: {e}")

        return jsonify({"success": True, "message": "Crash cancelled."}), 200

    except Exception as e:
        print(f"[CANCEL] ❌ {e}")
        return jsonify({"error": str(e)}), 400


@app.route('/api/crash/status/<session_id>', methods=['GET'])
def crash_status(session_id):
    try:
        doc = db.collection('CrashEvents').document(session_id).get()
        if not doc.exists:
            return jsonify({"error": "Not found"}), 404
        return jsonify(doc.to_dict()), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ─────────────────── DEBUG ENDPOINTS ────────────────
# Remove before final production deploy

@app.route('/api/debug/contact/<path:rider_email>', methods=['GET'])
def debug_contact(rider_email):
    """Test TrustedContact lookup. GET /api/debug/contact/rafhaelmaglunob02@gmail.com"""
    print(f"[DEBUG] Contact lookup test for '{rider_email}'")
    contact = get_emergency_contact(rider_email)
    return jsonify({
        "rider_email":     rider_email,
        "emergency_email": contact,
        "found":           bool(contact),
    }), 200


@app.route('/api/debug/smtp', methods=['GET'])
def debug_smtp():
    """Send a test email to both SMTP_USER and idleheroes20009@gmail.com"""
    results = {}

    # Test to sender (motosphere.smart)
    results["to_smtp_user"] = send_email(
        to      = SMTP_USER,
        subject = "MotoSphere SMTP Self-Test",
        body    = "SMTP is working. This is a self-test from the Relay backend.",
    )

    # Test to actual emergency contact
    results["to_emergency"] = send_email(
        to      = "idleheroes20009@gmail.com",
        subject = "MotoSphere SMTP Test — Emergency Contact",
        body    = "If you see this, email delivery to the emergency contact is working correctly.",
    )

    return jsonify({
        "smtp_user":     SMTP_USER,
        "results":       results,
        "all_ok":        all(results.values()),
    }), 200


@app.route('/api/debug/full_test', methods=['GET'])
def debug_full_test():
    """
    Simulate a full crash alert flow without needing the Pi:
    1. Lookup contact for rafhaelmaglunob02@gmail.com
    2. Send a test crash email to that contact
    GET /api/debug/full_test
    """
    rider_email = "rafhaelmaglunob02@gmail.com"
    contact     = get_emergency_contact(rider_email)

    if not contact:
        return jsonify({
            "error":       "No TrustedContact found",
            "rider_email": rider_email,
            "tip":         "Check Firestore: contactEmail field must match exactly, status must be 'accepted'"
        }), 404

    sent = send_email(
        to       = contact,
        subject  = "🚨 TEST CRASH ALERT — PiCAM",
        body     = (
            f"This is a TEST crash alert.\n\n"
            f"Rider:    {rider_email}\n"
            f"Contact:  {contact}\n"
            f"Time:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"If you received this, the full pipeline is working correctly."
        ),
    )

    return jsonify({
        "rider_email":    rider_email,
        "emergency_email": contact,
        "email_sent":     sent,
    }), 200 if sent else 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "active_crashes": len(active_crashes)}), 200


@app.route('/', methods=['GET'])
def index():
    return jsonify({
        "service": "PiCAM Relay v2.2",
        "endpoints": {
            "POST /api/crash/alert":                "Pi sends crash",
            "POST /api/crash/heartbeat":            "Pi heartbeat",
            "POST /api/crash/cancel":               "Cancel crash",
            "GET  /api/crash/status/<id>":          "Crash status",
            "GET  /api/debug/contact/<email>":      "Test contact lookup",
            "GET  /api/debug/smtp":                 "Test SMTP send",
            "GET  /api/debug/full_test":            "Full end-to-end test",
            "GET  /health":                         "Health check",
        }
    }), 200


if __name__ == '__main__':
    threading.Thread(target=check_heartbeats_worker, daemon=True).start()
    port = int(os.getenv('PORT', 5000))
    print(f"[RELAY] Starting on port {port}")
    print(f"[RELAY] SMTP_USER = {SMTP_USER}")
    app.run(host='0.0.0.0', port=port, debug=False)

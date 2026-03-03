# ─────────────────── CLOUD SENDER (FIXED) ───────────────────
# Replace the existing cloud_sender() function in pi_server.py with this:

def cloud_sender():
    if not CLOUD_ENABLED: return
    session = req_lib.Session()
    session.headers.update({"X-Secret": PUSH_SECRET})
    lf = ls = lg = lm = lml = 0
    last_sent = None
    print(f"[CLOUD] Sender started → {CLOUD_URL}")

    while True:
        now = time.time()

        # ── Frame: push every 200ms (5fps) for low-latency SSE ──
        if now - lf >= 0.2:
            with combined_frame_lock: frame = combined_frame
            if frame and frame is not last_sent:
                try:
                    r = session.post(f"{CLOUD_URL}/push/frame", data=frame,
                                     headers={"Content-Type": "image/jpeg"}, timeout=4)
                    if r.status_code == 200:
                        last_sent = frame
                        lf = now
                    elif r.status_code == 401:
                        print("[CLOUD] ❌ Wrong secret — check PUSH_SECRET")
                except Exception as e:
                    print(f"[CLOUD] Frame: {e}")

        # ── ML results: push every 1s ────────────────────────
        if now - lm >= 1.0:
            with confirm_lock:
                payload = {
                    str(idx): {
                        "confirmed":    confirm_state[idx]["confirmed"],
                        "elapsed":      round(confirm_state[idx]["elapsed"], 1),
                        "boxes":        confirm_state[idx]["boxes"],
                        "first_seen":   confirm_state[idx]["first_seen"] is not None,
                        "confirm_secs": CRASH_CONFIRM_SECONDS,
                    }
                    for idx in (0, 1)
                }
            try:
                session.post(f"{CLOUD_URL}/push/ml", json=payload, timeout=4)
                lm = now
            except Exception as e:
                print(f"[CLOUD] ML: {e}")

        # ── Camera status: push every 10s ────────────────────
        if now - ls >= 10.0:
            s = {str(idx): cameras[idx]["status"] for idx in (0, 1)}
            try:
                session.post(f"{CLOUD_URL}/push/status", json=s, timeout=4)
                ls = now
            except Exception as e:
                print(f"[CLOUD] Status: {e}")

        # ── GPS: push every 10s ──────────────────────────────
        if now - lg >= 10.0:
            with gps_state_lock: gps = gps_state.copy()
            if gps.get('lat') and gps.get('lon'):
                try:
                    session.post(f"{CLOUD_URL}/push/gps", json=gps, timeout=4)
                    lg = now
                except Exception as e:
                    print(f"[CLOUD] GPS: {e}")

        # ── Poll cloud for ML toggle command every 2s ────────
        # This lets the cloud dashboard turn ML on/off on the Pi
        if now - lml >= 2.0:
            try:
                r = session.get(f"{CLOUD_URL}/pi/ml_state",
                                params={"secret": PUSH_SECRET}, timeout=4)
                if r.status_code == 200:
                    cloud_ml = r.json().get("enabled", False)
                    global ml_detection_enabled
                    if cloud_ml != ml_detection_enabled:
                        ml_detection_enabled = cloud_ml
                        print(f"[CLOUD] ML toggled by cloud dashboard: {cloud_ml}")
                lml = now
            except Exception as e:
                pass  # silent — not critical

        time.sleep(0.15)  # tighter loop for faster frame push
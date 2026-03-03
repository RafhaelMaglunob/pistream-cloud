#!/usr/bin/env python3
"""
pi_cloud_client.py — WebSocket + WebRTC client for the Pi.

HOW TO USE:
  Add these lines at the bottom of pi_server.py __main__ block:

    from pi_cloud_client import PiCloudClient
    cloud = PiCloudClient(cameras, combined_frame_lock, confirm_state, gps_state)
    cloud.start()

INSTALL:
  pip install aiortc aiohttp websockets av pillow numpy
"""

import asyncio
import threading
import time
import io
import json
import base64
import requests

import numpy as np
from PIL import Image

try:
    import websockets
    import aiohttp
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate, VideoStreamTrack
    import av
    WEBRTC_OK = True
except ImportError as e:
    WEBRTC_OK = False
    print(f"[CLOUD] Missing dependency: {e}")
    print("[CLOUD] Run: pip install aiortc aiohttp websockets av")

CLOUD_WS_URL = "wss://pistream-cloud.onrender.com/ws/pi"
CLOUD_URL    = "https://pistream-cloud.onrender.com"
PUSH_SECRET  = "Rafhael@1"


class PiCloudClient:
    def __init__(self, cameras_ref, combined_frame_lock_ref,
                 confirm_state_ref, gps_state_ref,
                 confirm_secs=3, cooldown_secs=10):
        self.cameras              = cameras_ref
        self.combined_frame_lock  = combined_frame_lock_ref
        self.confirm_state        = confirm_state_ref
        self.gps_state            = gps_state_ref
        self.CRASH_CONFIRM_SECS   = confirm_secs
        self.CRASH_COOLDOWN_SECS  = cooldown_secs

        # These are set by pi_server global variable references
        self._combined_frame_ref  = None   # set via set_combined_frame_ref()

        self._pc   = None
        self._ws   = None
        self._loop = None

    def set_combined_frame_ref(self, ref_fn):
        """Pass a callable that returns the current combined_frame bytes."""
        self._combined_frame_ref = ref_fn

    def start(self):
        """Start background threads for WebRTC + HTTP fallback sender."""
        t1 = threading.Thread(target=self._run_ws_loop, daemon=True)
        t1.start()
        t2 = threading.Thread(target=self._http_sender_loop, daemon=True)
        t2.start()
        print("[CLOUD] PiCloudClient started")

    # ── WebSocket + WebRTC loop ───────────────────────────────
    def _run_ws_loop(self):
        time.sleep(8)   # wait for cameras to warm up
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._ws_main())

    async def _ws_main(self):
        uri = f"{CLOUD_WS_URL}?secret={PUSH_SECRET}"
        while True:
            try:
                print(f"[CLOUD-WS] Connecting to {uri}...")
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=10,
                    open_timeout=15,
                ) as ws:
                    self._ws = ws
                    print("[CLOUD-WS] ✅ Connected")
                    await self._session(ws)
            except Exception as e:
                print(f"[CLOUD-WS] Error: {e}")
            finally:
                self._ws = None
                if self._pc:
                    try: await self._pc.close()
                    except: pass
                    self._pc = None
            print("[CLOUD-WS] Reconnecting in 5s...")
            await asyncio.sleep(5)

    async def _session(self, ws):
        """Handle one WebSocket session."""
        # Create peer connection and send an initial offer
        await self._new_pc(ws)

        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            t = data.get('type')

            if t == 'request_offer':
                print("[CLOUD-WS] Viewer asked for offer — creating...")
                await self._new_pc(ws)

            elif t == 'answer':
                print("[CLOUD-WS] Got SDP answer from viewer")
                if self._pc:
                    try:
                        sdp = data['sdp']
                        await self._pc.setRemoteDescription(
                            RTCSessionDescription(type=sdp['type'], sdp=sdp['sdp'])
                        )
                    except Exception as e:
                        print(f"[CLOUD-WS] setRemoteDescription: {e}")

            elif t == 'ice':
                if self._pc and data.get('candidate'):
                    try:
                        c = data['candidate']
                        # candidate dict from aiortc browser
                        await self._pc.addIceCandidate(
                            RTCIceCandidate(
                                candidate     = c.get('candidate',''),
                                sdpMid        = c.get('sdpMid',''),
                                sdpMLineIndex = c.get('sdpMLineIndex', 0),
                            )
                        )
                    except Exception as e:
                        print(f"[CLOUD-WS] addIceCandidate: {e}")

    async def _new_pc(self, ws):
        """Create a new RTCPeerConnection and send an offer."""
        if not WEBRTC_OK:
            return

        if self._pc:
            try: await self._pc.close()
            except: pass

        self._pc = RTCPeerConnection()

        # Add both camera tracks
        for cam_idx in (0, 1):
            track = _CameraTrack(cam_idx, self.cameras)
            self._pc.addTrack(track)

        @self._pc.on("icecandidate")
        async def on_ice(candidate):
            if candidate and self._ws:
                try:
                    await self._ws.send(json.dumps({
                        "type": "ice",
                        "candidate": {
                            "candidate":     candidate.candidate,
                            "sdpMid":        candidate.sdpMid,
                            "sdpMLineIndex": candidate.sdpMLineIndex,
                        }
                    }))
                except Exception:
                    pass

        @self._pc.on("connectionstatechange")
        async def on_state():
            s = self._pc.connectionState
            print(f"[CLOUD-RTC] State: {s}")

        try:
            offer = await self._pc.createOffer()
            await self._pc.setLocalDescription(offer)
            await ws.send(json.dumps({
                "type": "offer",
                "sdp": {
                    "type": self._pc.localDescription.type,
                    "sdp":  self._pc.localDescription.sdp,
                }
            }))
            print("[CLOUD-RTC] Offer sent")
        except Exception as e:
            print(f"[CLOUD-RTC] createOffer error: {e}")

    # ── HTTP fallback sender ──────────────────────────────────
    def _http_sender_loop(self):
        """
        Keeps sending snapshots + ML + status + GPS via HTTP.
        This powers the SSE fallback and the dashboard data.
        """
        sess = requests.Session()
        sess.headers.update({"X-Secret": PUSH_SECRET})

        lf = ls = lg = lm = 0.0
        last_frame = None

        while True:
            now = time.time()

            # ── Snapshot (fallback for SSE) ───────────────
            if now - lf >= 0.5:   # 2fps fallback
                frame = self._get_combined_frame()
                if frame and frame is not last_frame:
                    try:
                        r = sess.post(f"{CLOUD_URL}/push/frame",
                                      data=frame,
                                      headers={"Content-Type": "image/jpeg"},
                                      timeout=6)
                        if r.status_code == 200:
                            last_frame = frame
                            lf = now
                    except Exception as e:
                        print(f"[CLOUD-HTTP] Frame: {e}")

            # ── ML / crash state ──────────────────────────
            if now - lm >= 2.0:
                try:
                    with threading.Lock():   # confirm_state already thread-safe
                        payload = {
                            str(idx): {
                                "confirmed":   self.confirm_state[idx]["confirmed"],
                                "elapsed":     round(self.confirm_state[idx]["elapsed"], 1),
                                "boxes":       self.confirm_state[idx]["boxes"],
                                "first_seen":  self.confirm_state[idx]["first_seen"] is not None,
                                "confirm_secs": self.CRASH_CONFIRM_SECS,
                            }
                            for idx in (0, 1)
                        }
                    sess.post(f"{CLOUD_URL}/push/ml", json=payload, timeout=5)
                    lm = now
                except Exception as e:
                    print(f"[CLOUD-HTTP] ML: {e}")

            # ── Camera status ─────────────────────────────
            if now - ls >= 10.0:
                try:
                    status = {
                        str(idx): self.cameras[idx]["status"]
                        for idx in (0, 1)
                    }
                    sess.post(f"{CLOUD_URL}/push/status", json=status, timeout=5)
                    ls = now
                except Exception as e:
                    print(f"[CLOUD-HTTP] Status: {e}")

            # ── GPS ───────────────────────────────────────
            if now - lg >= 10.0:
                try:
                    if self.gps_state.get('lat') and self.gps_state.get('lon'):
                        sess.post(f"{CLOUD_URL}/push/gps",
                                  json=self.gps_state, timeout=5)
                        lg = now
                except Exception as e:
                    print(f"[CLOUD-HTTP] GPS: {e}")

            time.sleep(0.3)

    def _get_combined_frame(self):
        """Get current combined JPEG bytes from pi_server globals."""
        if self._combined_frame_ref:
            return self._combined_frame_ref()
        # Try importing directly
        try:
            import pi_server as ps
            with ps.combined_frame_lock:
                return ps.combined_frame
        except Exception:
            return None


# ── Camera VideoStreamTrack ───────────────────────────────────
class _CameraTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, cam_idx, cameras):
        super().__init__()
        self.cam_idx  = cam_idx
        self.cameras  = cameras
        self._last    = None

    async def recv(self):
        pts, time_base = await self.next_timestamp()

        cam = self.cameras[self.cam_idx]

        # Prefer overlay frame (has detection boxes), fall back to raw
        frame_bytes = None
        with cam["overlay_lock"]:
            frame_bytes = cam["overlay_frame"]
        if frame_bytes is None:
            with cam["frame_lock"]:
                frame_bytes = cam["latest_frame"]

        if frame_bytes is None or frame_bytes is self._last:
            await asyncio.sleep(0.033)
            return await self.recv()

        self._last = frame_bytes

        try:
            img = Image.open(io.BytesIO(frame_bytes)).convert("RGB")
            frame = av.VideoFrame.from_ndarray(np.array(img), format="rgb24")
            frame.pts       = pts
            frame.time_base = time_base
            return frame
        except Exception:
            await asyncio.sleep(0.033)
            return await self.recv()

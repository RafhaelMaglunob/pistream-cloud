#!/usr/bin/env python3
"""
Pi Cloud Client — WebSocket signaling for WebRTC + Cloud push
Run this alongside your camera-server.py
"""

import asyncio
import websockets
import json
import threading
import time
import requests
import base64
import queue
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCIceCandidate
import av
from PIL import Image
import io

# ─────────────────── CLOUD WEBSOCKET CONFIG ───────────────────
CLOUD_WS_URL = "wss://pistream-cloud.onrender.com/ws/pi"
PUSH_SECRET = "Rafhael@1"
PI_ID = "raspberrypi"
CLOUD_URL = "https://pistream-cloud.onrender.com"  # Add this for HTTP fallback

# WebRTC globals
peer_connection = None
web_socket = None
ws_connected = False
webrtc_active = False
signaling_queue = queue.Queue(maxsize=20)

# We'll need to access these from camera-server.py
# For now, we'll create placeholders - the actual values will come from camera-server
cameras = None
combined_frame = None
confirm_state = None
gps_state = None
CRASH_CONFIRM_SECONDS = 3

def set_globals(cameras_ref, combined_frame_ref, confirm_state_ref, gps_state_ref, confirm_secs):
    """Call this from camera-server.py to pass the global variables"""
    global cameras, combined_frame, confirm_state, gps_state, CRASH_CONFIRM_SECONDS
    cameras = cameras_ref
    combined_frame = combined_frame_ref
    confirm_state = confirm_state_ref
    gps_state = gps_state_ref
    CRASH_CONFIRM_SECONDS = confirm_secs
    print("[CLOUD] Globals set from camera-server.py")
    
def start_cloud_webrtc_client():
    """Start the WebSocket client in a background thread"""
    def run_async_loop():
        asyncio.new_event_loop().run_until_complete(websocket_client())
    
    thread = threading.Thread(target=run_async_loop, daemon=True)
    thread.start()
    print("[CLOUD] WebRTC signaling client started")

async def websocket_client():
    """Main WebSocket connection to relay server"""
    global web_socket, ws_connected, webrtc_active
    
    uri = f"{CLOUD_WS_URL}?pi_id={PI_ID}"
    headers = {"X-Secret": PUSH_SECRET}
    
    while True:
        try:
            print(f"[CLOUD] Connecting to {uri}...")
            async with websockets.connect(uri, extra_headers=headers) as ws:
                web_socket = ws
                ws_connected = True
                print("[CLOUD] ✅ WebSocket connected")
                
                # Start WebRTC peer connection
                await create_peer_connection()
                
                # Listen for messages
                async for message in ws:
                    data = json.loads(message)
                    
                    if data['type'] == 'request_offer':
                        print("[CLOUD] Received offer request, sending SDP...")
                        await send_offer()
                        
                    elif data['type'] == 'answer':
                        print("[CLOUD] Received SDP answer")
                        await handle_answer(data['sdp'])
                        
                    elif data['type'] == 'ice':
                        print("[CLOUD] Received ICE candidate")
                        if peer_connection:
                            cand = data['candidate']
                            candidate = RTCIceCandidate(
                                candidate=cand['candidate'],
                                sdpMid=cand['sdpMid'],
                                sdpMLineIndex=cand['sdpMLineIndex']
                            )
                            await peer_connection.addIceCandidate(candidate)
                            
        except websockets.exceptions.ConnectionClosed:
            print("[CLOUD] WebSocket disconnected, reconnecting...")
            ws_connected = False
            webrtc_active = False
            await asyncio.sleep(3)
        except Exception as e:
            print(f"[CLOUD] WebSocket error: {e}")
            ws_connected = False
            webrtc_active = False
            await asyncio.sleep(5)

async def create_peer_connection():
    """Create RTCPeerConnection with video tracks from your cameras"""
    global peer_connection, webrtc_active
    
    class PiVideoStreamTrack(VideoStreamTrack):
        """Video track that pulls frames from your existing camera threads"""
        def __init__(self, cam_idx):
            super().__init__()
            self.cam_idx = cam_idx
            self.last_frame = None
            
        async def recv(self):
            pts, time_base = await self.next_timestamp()
            
            if cameras is None:
                await asyncio.sleep(0.033)
                return await self.recv()
            
            # Try overlay frame first (with detection boxes), fallback to raw
            with cameras[self.cam_idx]["overlay_lock"]:
                frame_bytes = cameras[self.cam_idx]["overlay_frame"]
            if frame_bytes is None:
                with cameras[self.cam_idx]["frame_lock"]:
                    frame_bytes = cameras[self.cam_idx]["latest_frame"]
            
            if frame_bytes is None or frame_bytes is self.last_frame:
                # No new frame, wait
                await asyncio.sleep(0.033)
                return await self.recv()
            
            self.last_frame = frame_bytes
            
            # Convert JPEG bytes to VideoFrame
            img = Image.open(io.BytesIO(frame_bytes))
            frame = av.VideoFrame.from_ndarray(
                np.array(img), format="rgb24"
            )
            frame.pts = pts
            frame.time_base = time_base
            return frame
    
    peer_connection = RTCPeerConnection()
    
    # Add both camera tracks
    for cam_idx in [0, 1]:
        track = PiVideoStreamTrack(cam_idx)
        peer_connection.addTrack(track)
        print(f"[CLOUD] Added camera {cam_idx} track")
    
    @peer_connection.on("icecandidate")
    async def on_icecandidate(candidate):
        if candidate and web_socket and ws_connected:
            await web_socket.send(json.dumps({
                'type': 'ice',
                'candidate': {
                    'candidate': candidate.candidate,
                    'sdpMid': candidate.sdpMid,
                    'sdpMLineIndex': candidate.sdpMLineIndex
                }
            }))
    
    @peer_connection.on("connectionstatechange")
    async def on_connectionstatechange():
        print(f"[CLOUD] WebRTC state: {peer_connection.connectionState}")
        if peer_connection.connectionState == "connected":
            webrtc_active = True
            print("[CLOUD] ✅ WebRTC connected - streaming live!")
        elif peer_connection.connectionState in ["failed", "disconnected", "closed"]:
            webrtc_active = False
            print("[CLOUD] WebRTC disconnected")

async def send_offer():
    """Create and send SDP offer"""
    global peer_connection
    
    if not peer_connection:
        print("[CLOUD] No peer connection")
        return
    
    offer = await peer_connection.createOffer()
    await peer_connection.setLocalDescription(offer)
    
    if web_socket and ws_connected:
        await web_socket.send(json.dumps({
            'type': 'offer',
            'sdp': {
                'type': peer_connection.localDescription.type,
                'sdp': peer_connection.localDescription.sdp
            }
        }))
        print("[CLOUD] 📤 Sent SDP offer")

async def handle_answer(answer_sdp):
    """Handle SDP answer from viewer"""
    global peer_connection
    
    answer = RTCSessionDescription(
        type=answer_sdp['type'],
        sdp=answer_sdp['sdp']
    )
    await peer_connection.setRemoteDescription(answer)
    print("[CLOUD] 📥 Received SDP answer")

# ─────────────────── ENHANCED CLOUD SENDER ───────────────────
def enhanced_cloud_sender():
    """
    Enhanced cloud sender that also reports WebRTC status
    """
    global webrtc_active, ws_connected, combined_frame, cameras, gps_state, confirm_state
    
    session = requests.Session()
    session.headers.update({"X-Secret": PUSH_SECRET})
    
    lf = ls = lg = lm = 0
    last_frame = None
    
    print(f"[CLOUD] Enhanced sender started → {CLOUD_URL}")
    
    while True:
        now = time.time()
        
        # Push frame every 1s (fallback for SSE)
        if now - lf >= 1.0 and combined_frame is not None:
            if combined_frame and combined_frame is not last_frame:
                try:
                    r = session.post(
                        f"{CLOUD_URL}/push/frame",
                        data=combined_frame,
                        headers={"Content-Type": "image/jpeg"},
                        timeout=8
                    )
                    if r.status_code == 200:
                        last_frame = combined_frame
                        lf = now
                        print(f"[CLOUD] Frame pushed, size: {len(combined_frame)} bytes")
                    elif r.status_code == 401:
                        print("[CLOUD] ❌ Wrong secret")
                except Exception as e:
                    print(f"[CLOUD] Frame push error: {e}")
        
        # Push ML results every 2s
        if now - lm >= 2.0 and confirm_state is not None:
            try:
                payload = {
                    str(idx): {
                        "confirmed": confirm_state[idx]["confirmed"],
                        "elapsed": round(confirm_state[idx]["elapsed"], 1),
                        "boxes": confirm_state[idx]["boxes"],
                        "first_seen": confirm_state[idx]["first_seen"] is not None,
                        "confirm_secs": CRASH_CONFIRM_SECONDS
                    } for idx in (0, 1)
                }
                session.post(f"{CLOUD_URL}/push/ml", json=payload, timeout=5)
                lm = now
            except Exception as e:
                print(f"[CLOUD] ML push error: {e}")
        
        # Push status every 10s (including WebRTC status)
        if now - ls >= 10.0 and cameras is not None:
            try:
                status = {
                    "0": cameras[0]["status"],
                    "1": cameras[1]["status"],
                    "webrtc": "Active" if webrtc_active else "Inactive",
                    "websocket": "Connected" if ws_connected else "Disconnected"
                }
                session.post(f"{CLOUD_URL}/push/status", json=status, timeout=5)
                ls = now
            except Exception as e:
                print(f"[CLOUD] Status push error: {e}")
        
        # Push GPS every 10s
        if now - lg >= 10.0 and gps_state is not None:
            try:
                if gps_state['lat'] and gps_state['lon']:
                    session.post(f"{CLOUD_URL}/push/gps", json=gps_state, timeout=5)
                    lg = now
            except Exception as e:
                print(f"[CLOUD] GPS push error: {e}")
        
        time.sleep(0.5)

if __name__ == "__main__":
    print("Starting Pi Cloud WebRTC Client...")
    print("NOTE: This should be run alongside camera-server.py")
    print("The globals will be set by camera-server.py when it calls set_globals()")
    start_cloud_webrtc_client()
    
    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down...")

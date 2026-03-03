#!/usr/bin/env python3
"""
Pi Cloud Client — WebSocket signaling for WebRTC + Cloud push
Add this to your existing camera-server.py to enable WebRTC streaming
"""

import asyncio
import websockets
import json
import threading
import time
import requests
import base64
import queue
from functools import partial

# ─────────────────── CLOUD WEBSOCKET CONFIG ───────────────────
CLOUD_WS_URL = "wss://pistream-cloud.onrender.com/ws/pi"
PUSH_SECRET = "Rafhael@1"
PI_ID = "raspberrypi"

# WebRTC globals
peer_connection = None
web_socket = None
ws_connected = False
webrtc_active = False
signaling_queue = queue.Queue(maxsize=20)

# Reference to your existing cameras dict (imported from main)
# You'll need to make cameras accessible, e.g., by importing from your main module
# For now, we'll assume we can access it globally

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
                            from aiortc import RTCIceCandidate
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
    
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    import av
    
    class PiVideoStreamTrack(VideoStreamTrack):
        """Video track that pulls frames from your existing camera threads"""
        def __init__(self, cam_idx):
            super().__init__()
            self.cam_idx = cam_idx
            self.last_frame = None
            
        async def recv(self):
            pts, time_base = await self.next_timestamp()
            
            # Get the latest frame from your camera's overlay_frame or latest_frame
            from main import cameras  # Import your cameras dict
            
            # Try overlay frame first (with detection boxes), fallback to raw
            with cameras[self.cam_idx]["overlay_lock"]:
                frame_bytes = cameras[self.cam_idx]["overlay_frame"]
            if frame_bytes is None:
                with cameras[self.cam_idx]["frame_lock"]:
                    frame_bytes = cameras[self.cam_idx]["latest_frame"]
            
            if frame_bytes is None or frame_bytes is self.last_frame:
                # No new frame, return a blank frame or wait
                await asyncio.sleep(0.033)
                return await self.recv()
            
            self.last_frame = frame_bytes
            
            # Convert JPEG bytes to VideoFrame
            import io
            from PIL import Image
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
    
    from aiortc import RTCSessionDescription
    answer = RTCSessionDescription(
        type=answer_sdp['type'],
        sdp=answer_sdp['sdp']
    )
    await peer_connection.setRemoteDescription(answer)
    print("[CLOUD] 📥 Received SDP answer")

# ─────────────────── ENHANCED CLOUD SENDER ───────────────────
# Replace your existing cloud_sender() with this enhanced version

def enhanced_cloud_sender():
    """
    Enhanced cloud sender that also reports WebRTC status
    Replace your existing cloud_sender() with this
    """
    global webrtc_active, ws_connected
    
    session = requests.Session()
    session.headers.update({"X-Secret": PUSH_SECRET})
    
    lf = ls = lg = lm = 0
    last_frame = None
    
    print(f"[CLOUD] Enhanced sender started → {CLOUD_URL}")
    
    while True:
        now = time.time()
        
        # Push frame every 1s (fallback for SSE)
        if now - lf >= 1.0:
            from main import combined_frame  # Import your combined frame
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
                except Exception as e:
                    print(f"[CLOUD] Frame push error: {e}")
        
        # Push ML results every 2s
        if now - lm >= 2.0:
            try:
                from main import confirm_state, CRASH_CONFIRM_SECONDS
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
            except Exception:
                pass
        
        # Push status every 10s (including WebRTC status)
        if now - ls >= 10.0:
            try:
                from main import cameras
                status = {
                    "0": cameras[0]["status"],
                    "1": cameras[1]["status"],
                    "webrtc": "Active" if webrtc_active else "Inactive",
                    "websocket": "Connected" if ws_connected else "Disconnected"
                }
                session.post(f"{CLOUD_URL}/push/status", json=status, timeout=5)
                ls = now
            except Exception:
                pass
        
        # Push GPS every 10s
        if now - lg >= 10.0:
            try:
                from main import gps_state
                if gps_state['lat'] and gps_state['lon']:
                    session.post(f"{CLOUD_URL}/push/gps", json=gps_state, timeout=5)
                    lg = now
            except Exception:
                pass
        
        time.sleep(0.5)

# ─────────────────── INTEGRATION WITH YOUR MAIN ───────────────────
"""
To integrate this with your existing camera-server.py:

1. Add imports at the top of your file:
   import asyncio
   import websockets
   from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
   import av
   import numpy as np

2. Add these lines near the end of your main() function, before app.run():
   # Start WebRTC client
   start_cloud_webrtc_client()
   
   # Replace your existing cloud_sender thread with enhanced version
   # Comment out your old cloud_sender thread and add:
   threading.Thread(target=enhanced_cloud_sender, daemon=True).start()

3. Install required packages on Pi:
   pip install aiortc av websockets numpy Pillow
"""

# If you want to run this standalone for testing
if __name__ == "__main__":
    print("Starting Pi Cloud WebRTC Client...")
    start_cloud_webrtc_client()
    
    # Keep the main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down...")
#!/bin/bash

# One-command installation for Pi Camera Auto-Start
# Usage: curl -sSL https://raw.githubusercontent.com/YOUR_REPO/install.sh | sudo bash

echo "=============================================="
echo "Pi Camera Auto-Start Setup"
echo "=============================================="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "❌ Please run with sudo"
    exit 1
fi

ACTUAL_USER=${SUDO_USER:-pi}
USER_HOME=$(eval echo ~$ACTUAL_USER)

echo "📦 Installing dependencies..."
pip3 install pillow numpy flask --break-system-packages

echo "📝 Creating camera script..."
cat > $USER_HOME/camera_full_color_rgb.py << 'EOFPYTHON'
#!/usr/bin/env python3
from flask import Flask, Response, request, jsonify
import subprocess
import threading
import time
import socket
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import io
import colorsys

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

latest_frame = None
first_frame = None
frame_lock = threading.Lock()
frame_ready = threading.Event()
clients = 0

color_detection_enabled = False
detection_mode = 'center'
detected_colors = []
detection_lock = threading.Lock()

def check_port(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(('127.0.0.1', port))
    sock.close()
    return result != 0

def rgb_to_color_name(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r/255, g/255, b/255)
    h = h * 360
    s = s * 100
    v = v * 100
    
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

def detect_colors_in_frame(frame_bytes, mode='center'):
    global detected_colors
    try:
        img = Image.open(io.BytesIO(frame_bytes))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        img_array = np.array(img)
        height, width = img_array.shape[:2]
        colors = []
        
        if mode == 'center':
            cx, cy = width // 2, height // 2
            sample_size = 30
            y1, y2 = max(0, cy - sample_size), min(height, cy + sample_size)
            x1, x2 = max(0, cx - sample_size), min(width, cx + sample_size)
            region = img_array[y1:y2, x1:x2]
            avg_color = region.mean(axis=(0, 1)).astype(int)
            r, g, b = avg_color
            colors.append({
                'position': 'center',
                'rgb': f'rgb({r},{g},{b})',
                'rgba': f'rgba({r},{g},{b},1)',
                'hex': f'#{r:02x}{g:02x}{b:02x}',
                'name': rgb_to_color_name(r, g, b),
                'coords': (cx, cy),
                'r': int(r), 'g': int(g), 'b': int(b)
            })
        
        with detection_lock:
            detected_colors = colors
    except Exception as e:
        print(f"Color detection error: {e}")

def add_color_overlay(frame_bytes, mode='center'):
    try:
        img = Image.open(io.BytesIO(frame_bytes))
        if img.mode != 'RGB':
            img = img.convert('RGB')
        draw = ImageDraw.Draw(img, 'RGBA')
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
            font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except:
            font = ImageFont.load_default()
            font_small = ImageFont.load_default()
        
        with detection_lock:
            colors = detected_colors.copy()
        
        if mode == 'center' and colors:
            color = colors[0]
            cx, cy = color['coords']
            size = 40
            draw.line([(cx - size, cy), (cx + size, cy)], fill=(0, 255, 0, 255), width=3)
            draw.line([(cx, cy - size), (cx, cy + size)], fill=(0, 255, 0, 255), width=3)
            draw.ellipse([cx - size, cy - size, cx + size, cy + size], outline=(0, 255, 0, 255), width=3)
            
            box_x, box_y = 10, 10
            box_width, box_height = 250, 120
            draw.rectangle([box_x, box_y, box_x + box_width, box_y + box_height],
                          fill=(0, 0, 0, 200), outline=(0, 255, 0, 255), width=2)
            
            swatch_size = 60
            r, g, b = color['r'], color['g'], color['b']
            draw.rectangle([box_x + 10, box_y + 10, box_x + 10 + swatch_size, box_y + 10 + swatch_size],
                          fill=(r, g, b), outline=(255, 255, 255, 255), width=3)
            
            text_x = box_x + swatch_size + 20
            draw.text((text_x, box_y + 10), color['name'], fill=(255, 255, 255, 255), font=font)
            draw.text((text_x, box_y + 35), color['hex'].upper(), fill=(0, 255, 0, 255), font=font_small)
            draw.text((text_x, box_y + 55), f"R:{r} G:{g} B:{b}", fill=(200, 200, 200, 255), font=font_small)
        
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=85)
        return output.getvalue()
    except Exception as e:
        print(f"Overlay error: {e}")
        return frame_bytes

def camera_thread():
    global latest_frame, first_frame
    print("Starting RGB camera...")
    
    process = subprocess.Popen([
        'rpicam-vid', '-t', '0', '--width', '640', '--height', '480',
        '--framerate', '20', '--codec', 'mjpeg', '--quality', '60',
        '--inline', '--nopreview', '--denoise', 'off', '--sharpness', '1.0',
        '--contrast', '1.1', '--saturation', '1.2', '--awb', 'auto',
        '--flush', '1', '-o', '-'
    ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    
    SOI, EOI = b'\xff\xd8', b'\xff\xd9'
    buffer = b''
    
    try:
        while True:
            chunk = process.stdout.read(8192)
            if not chunk: break
            buffer += chunk
            
            while True:
                soi = buffer.find(SOI)
                if soi == -1:
                    if len(buffer) > 2048: buffer = buffer[-2048:]
                    break
                eoi = buffer.find(EOI, soi + 2)
                if eoi == -1:
                    if len(buffer) > 100000: buffer = buffer[soi:]
                    break
                
                frame = buffer[soi:eoi + 2]
                with frame_lock:
                    latest_frame = frame
                    if first_frame is None:
                        first_frame = frame
                        frame_ready.set()
                buffer = buffer[eoi + 2:]
    except Exception as e:
        print(f"Camera error: {e}")
    finally:
        process.terminate()

def generate_frames():
    global clients
    if not frame_ready.wait(timeout=5): return
    
    clients += 1
    client_id = clients
    print(f"[Client {client_id}] Connected")
    
    with frame_lock:
        if first_frame:
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + first_frame + b'\r\n')
    
    last_frame = first_frame
    frame_skip = 0
    
    try:
        while True:
            with frame_lock:
                if latest_frame is not None and latest_frame != last_frame:
                    frame = latest_frame
                    last_frame = frame
                else:
                    time.sleep(0.01)
                    continue
            
            if color_detection_enabled and frame_skip % 2 == 0:
                detect_colors_in_frame(frame, detection_mode)
                frame_to_send = add_color_overlay(frame, detection_mode)
            else:
                frame_to_send = frame
            
            frame_skip += 1
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_to_send + b'\r\n')
    except:
        pass

@app.route('/stream')
def video_stream():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame',
                   headers={'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no'})

@app.route('/detection', methods=['GET', 'POST'])
def set_detection():
    global color_detection_enabled, detection_mode
    if request.method == 'POST':
        data = request.get_json()
        color_detection_enabled = data.get('enabled', False)
        detection_mode = data.get('mode', 'center')
        return jsonify({'success': True, 'enabled': color_detection_enabled, 'mode': detection_mode})
    return jsonify({'enabled': color_detection_enabled, 'mode': detection_mode})

@app.route('/colors')
def get_colors():
    with detection_lock:
        return jsonify({'colors': detected_colors})

@app.route('/')
def index():
    return '''<!DOCTYPE html>
<html><head><title>Pi Camera</title><meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>*{margin:0;padding:0}body{background:#000;color:#0f0;font-family:monospace;padding:20px}
.container{max-width:800px;margin:0 auto}h1{text-align:center;margin-bottom:20px;color:#fff}
#stream{width:100%;border:2px solid #0f0;display:block}
.controls{margin-top:20px;display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.btn{background:#1a1a1a;border:2px solid #0f0;color:#0f0;padding:15px;cursor:pointer;font-size:14px}
.btn:hover{background:#0f0;color:#000}.btn.active{background:#0f0;color:#000}
.status{text-align:center;margin-top:15px;color:#0ff}</style></head>
<body><div class="container"><h1>🎨 Pi Camera - Full Color</h1>
<img id="stream" src="/stream">
<div class="controls">
<button class="btn" onclick="toggleDetection()"><span id="toggle-text">🔍 Enable Detection</span></button>
<button class="btn" onclick="setMode('center')">📍 Center Point</button>
</div><div class="status">Status: <span id="status">Disabled</span></div></div>
<script>let enabled=false,mode='center';
function toggleDetection(){enabled=!enabled;fetch('/detection',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({enabled:enabled,mode:mode})}).then(r=>r.json()).then(data=>{
document.getElementById('toggle-text').textContent=enabled?'🔍 Disable':'🔍 Enable';
document.getElementById('status').textContent=enabled?'Active':'Disabled';})}
function setMode(newMode){mode=newMode;fetch('/detection',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:enabled,mode:mode})})}</script>
</body></html>'''

if __name__ == '__main__':
    ports_to_try = [5000, 5001, 8000, 8080]
    selected_port = None
    for port in ports_to_try:
        if check_port(port):
            selected_port = port
            break
    
    if not selected_port:
        print("ERROR: No ports available!")
        exit(1)
    
    print(f"Starting on port {selected_port}...")
    camera = threading.Thread(target=camera_thread, daemon=True)
    camera.start()
    
    if frame_ready.wait(timeout=5):
        print("✓ Camera ready!")
    
    import logging
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    
    try:
        app.run(host='0.0.0.0', port=selected_port, threaded=True, debug=False)
    except KeyboardInterrupt:
        print("\nShutting down...")
EOFPYTHON

chmod +x $USER_HOME/camera_full_color_rgb.py
chown $ACTUAL_USER:$ACTUAL_USER $USER_HOME/camera_full_color_rgb.py

echo "🔧 Creating systemd service..."
cat > /etc/systemd/system/picamera.service << EOF
[Unit]
Description=Pi Camera Color Detection Server
After=network.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$USER_HOME
ExecStart=/usr/bin/python3 $USER_HOME/camera_full_color_rgb.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable picamera.service
systemctl start picamera.service

echo ""
echo "=============================================="
echo "✅ Installation Complete!"
echo "=============================================="
echo ""
echo "Your Pi camera will now start on boot!"
echo ""
echo "📱 Access from your phone:"
echo "  Open browser and go to: http://$(hostname -I | awk '{print $1}'):5000"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status picamera"
echo "  sudo systemctl restart picamera"
echo "  sudo journalctl -u picamera -f"
echo ""

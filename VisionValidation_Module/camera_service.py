"""
Local Camera Control API Bridge for Vision Validation Module
Smart Storage FYP
Author: Daren Tan Khai Shuen

Runs a lightweight HTTP server on port 8008 providing:
- On-demand single-frame snapshot capture with latency metrics (GET /test_feed)
- Desktop OpenCV Shelf ROI Calibration launching (POST /run_calibration)
- Health check (GET /status)
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Add backend directory to sys.path
BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "backend"))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

try:
    import cv2
    _OPENCV_AVAILABLE = True
except ImportError:
    _OPENCV_AVAILABLE = False

try:
    from src.config_loader import get_active_rtsp_url, mask_rtsp_url
    _CONFIG_LOADER_AVAILABLE = True
except ImportError:
    _CONFIG_LOADER_AVAILABLE = False

    def mask_rtsp_url(url):
        if not isinstance(url, str):
            return str(url)
        return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:****@", url)

    def get_active_rtsp_url(default_source=0):
        return default_source


def capture_single_frame(source_str):
    """
    Connects to an RTSP or webcam source, captures a single frame, and measures latency.
    Forces TCP transport for RTSP feeds to prevent UDP frame corruption on Hikvision cameras.
    :return: dict with status, latency_ms, dimensions, and base64 image data URL
    """
    if not _OPENCV_AVAILABLE:
        return {
            "status": "failed",
            "error": "OpenCV (cv2) is not installed in the current Python environment.",
            "latency_ms": 0,
        }

    # Determine numeric or string source
    try:
        source = int(source_str)
        is_webcam = True
    except (ValueError, TypeError):
        source = str(source_str)
        is_webcam = False

    masked = mask_rtsp_url(source)
    print(f"[CameraService] Capturing on-demand test frame from: {masked}")

    t_start = time.time()
    cap = None

    try:
        if is_webcam:
            cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)
        else:
            # Enforce TCP transport for Hikvision IP cameras to eliminate packet drop
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"
            cap = cv2.VideoCapture(source)

        if not cap.isOpened():
            latency = round((time.time() - t_start) * 1000, 1)
            return {
                "status": "failed",
                "error": f"Failed to open video source ({masked}). Check camera IP, power, and credentials.",
                "latency_ms": latency,
                "source": masked,
            }

        # Set low buffer size
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        # Read single frame
        ret, frame = cap.read()
        latency = round((time.time() - t_start) * 1000, 1)

        if not ret or frame is None:
            return {
                "status": "failed",
                "error": f"Connection established but failed to read frame buffer from {masked}.",
                "latency_ms": latency,
                "source": masked,
            }

        h, w = frame.shape[:2]
        channels = frame.shape[2] if len(frame.shape) > 2 else 1

        # Encode to JPEG in memory
        success, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not success:
            return {
                "status": "failed",
                "error": "Failed to encode captured frame to JPEG format.",
                "latency_ms": latency,
                "source": masked,
            }

        b64_str = base64.b64encode(buffer).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{b64_str}"

        return {
            "status": "success",
            "latency_ms": latency,
            "width": w,
            "height": h,
            "channels": channels,
            "image": data_url,
            "source": masked,
        }

    except Exception as exc:
        latency = round((time.time() - t_start) * 1000, 1)
        return {
            "status": "failed",
            "error": f"Exception during frame capture: {str(exc)}",
            "latency_ms": latency,
            "source": masked,
        }
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass


class CameraServiceRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for camera control endpoints."""

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ["/", "/status", "/health"]:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            payload = {
                "status": "ok",
                "service": "Hikvision Camera Control Service",
                "opencv_available": _OPENCV_AVAILABLE,
                "port": 8008,
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        if path == "/test_feed":
            source_param = query.get("source", [None])[0]
            if not source_param:
                # Fallback to active camera from Supabase
                source_param = get_active_rtsp_url(default_source=0)

            result = capture_single_frame(source_param)
            self.send_response(200 if result.get("status") == "success" else 502)
            self.send_header("Content-Type", "application/json")
            self._send_cors_headers()
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
            return

        self.send_response(404)
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Endpoint not found"}).encode("utf-8"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/run_calibration":
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            body = {}
            if body_bytes:
                try:
                    body = json.loads(body_bytes.decode("utf-8"))
                except Exception:
                    pass

            source = body.get("source")
            if not source:
                source = get_active_rtsp_url(default_source=0)

            # Path to calibrate_roi.py
            script_path = os.path.join(BACKEND_DIR, "calibrate_roi.py")
            if not os.path.isfile(script_path):
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(
                    json.dumps({"status": "failed", "error": f"Script not found at {script_path}"}).encode("utf-8")
                )
                return

            masked = mask_rtsp_url(source)
            print(f"[CameraService] Spawning ROI Calibration GUI for: {masked}")

            try:
                cmd = [sys.executable, script_path, "--source", str(source)]
                proc = subprocess.Popen(
                    cmd,
                    cwd=BACKEND_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self._send_cors_headers()
                self.end_headers()
                payload = {
                    "status": "launched",
                    "pid": proc.pid,
                    "source": masked,
                    "message": "Shelf ROI Calibration window opened on local desktop.",
                }
                self.wfile.write(json.dumps(payload).encode("utf-8"))
            except Exception as exc:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self._send_cors_headers()
                self.end_headers()
                self.wfile.write(
                    json.dumps({"status": "failed", "error": f"Failed to spawn calibration process: {str(exc)}"}).encode(
                        "utf-8"
                    )
                )
            return

        self.send_response(404)
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"error": "Endpoint not found"}).encode("utf-8"))

    def log_message(self, format, *args):
        # Override to suppress default noisy access logs, keep custom logs clean
        sys.stderr.write(f"[CameraService HTTP] {args[0]} - {args[1]}\n")


def run_service(host="0.0.0.0", port=8008):
    server_address = (host, port)
    httpd = ThreadingHTTPServer(server_address, CameraServiceRequestHandler)
    print("=================================================================")
    print(f"  HIKVISION CCTV LOCAL CONTROL SERVICE RUNNING ON PORT {port}")
    print(f"  Available at: http://localhost:{port}/")
    print("  Endpoints:")
    print("    - GET  /status           : Health check")
    print("    - GET  /test_feed?source : Capture single frame snapshot & latency")
    print("    - POST /run_calibration  : Open desktop OpenCV ROI calibrator")
    print("=================================================================")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[CameraService] Shutting down server...")
    finally:
        httpd.server_close()
        print("[CameraService] Server stopped.")


if __name__ == "__main__":
    port = 8008
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    run_service(port=port)


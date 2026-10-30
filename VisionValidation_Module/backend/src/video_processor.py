"""
Video Processor Module for Smart Storage FYP
Handles camera ingestion (Webcam, USB camera, RTSP IP stream, or video file test fixture)
with a zero-latency background frame-draining thread for real-time RTSP CCTV streams.
Complies with FR-01 (Capture Macro-Level Video Feed) and Use-Case 1.
Author: Daren Tan Khai Shuen
"""

import os
import re
import threading
import time
from typing import Any, Optional, Tuple, Union
import cv2


class VideoProcessor:
    """
    Video stream manager for macro-surveillance input.
    Uses a background daemon thread for RTSP/live cameras to continuously drain
    FFmpeg's internal TCP ring buffer, guaranteeing zero-lag real-time frames.
    """

    def __init__(
        self,
        source: Union[int, str] = 0,
        width: int = 640,
        height: int = 480,
        auto_reconnect: bool = True,
        max_consecutive_drops: int = 5,
    ):
        self.source = source
        self.target_width = width
        self.target_height = height
        self.auto_reconnect = auto_reconnect
        self.max_consecutive_drops = max_consecutive_drops

        self.cap: Optional[cv2.VideoCapture] = None
        self.is_streaming = False
        self.consecutive_drops = 0
        self.is_rtsp = False

        # Threaded zero-latency frame buffer
        self._lock = threading.Lock()
        self._latest_frame: Optional[Any] = None
        self._latest_ret: bool = False
        self._frame_seq: int = 0
        self._last_read_seq: int = -1
        self._reader_running: bool = False
        self._reader_thread: Optional[threading.Thread] = None

        # FPS Tracking
        self.fps = 0.0
        self._frame_count = 0
        self._fps_start_time = time.time()
        self.last_frame_timestamp = 0.0

        self.connect()

    @staticmethod
    def _mask_url(url: Union[int, str]) -> str:
        """Mask credentials in RTSP URL for safe terminal logging."""
        if not isinstance(url, str):
            return str(url)
        return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:****@", url)

    def set_source(self, new_source: Union[int, str]) -> bool:
        """Dynamically switches video source and reconnects."""
        self.source = new_source
        return self.connect()

    def connect(self) -> bool:
        """Initializes the VideoCapture connection and starts the low-latency reader thread."""
        self._stop_reader_thread()

        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

        masked_source = self._mask_url(self.source)
        print(f"[VideoProcessor] Connecting to video source: {masked_source}...")

        is_webcam_index = False
        self.is_rtsp = False

        try:
            cam_idx = int(self.source)
            is_webcam_index = True
            self.cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(cam_idx)
        except (ValueError, TypeError):
            str_source = str(self.source)
            if str_source.lower().startswith("rtsp://"):
                self.is_rtsp = True
                # Low-latency FFmpeg RTSP flags: nobuffer + low_delay over TCP
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
                    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|max_delay;50000"
                )
            self.cap = cv2.VideoCapture(str_source, cv2.CAP_FFMPEG) if self.is_rtsp else cv2.VideoCapture(str_source)

        if self.cap is None or not self.cap.isOpened():
            print(f"[VideoProcessor] Failed to open video source: {masked_source}")
            self.is_streaming = False
            return False

        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass

        if is_webcam_index and self.target_width > 0 and self.target_height > 0:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.target_width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.target_height)

        # Read one initial frame to verify stream and seed buffer
        ret, first_frame = self.cap.read()
        if ret and first_frame is not None:
            if self.target_width > 0 and self.target_height > 0:
                h, w = first_frame.shape[:2]
                if w != self.target_width or h != self.target_height:
                    first_frame = cv2.resize(first_frame, (self.target_width, self.target_height), interpolation=cv2.INTER_LINEAR)
            with self._lock:
                self._latest_frame = first_frame
                self._latest_ret = True
                self._frame_seq = 1

        self.is_streaming = True
        self.consecutive_drops = 0
        self._fps_start_time = time.time()
        self._frame_count = 0

        # Start background frame-draining thread so RTSP never lags behind real-time
        self._reader_running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True, name="RTSPFrameDrainer")
        self._reader_thread.start()

        print(f"[VideoProcessor] Connected to video source ({masked_source}) [Zero-Latency Thread Active].")
        return True

    def _stop_reader_thread(self) -> None:
        self._reader_running = False
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None

    def _reader_loop(self) -> None:
        """Continuously grabs frames from VideoCapture so the OS/FFmpeg buffer never accumulates lag."""
        while self._reader_running and self.cap is not None:
            try:
                if not self.cap.isOpened():
                    time.sleep(0.05)
                    continue
                # grab() + retrieve() drains queued packets at maximum speed
                grabbed = self.cap.grab()
                if not grabbed:
                    with self._lock:
                        self._latest_ret = False
                    time.sleep(0.02)
                    continue

                ret, frame = self.cap.retrieve()
                if ret and frame is not None:
                    if self.target_width > 0 and self.target_height > 0:
                        h, w = frame.shape[:2]
                        if w != self.target_width or h != self.target_height:
                            frame = cv2.resize(frame, (self.target_width, self.target_height), interpolation=cv2.INTER_LINEAR)
                    with self._lock:
                        self._latest_frame = frame
                        self._latest_ret = True
                        self._frame_seq += 1
                else:
                    with self._lock:
                        self._latest_ret = False
            except Exception:
                time.sleep(0.02)

    def reconnect(self, max_retries: int = 5, retry_interval: float = 2.0) -> bool:
        """Attempts to reconnect to the video stream up to max_retries times."""
        masked = self._mask_url(self.source)
        print(f"[VideoProcessor] Attempting stream reconnection to {masked} (max {max_retries} retries)...")

        for attempt in range(1, max_retries + 1):
            print(f"[VideoProcessor] Reconnection attempt {attempt}/{max_retries}...")
            if self.connect():
                print(f"[VideoProcessor] Reconnected successfully on attempt {attempt}.")
                return True
            time.sleep(retry_interval)

        print(f"[VideoProcessor] Reconnection failed after {max_retries} attempts.")
        return False

    def capture_frame(self) -> Tuple[bool, Optional[Any]]:
        """
        Returns the latest real-time frame from the background reader thread.
        """
        if not self.is_streaming or self.cap is None:
            if self.auto_reconnect:
                if self.reconnect(max_retries=2, retry_interval=1.0):
                    return self.capture_frame()
            return False, None

        # Wait briefly (up to 40ms) if the consumer loop is faster than the camera's native FPS
        t_wait = time.perf_counter()
        while (time.perf_counter() - t_wait) < 0.04:
            with self._lock:
                if self._frame_seq != self._last_read_seq and self._latest_frame is not None:
                    break
            time.sleep(0.002)

        with self._lock:
            ret = self._latest_ret
            frame = self._latest_frame.copy() if (self._latest_frame is not None) else None
            self._last_read_seq = self._frame_seq

        now = time.time()
        self.last_frame_timestamp = now

        if not ret or frame is None:
            self.consecutive_drops += 1
            if self.consecutive_drops >= self.max_consecutive_drops:
                print("[VideoProcessor] Stream disconnected due to sustained frame drops.")
                self.is_streaming = False
                if self.auto_reconnect:
                    if self.reconnect(max_retries=3, retry_interval=1.0):
                        return self.capture_frame()
            return False, None

        self.consecutive_drops = 0

        self._frame_count += 1
        elapsed = now - self._fps_start_time
        if elapsed >= 1.0:
            self.fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_start_time = now

        return True, frame

    def release_stream(self) -> None:
        """Closes the background reader thread and video capture stream."""
        self._stop_reader_thread()
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self.is_streaming = False
        print("[VideoProcessor] Stream released.")

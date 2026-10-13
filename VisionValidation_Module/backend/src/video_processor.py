"""
Video Processor Module for Smart Storage FYP
Handles camera ingestion (Webcam, USB camera, RTSP IP stream, or video file test fixture).
Complies with FR-01 (Capture Macro-Level Video Feed) and Use-Case 1.
Author: Daren Tan Khai Shuen
"""

import os
import re
import time
from typing import Optional, Tuple, Union
import cv2


class VideoProcessor:
    """
    Video stream manager for macro-surveillance input.
    Provides frame decoding, FPS calculation, and reconnection fault tolerance
    for network RTSP CCTV cameras (Hikvision DS-2CD1023G0E-I) and local webcams.
    """

    def __init__(
        self,
        source: Union[int, str] = 0,
        width: int = 640,
        height: int = 480,
        auto_reconnect: bool = True,
        max_consecutive_drops: int = 5,
    ):
        """
        :param source: Camera index (int, e.g. 0, 1) or RTSP URL string (e.g. 'rtsp://...'), or video file path
        :param width: Target width (optional, 0 for native)
        :param height: Target height (optional, 0 for native)
        :param auto_reconnect: Whether to automatically attempt reconnection when frames drop
        :param max_consecutive_drops: Number of consecutive failed frame reads before declaring connection lost
        """
        self.source = source
        self.target_width = width
        self.target_height = height
        self.auto_reconnect = auto_reconnect
        self.max_consecutive_drops = max_consecutive_drops

        self.cap: Optional[cv2.VideoCapture] = None
        self.is_streaming = False
        self.consecutive_drops = 0
        self.is_rtsp = False

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
        """Initializes the VideoCapture connection with RTSP optimizations and drop handling."""
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
            # Fast DirectShow backend for USB webcams on Windows
            self.cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
        except (ValueError, TypeError):
            # Source is an RTSP stream URL or video file
            str_source = str(self.source)
            if str_source.lower().startswith("rtsp://"):
                self.is_rtsp = True
                # Force TCP transport for FFmpeg to eliminate packet loss and gray artifacts on Hikvision streams
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp"

            self.cap = cv2.VideoCapture(str_source)

        if self.cap is None or not self.cap.isOpened():
            print(f"[VideoProcessor] Failed to open video source: {masked_source}")
            self.is_streaming = False
            return False

        # Set low buffer size on RTSP to prevent frame latency accumulation in real-time inference
        if self.is_rtsp:
            try:
                self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass

        if is_webcam_index and self.target_width > 0 and self.target_height > 0:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.target_width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.target_height)

        self.is_streaming = True
        self.consecutive_drops = 0
        self._fps_start_time = time.time()
        self._frame_count = 0
        print(f"[VideoProcessor] Successfully connected to video source ({masked_source}).")
        return True

    def reconnect(self, max_retries: int = 5, retry_interval: float = 2.0) -> bool:
        """
        Attempts to reconnect to the video stream up to max_retries times.
        :param max_retries: Maximum reconnection attempts
        :param retry_interval: Sleep interval in seconds between attempts
        :return: True if reconnected successfully, False otherwise
        """
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

    def capture_frame(self) -> Tuple[bool, Optional[cv2.Mat]]:
        """
        Captures and returns the next frame from the stream buffer.
        Handles frame drop detection and optional auto-reconnection.
        :return: (ret, frame) tuple
        """
        if not self.is_streaming or self.cap is None:
            if self.auto_reconnect:
                if self.reconnect(max_retries=2, retry_interval=1.0):
                    return self.capture_frame()
            return False, None

        ret, frame = self.cap.read()
        now = time.time()
        self.last_frame_timestamp = now

        if not ret or frame is None:
            self.consecutive_drops += 1
            print(
                f"[VideoProcessor] Frame read failed (drop count: "
                f"{self.consecutive_drops}/{self.max_consecutive_drops})."
            )

            if self.consecutive_drops >= self.max_consecutive_drops:
                print("[VideoProcessor] Stream disconnected due to sustained frame drops.")
                self.is_streaming = False
                if self.auto_reconnect:
                    if self.reconnect(max_retries=3, retry_interval=1.5):
                        return self.capture_frame()

            return False, None

        # Reset drop counter on healthy frame
        self.consecutive_drops = 0

        # Update FPS rolling estimate
        self._frame_count += 1
        elapsed = now - self._fps_start_time
        if elapsed >= 1.0:
            self.fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_start_time = now

        return True, frame

    def release_stream(self) -> None:
        """Closes the video capture stream and cleans up resources."""
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self.is_streaming = False
        print("[VideoProcessor] Stream released.")

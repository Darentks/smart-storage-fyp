"""
Video Processor Module for Smart Storage FYP
Handles camera ingestion (Webcam, USB camera, RTSP IP stream, or video file test fixture).
Complies with FR-01 (Capture Macro-Level Video Feed) and Use-Case 1.
"""

import time
import cv2


class VideoProcessor:
    """
    Video stream manager for macro-surveillance input.
    Provides frame decoding, FPS calculation, and reconnection fault tolerance.
    """

    def __init__(self, source=0, width=640, height=480):
        """
        :param source: Camera index (int, e.g. 0, 1) or RTSP URL string (e.g. 'rtsp://...'), or video file path
        :param width: Target width (optional, 0 for native)
        :param height: Target height (optional, 0 for native)
        """
        self.source = source
        self.target_width = width
        self.target_height = height
        self.cap = None
        self.is_streaming = False

        # FPS Tracking
        self.fps = 0.0
        self._frame_count = 0
        self._fps_start_time = time.time()
        self.last_frame_timestamp = 0.0

        self.connect()

    def connect(self):
        """Initializes the VideoCapture connection."""
        if self.cap is not None:
            self.cap.release()

        print(f"[VideoProcessor] Connecting to video source: {self.source}...")

        # If source is integer or string representing integer, use CAP_DSHOW on Windows for fast webcam init
        is_webcam_index = False
        try:
            cam_idx = int(self.source)
            is_webcam_index = True
            self.cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
        except ValueError:
            # Source is an RTSP stream URL or video file
            self.cap = cv2.VideoCapture(self.source)

        if not self.cap.isOpened():
            print(f"[VideoProcessor] Failed to open video source: {self.source}")
            self.is_streaming = False
            return False

        if is_webcam_index and self.target_width > 0 and self.target_height > 0:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.target_width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.target_height)

        self.is_streaming = True
        self._fps_start_time = time.time()
        self._frame_count = 0
        print(f"[VideoProcessor] Successfully connected to video source.")
        return True

    def capture_frame(self):
        """
        Captures and returns the next frame from the stream buffer.
        :return: (ret, frame) tuple
        """
        if not self.is_streaming or self.cap is None:
            return False, None

        ret, frame = self.cap.read()
        now = time.time()
        self.last_frame_timestamp = now

        if not ret:
            print("[VideoProcessor] Frame read failed (end of stream or disconnected).")
            self.is_streaming = False
            return False, None

        # Update FPS rolling estimate
        self._frame_count += 1
        elapsed = now - self._fps_start_time
        if elapsed >= 1.0:
            self.fps = self._frame_count / elapsed
            self._frame_count = 0
            self._fps_start_time = now

        return True, frame

    def release_stream(self):
        """Closes the video capture stream."""
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        self.is_streaming = False
        print("[VideoProcessor] Stream released.")


"""The single live-tracking service the web app talks to.

One camera, one pipeline, started on first use and shared by every request.
It also keeps the rolling history the dashboard charts are drawn from.
"""
import logging
import os
import threading
import time
from collections import deque

from .config import load_settings

log = logging.getLogger(__name__)

HISTORY_POINTS = 12      # points per dashboard chart
HISTORY_EVERY_S = 10.0   # one point every 10s, so a chart covers 2 minutes


class VisionService:
    def __init__(self):
        self._lock = threading.Lock()
        self.settings = load_settings()
        self.pipeline = None
        self.source = None
        self.started_at = None
        self.error = None
        self.history = {key: deque(maxlen=HISTORY_POINTS)
                        for key in ("view", "movement", "peak", "confidence", "tracks", "activity")}
        self._next_sample = 0.0
        self._sampler = None

    # ---- lifecycle -----------------------------------------------------------
    @property
    def offline(self):
        """LIVE_TANK_OFFLINE=1 keeps the camera shut: used by tests and by
        hosts that cannot reach the tank."""
        return os.environ.get("LIVE_TANK_OFFLINE") == "1"

    def start(self):
        """Start the camera and pipeline once; safe to call from any request."""
        if self.pipeline is not None or not self.settings.configured or self.offline:
            return self.pipeline
        with self._lock:
            if self.pipeline is not None:
                return self.pipeline
            try:
                # Imported here so the site still runs where OpenCV is absent.
                os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
                os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
                from .capture import FrameSource
                from .pipeline import VisionPipeline

                self.source = FrameSource(self.settings.source_url, self.settings.source_label,
                                          self.settings.live)
                self.pipeline = VisionPipeline(self.source, self.settings)
                self.source.start()
                self.pipeline.start()
                self.started_at = time.time()
                self._sampler = threading.Thread(target=self._sample_history, daemon=True,
                                                 name="history")
                self._sampler.start()
                log.info("Live tracking started on %s", self.settings.source_label)
            except Exception as exc:                       # missing OpenCV, bad URL, no camera
                self.error = str(exc)
                log.exception("Could not start live tracking")
        return self.pipeline

    @property
    def available(self):
        """True once the pipeline is producing frames from the camera."""
        pipeline = self.start()
        return pipeline is not None and pipeline.frames > 0

    def state(self):
        pipeline = self.start()
        if pipeline is None:
            configured = self.settings.configured and not self.offline
            return {"configured": configured, "error": self.error,
                    "source": {"name": self.settings.source_label if configured else "No camera configured",
                               "status": "NO CAMERA"}}
        return {"configured": True, "error": self.error, **pipeline.state()}

    def frames(self, mask=False, fps=None):
        """Yield multipart MJPEG chunks of the annotated video (or the mask).

        Frames past `fps` are dropped rather than queued, so a viewer on a thin
        link falls behind in rate instead of in time.
        """
        pipeline = self.start()
        if pipeline is None:
            return
        if fps is None:
            fps = self.settings.stream_fps
        period = 1.0 / fps if fps > 0 else 0.0
        seq = 0
        next_due = 0.0
        while True:
            got = pipeline.wait_jpeg(seq, timeout=5.0, mask=mask)
            if got is None:
                continue
            seq, jpeg = got
            now = time.monotonic()
            if jpeg and now >= next_due:
                next_due = max(now, next_due) + period
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

    def snapshot(self):
        """Latest annotated frame as a single small JPEG, or None."""
        pipeline = self.start()
        return pipeline.snapshot() if pipeline is not None else None

    def set_layer(self, name, on):
        pipeline = self.start()
        if pipeline is not None:
            pipeline.set_layer(name, on)

    def relearn(self):
        pipeline = self.start()
        if pipeline is not None:
            pipeline.relearn()

    # ---- history for the dashboard charts -------------------------------------
    def _sample_history(self):
        while True:
            time.sleep(HISTORY_EVERY_S)
            try:
                self._record(self.pipeline.state())
            except Exception:
                log.exception("History sample failed")

    def _record(self, state):
        tracks = state["tracks"]
        speeds = [t["speed"] for t in tracks] or [0]
        confidences = [t["conf"] for t in tracks] or [0]
        moving = [s for s in speeds if s > 0]
        self.history["view"].append(state["counts"]["onscreen"])
        self.history["movement"].append(self.settings.to_speed(sum(speeds) / len(speeds)))
        self.history["peak"].append(self.settings.to_speed(max(speeds)))
        self.history["confidence"].append(round(100 * sum(confidences) / len(confidences)))
        self.history["tracks"].append(state["counts"]["known"])
        # Activity index: share of fish in view that are actually swimming.
        self.history["activity"].append(round(100 * len(moving) / len(tracks)) if tracks else 0)

    def series(self, key):
        """History padded to a full chart width, so the page always has 12 points."""
        points = list(self.history[key])
        if not points:
            return [0] * HISTORY_POINTS
        return [points[0]] * (HISTORY_POINTS - len(points)) + points


service = VisionService()

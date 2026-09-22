"""Frame -> detections -> DeepSORT tracks -> fish identities -> JPEG + HUD state."""
import logging
import threading
import time
from collections import deque

import cv2
import numpy as np

from .tracking import iou_matching, nn_matching
from .tracking.detection import Detection
from .tracking.tracker import Tracker

from .detector import FishDetector
from .identity import IdentityGallery
from .render import BLUE, DEFAULT_LAYERS, ICE, NAVY, draw_frame, erase_osd_text

log = logging.getLogger(__name__)

MAX_COSINE_DISTANCE = 0.4
NN_BUDGET = 60
N_INIT = 5                 # hits before a track is confirmed (~0.25 s at 20 fps)
TRAIL_LENGTH = 48
TRAIL_SMOOTHING = 0.3      # EMA weight of the newest trail point
MASK_PREVIEW_WIDTH = 384
SNAPSHOT_WIDTH = 960       # small annotated frame for remote viewers
SNAPSHOT_QUALITY = 70
SNAPSHOT_KEEP_S = 15.0     # keep encoding them for this long after the last request
MASK_KEEP_S = 5.0          # same for the mask preview, which few viewers open
STALE_AFTER_S = 3.0
BOOT_ID = str(int(time.time()))  # open pages reload when this changes (server restarted)


def resize_to_width(img, width):
    h, w = img.shape[:2]
    if w == width:
        return img.copy()
    return cv2.resize(img, (width, round(h * width / w)),
                      interpolation=cv2.INTER_AREA if width < w else cv2.INTER_LINEAR)


class VisionPipeline(threading.Thread):
    def __init__(self, source, settings):
        super().__init__(daemon=True, name="vision")
        self.source = source
        self.settings = settings
        self.detector = FishDetector(width=settings.detect_width,
                                     ignore_rects=settings.ignore_rects)
        metric = nn_matching.NearestNeighborDistanceMetric("cosine", MAX_COSINE_DISTANCE, NN_BUDGET)
        self.tracker = Tracker(metric, max_iou_distance=0.7, max_age=settings.max_age, n_init=N_INIT)
        self.identities = IdentityGallery(match_threshold=settings.reid_threshold)
        self.layers = dict(DEFAULT_LAYERS)

        self.fps = 0.0
        self.latency = 0.0
        self.frames = 0
        self.started = time.time()
        self.history = deque(maxlen=120)  # fish on screen, sampled at 1 Hz

        self._trails = {}
        self._track_conf = {}
        self._track_feature = {}
        self._track_shape = {}
        self._track_origin = {}
        self._relearn = threading.Event()
        self._cond = threading.Condition()
        self._jpeg = None
        self._mask_jpeg = None
        self._snapshot_bytes = None
        self._snapshot_until = 0.0
        self._mask_until = 0.0
        self._seq = 0
        self._snapshot = {"counts": {"onscreen": 0, "detections": 0, "candidates": 0,
                                     "reflections": 0, "edges": 0, "dark": 0, "reseeds": 0,
                                     "known": 0, "missing": 0, "reids": 0, "tracks_created": 0},
                          "tracks": [], "missing": []}

    # ---- control surface used by the web server -------------------------
    def relearn(self):
        self._relearn.set()

    def set_layer(self, name, on):
        if name in self.layers:
            self.layers[name] = bool(on)

    def wait_jpeg(self, after_seq, timeout, mask=False):
        if mask:  # asking for it is what keeps it being encoded
            self._mask_until = time.time() + MASK_KEEP_S
        with self._cond:
            if not self._cond.wait_for(lambda: self._seq > after_seq, timeout):
                return None
            return self._seq, (self._mask_jpeg if mask else self._jpeg)

    def state(self):
        src = self.source
        now = time.time()
        status = src.status
        if status in ("LIVE", "PLAYBACK") and src.last_frame_at and now - src.last_frame_at > STALE_AFTER_S:
            status = "STALLED"
        return {
            "boot": BOOT_ID,
            "label": self.settings.label,
            "source": {"name": src.label, "status": status, "live": src.live,
                       "width": src.width, "height": src.height, "fps": round(src.fps, 1)},
            "vision": {"phase": "LEARNING" if self.detector.learning else "TRACKING",
                       "progress": round(self.detector.progress, 3),
                       "fps": round(self.fps, 1), "latency_ms": round(self.latency * 1000),
                       "frame": self.frames},
            "counts": self._snapshot["counts"],
            "tracks": self._snapshot["tracks"],
            "missing": self._snapshot["missing"],
            "history": list(self.history),
            "layers": dict(self.layers),
            "uptime_s": int(now - self.started),
        }

    # ---- processing loop ----------------------------------------------------
    def run(self):
        last_seq = 0
        last_tick = None
        next_history = time.time()
        while True:
            got = self.source.wait_frame(last_seq, timeout=1.0)
            if got is None:
                continue
            last_seq, frame, captured_at = got
            try:
                self._process(frame, captured_at)
            except Exception:
                log.exception("Frame processing failed")
                continue

            now = time.time()
            if last_tick is not None and now > last_tick:
                inst = 1.0 / (now - last_tick)
                self.fps = inst if self.fps == 0 else self.fps * 0.9 + inst * 0.1
            last_tick = now
            if now >= next_history:
                self.history.append(self._snapshot["counts"]["onscreen"])
                next_history = now + 1.0

    def _process(self, frame, captured_at):
        if self._relearn.is_set():
            self._relearn.clear()
            self.detector.reset()

        display = resize_to_width(frame, min(self.settings.output_width, frame.shape[1]))
        if self.settings.osd_clock_rect:
            erase_osd_text(display, self.settings.osd_clock_rect)
        small = resize_to_width(display, self.settings.detect_width)
        k = display.shape[1] / small.shape[1]

        # Predict first so the detector can split a blob between the fish
        # expected inside it this frame.
        self.tracker.predict()
        tracked = [self._fitted(t)[0] / k for t in self.tracker.tracks
                   if t.is_confirmed() and t.time_since_update <= 2]
        blobs = self.detector.detect(small, tracked)
        rejected = self.detector.rejected
        small_mask = self.detector.mask
        self.debug = (small, blobs, rejected)
        detections = []
        for b in blobs:
            det = Detection((b.x * k, b.y * k, b.w * k, b.h * k), b.confidence, self.settings.label, b.feature)
            det.corners = b.corners * k
            detections.append(det)
        # No non-max suppression: blobs never share pixels, and the upright
        # boxes of two crossing fish overlap heavily, so NMS would drop one.
        self.tracker.update(detections)

        tracks, candidates = self._describe_tracks(detections, display.shape, frame.shape[1] / display.shape[1])
        self.frames += 1

        display_mask = None
        if self.layers["mask"]:
            display_mask = cv2.resize(small_mask, (display.shape[1], display.shape[0]),
                                      interpolation=cv2.INTER_NEAREST)
        draw_frame(display, tracks + candidates,
                   [b.corners * k for b in blobs],
                   [(b.corners * k, reason) for b, reason in rejected],
                   display_mask, self.layers, self.settings.label)

        ok, jpeg = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, self.settings.jpeg_quality])
        mask_jpeg = (self._mask_preview(small_mask, blobs, [b for b, _reason in rejected])
                     if time.time() <= self._mask_until else None)
        snapshot = self._snapshot_jpeg(display)
        self.latency = time.time() - captured_at
        if not ok:
            return

        now = time.time()
        missing = sorted(self.identities.missing(now), key=lambda i: i.lost_at, reverse=True)
        self._snapshot = {
            "counts": {"onscreen": sum(1 for t in tracks if t["matched"]),
                       "detections": len(detections),
                       "candidates": len(candidates),
                       "reflections": sum(1 for _b, reason in rejected if reason == "REFLECTION"),
                       "edges": sum(1 for _b, reason in rejected if reason == "TANK EDGE"),
                       "dark": sum(1 for _b, reason in rejected if reason == "DARK OBJECT"),
                       "reseeds": self.detector.reseeds,
                       "known": len(self.identities.identities),
                       "missing": len(missing),
                       "reids": self.identities.reids,
                       "tracks_created": self.tracker._next_id - 1},
            "tracks": [{key: t[key] for key in ("id", "track_id", "conf", "speed", "age_s",
                                                "size", "matched", "pos", "heading")}
                       for t in sorted(tracks, key=lambda t: t["id"])],
            "missing": [{"id": i.number, "gone_s": round(now - i.lost_at, 1),
                         "seen_s": round(i.last_seen - i.first_seen, 1)} for i in missing[:12]],
        }
        with self._cond:
            self._jpeg = jpeg.tobytes()
            self._mask_jpeg = mask_jpeg
            if snapshot is not None:
                self._snapshot_bytes = snapshot
            self._seq += 1
            self._cond.notify_all()

    def _describe_tracks(self, detections, display_shape, to_source_px):
        """Returns (identified fish, unconfirmed candidate tracks) as render dicts."""
        now = time.time()
        det_boxes = np.array([d.tlwh for d in detections]) if detections else None
        live_ids = {t.track_id for t in self.tracker.tracks}

        for track in self.tracker.tracks:
            self._track_origin.setdefault(track.track_id, _center(track))
            if det_boxes is None or track.time_since_update != 0:
                continue
            ious = iou_matching.iou(track.to_tlwh(), det_boxes)
            best = int(np.argmax(ious))
            if ious[best] > 0.3:
                det = detections[best]
                prev = self._track_conf.get(track.track_id, det.confidence)
                self._track_conf[track.track_id] = prev * 0.7 + det.confidence * 0.3
                self._track_feature[track.track_id] = det.feature
                self._track_shape[track.track_id] = (det.corners, det.corners.mean(axis=0))

        confirmed = [t for t in self.tracker.tracks if t.is_confirmed()]
        frame_diag = float(np.hypot(display_shape[1], display_shape[0]))
        binding = self.identities.update(
            [{"track_id": t.track_id, "center": _center(t), "origin": self._track_origin[t.track_id],
              "box": tuple(t.to_tlbr()),
              "area": float(t.to_tlwh()[2:].prod()),
              "matched": t.time_since_update == 0, "hits": t.hits,
              "feature": self._track_feature.get(t.track_id)} for t in confirmed],
            frame_diag, now)

        fish, candidates = [], []
        for track in self.tracker.tracks:
            if track.time_since_update > 1:
                continue
            identity = binding.get(track.track_id)
            if identity is None:
                # Not yet an identified fish: still being acquired or re-identified.
                # Single-frame flickers are not worth drawing.
                if track.hits >= 3:
                    candidates.append(self._render_dict(track, None, now, to_source_px, display_shape))
                continue
            trail = self._trails.setdefault(identity, {"track_id": track.track_id, "points": deque(maxlen=TRAIL_LENGTH)})
            if trail["track_id"] != track.track_id:  # re-bound to a new track: start a fresh trail
                trail["track_id"] = track.track_id
                trail["points"].clear()
            # Kalman centre: steadier than the fitted box, whose centre jumps
            # when a bending fish changes shape.
            cx, cy = _center(track)
            if trail["points"]:
                px, py = trail["points"][-1]
                cx = px + (cx - px) * TRAIL_SMOOTHING
                cy = py + (cy - py) * TRAIL_SMOOTHING
            trail["points"].append((int(cx), int(cy)))
            fish.append(self._render_dict(track, identity, now, to_source_px, display_shape))

        for store in (self._track_conf, self._track_feature, self._track_shape, self._track_origin):
            for tid in [tid for tid in store if tid not in live_ids]:
                del store[tid]
        bound_identities = set(binding.values())
        for identity in [i for i in self._trails if i not in bound_identities]:
            del self._trails[identity]
        return fish, candidates

    def _fitted(self, track):
        """(corners, center) of the rotated box hugging the fish.

        Uses the fish's last matched detection; while DeepSORT coasts, that
        shape is moved along with the Kalman prediction.
        """
        shape = self._track_shape.get(track.track_id)
        kalman_center = np.array(_center(track), np.float32)
        if shape is None:
            x1, y1, x2, y2 = track.to_tlbr()
            corners = np.array([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], np.float32)
            return corners, kalman_center
        corners, center = shape
        if track.time_since_update == 0:
            return corners, center
        return corners + (kalman_center - center), kalman_center

    def _render_dict(self, track, identity, now, to_source_px, display_shape):
        corners, center = self._fitted(track)
        vx, vy = float(track.mean[4]), float(track.mean[5])
        # Heading in compass degrees: 0 is up the frame, 90 is to the right.
        heading = int(np.degrees(np.arctan2(vx, -vy))) % 360
        trail = self._trails.get(identity, {}).get("points", ()) if identity is not None else ()
        sides = sorted((float(np.linalg.norm(corners[1] - corners[0])), float(np.linalg.norm(corners[2] - corners[1]))),
                       reverse=True)
        return {
            "id": identity,
            "track_id": track.track_id,
            "confirmed": identity is not None,
            # Before an ID: DeepSORT still confirming the track, then waiting for an identity.
            "stage": None if identity is not None else ("ASSIGNING ID..." if track.is_confirmed() else "DETECTING..."),
            "matched": track.time_since_update == 0,
            "corners": corners,
            "center": (int(center[0]), int(center[1])),
            "pos": (round(float(center[0]) / display_shape[1], 3), round(float(center[1]) / display_shape[0], 3)),
            "heading": heading,
            "velocity": (vx, vy),
            "trail": list(trail),
            "conf": round(self._track_conf.get(track.track_id, 0.0), 3),
            "speed": round(np.hypot(vx, vy) * max(self.fps, 1.0) * to_source_px),
            "age_s": round(now - self.identities.first_seen(identity, now), 1) if identity is not None else 0,
            "size": [round(sides[0] * to_source_px), round(sides[1] * to_source_px)],
        }

    def snapshot(self):
        """Latest small annotated frame, for viewers that cannot take the stream.

        Only encoded while someone keeps asking, so the local stream pays
        nothing for it when nobody is watching remotely.
        """
        self._snapshot_until = time.time() + SNAPSHOT_KEEP_S
        with self._cond:
            if self._snapshot_bytes is None:
                self._cond.wait(timeout=2.0)
            return self._snapshot_bytes

    def _snapshot_jpeg(self, display):
        if time.time() > self._snapshot_until:
            return None
        small = resize_to_width(display, min(SNAPSHOT_WIDTH, display.shape[1]))
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, SNAPSHOT_QUALITY])
        return buf.tobytes() if ok else None

    def _mask_preview(self, mask, blobs, reflections):
        h, w = mask.shape[:2]
        pw = MASK_PREVIEW_WIDTH
        ph = round(h * pw / w)
        preview = np.empty((ph, pw, 3), np.uint8)
        preview[:] = NAVY
        preview[cv2.resize(mask, (pw, ph), interpolation=cv2.INTER_NEAREST) > 0] = ICE
        s = pw / w
        for b in reflections:
            cv2.polylines(preview, [np.int32(b.corners * s)], True, (150, 110, 90), 1, cv2.LINE_AA)
        for b in blobs:
            cv2.polylines(preview, [np.int32(b.corners * s)], True, BLUE, 2, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None


def _center(track):
    x1, y1, x2, y2 = track.to_tlbr()
    return int((x1 + x2) / 2), int((y1 + y2) / 2)

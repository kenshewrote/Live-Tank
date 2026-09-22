"""Runtime settings, read from .env and the environment.

Copy .env.example to .env and fill in the camera details. With no camera
configured the app still runs: the site serves placeholder dashboard data and
the live panel reports that no camera is set up.
"""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent

# Normalised (x0, y0, x1, y1) regions the detector ignores: the camera's OSD
# clock (top left, changes every second) and the channel name.
DEFAULT_IGNORE_RECTS = "0,0,0.23,0.11;0.715,0.845,0.82,0.905"
# Where the OSD date/time sits; its text is erased from the displayed video.
DEFAULT_OSD_CLOCK_RECT = "0.008,0.03,0.225,0.105"


@dataclass(frozen=True)
class Settings:
    source_url: str | None       # None when no camera is configured
    source_label: str
    live: bool
    label: str
    output_width: int
    detect_width: int
    jpeg_quality: int
    stream_fps: float
    max_age: int
    reid_threshold: float
    px_per_cm: float             # 0 keeps speeds in pixels per second
    ignore_rects: tuple
    osd_clock_rect: tuple | None

    @property
    def configured(self):
        return self.source_url is not None

    @property
    def speed_unit(self):
        return "cm/s" if self.px_per_cm > 0 else "px/s"

    def to_speed(self, px_per_s):
        return round(px_per_s / self.px_per_cm, 1) if self.px_per_cm > 0 else round(px_per_s)


def load_dotenv(path=ROOT / ".env"):
    """Minimal .env reader; real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def parse_rects(spec):
    return tuple(tuple(float(v) for v in part.split(","))
                 for part in spec.split(";") if part.strip())


def load_settings():
    load_dotenv()
    env = os.environ.get

    source, channel = env("SOURCE"), env("CAM_CHANNEL", "101")
    if source:
        live = source.startswith(("rtsp://", "http://", "https://"))
        path = Path(source)
        if not live and not path.is_absolute():
            path = ROOT / path
        url, label = (source, "Custom stream") if live else (str(path), path.name)
    elif env("CAM_HOST") and env("CAM_PASS"):
        user, password = quote(env("CAM_USER", "admin"), safe=""), quote(env("CAM_PASS"), safe="")
        host = env("CAM_HOST")
        url = f"rtsp://{user}:{password}@{host}:{env('CAM_RTSP_PORT', '554')}/Streaming/Channels/{channel}"
        label, live = f"{host} · CH{channel}", True
    else:
        url, label, live = None, "No camera configured", False

    return Settings(
        source_url=url,
        source_label=label,
        live=live,
        label=env("FISH_LABEL", "LMB"),
        # MJPEG re-sends a whole frame every frame, so width, quality and rate
        # multiply straight into the bandwidth a remote viewer needs. These
        # defaults measure around 3-4 Mbit/s, which a home upload can carry;
        # raise them when everyone watching is on the tank's own network.
        output_width=int(env("OUTPUT_WIDTH", "1280")),
        detect_width=int(env("DETECT_WIDTH", "640")),
        jpeg_quality=int(env("JPEG_QUALITY", "60")),
        stream_fps=float(env("STREAM_FPS", "12")),
        max_age=int(env("TRACK_MAX_AGE", "40")),
        reid_threshold=float(env("REID_THRESHOLD", "0.15")),
        px_per_cm=float(env("PX_PER_CM", "0")),
        ignore_rects=parse_rects(env("IGNORE_RECTS", DEFAULT_IGNORE_RECTS)),
        # Set HIDE_OSD_CLOCK=0 to keep the camera's date/time in the video.
        osd_clock_rect=(parse_rects(env("OSD_CLOCK_RECT", DEFAULT_OSD_CLOCK_RECT))[0]
                        if env("HIDE_OSD_CLOCK", "1") != "0" else None),
    )

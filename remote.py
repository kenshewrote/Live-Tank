"""Talking to a tracker that runs somewhere else.

The site can be deployed where the tank is unreachable (Vercel, Render). Set
TRACKER_ORIGIN to the public URL of the machine running the tracker and the
site fetches video snapshots and data from it, server side, adding
TRACKER_TOKEN. The token stays in the host's environment and never reaches
the browser.
"""
import json
import logging
import os
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

TIMEOUT_S = 10
ORIGIN_FILE = Path(__file__).resolve().parent / "tracker_origin.txt"


def origin():
    """Where the tracker is published.

    TRACKER_ORIGIN wins. Otherwise tracker_origin.txt, which is committed so a
    deployment can follow the tracker without anyone editing host settings;
    an empty file means there is no remote tracker.
    """
    from_env = os.environ.get("TRACKER_ORIGIN", "").strip()
    if from_env:
        return from_env.rstrip("/")
    try:
        for line in ORIGIN_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line.rstrip("/")
    except OSError:
        pass
    return ""


def token():
    return os.environ.get("TRACKER_TOKEN", "")


@lru_cache(maxsize=1)
def _own_camera():
    """True when this machine watches the tank itself, so it is the tracker.

    Without this a committed tracker_origin.txt would make the tracker proxy
    to itself. Cached: it only depends on the environment at startup.
    """
    if os.environ.get("LIVE_TANK_OFFLINE") == "1":
        return False
    try:
        from live_tank.config import load_settings

        return load_settings().configured
    except Exception:
        return False


def enabled():
    """True when this instance should fetch from a tracker elsewhere."""
    return bool(origin()) and not _own_camera()


def fetch(path, timeout=TIMEOUT_S):
    """(body, content_type) from the remote tracker, or (None, None)."""
    url = f"{origin()}{path}"
    request = urllib.request.Request(url, headers={"X-Tank-Token": token()} if token() else {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Type", "application/octet-stream")
    except urllib.error.HTTPError as exc:
        log.warning("Tracker %s returned %s", path, exc.code)
    except Exception as exc:
        log.warning("Tracker %s unreachable: %s", path, exc)
    return None, None


def fetch_json(path, timeout=TIMEOUT_S):
    body, _type = fetch(path, timeout)
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        log.warning("Tracker %s sent something that is not JSON", path)
        return None

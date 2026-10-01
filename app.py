"""Live Tank: the tracking dashboard and the live camera, in one Flask app.

The same app runs in two places:

* On the machine that can see the tank, where it runs the tracker itself.
  Set LIVE_TANK_TOKEN there and every /api call must carry it.
* On a public host (Vercel, Render) that cannot reach the tank. Set
  TRACKER_ORIGIN and TRACKER_TOKEN and it fetches video snapshots and data
  from the first one, server side, so the token never reaches the browser.

Routes
  /                     the dashboard site (live_tracking.html)
  /live                 full-screen camera view with the tracking overlay
  /api/config           where the page should get video, and its ticket
  /api/stream-ticket    a fresh ticket for reopening the stream
  /api/video.mjpg       annotated camera stream (tracker machine only)
  /api/mask.mjpg        detector's foreground mask (tracker machine only)
  /api/snapshot.jpg     latest annotated frame, one JPEG (fallback)
  /api/vision           tracker state for the live panel
  /api/layers           POST: turn overlay layers on and off
  /api/relearn          POST: relearn the empty tank background
  /api/dashboard        whole dashboard payload (live, else placeholder)
  /api/stats            KPI cards
  /api/metrics[/<id>]   chart series per KPI
  /api/tracks           per-fish rows
  /api/activity         movement vs activity comparison
"""
import logging
import os

from flask import Flask, Response, abort, jsonify, render_template, request, send_from_directory
from flask_cors import CORS

import remote
import tickets
import tracking
from live_tank.service import service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__, template_folder="live_tank/web/templates",
            static_folder="live_tank/web/static", static_url_path="/static")
CORS(app)

# Warm the camera up at boot so the first visitor sees live numbers, not the
# sample data. No-op when no camera is configured or LIVE_TANK_OFFLINE=1.
service.start()

# Cache policies for the proxying instance only. Every request there is a
# function invocation that also waits on the tunnel, so letting the CDN serve
# one upstream fetch to many viewers is the difference between per-viewer cost
# and per-tank cost. Kept well under the polling intervals so figures stay
# current. On the tracker itself the data is local and free, and caching it
# would only serve stale numbers to the lab.
VISION_CACHE = "public, s-maxage=2, stale-while-revalidate=5"
DASHBOARD_CACHE = "public, s-maxage=10, stale-while-revalidate=20"
# A tracker that is merely unreachable may be back in a moment; don't pin that.
UNREACHABLE_CACHE = "public, s-maxage=2, stale-while-revalidate=2"


def shared(payload, policy):
    """Let the CDN hand one proxied response to several viewers."""
    response = jsonify(payload)
    if remote.enabled():
        response.headers["Cache-Control"] = policy
    return response


def uncached(payload):
    """A response no cache may keep: it carries a ticket with its own expiry."""
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def local_token():
    """Token this instance demands from outside callers (tracker machine)."""
    return os.environ.get("LIVE_TANK_TOKEN", "")


def from_outside(req):
    """True when the request came through a tunnel or proxy rather than the LAN.

    An ngrok or Cloudflare tunnel connects to this app from localhost, so the
    client address says nothing; the forwarding headers it adds do.
    """
    return bool(req.headers.get("X-Forwarded-For")
                or req.headers.get("CF-Connecting-IP")
                or req.headers.get("X-Real-IP"))


@app.before_request
def guard_outside_callers():
    """With LIVE_TANK_TOKEN set, this machine is published through a tunnel.

    Requests arriving that way must carry the token, and they only get the
    API: the site itself stays for viewers on the tank's own network. An
    instance that proxies a remote tracker never guards, because its visitors
    are the public.
    """
    if remote.enabled() or not local_token():
        return None
    if not from_outside(request):
        return None                      # a browser on the lab network
    if not request.path.startswith("/api/"):
        abort(404)                       # the tunnel publishes data, not the site
    given = request.headers.get("X-Tank-Token") or request.args.get("t", "")
    if given == local_token():
        return None
    # A browser sent here by the public site carries a ticket instead: good for
    # the video for a few minutes, never for the control endpoints.
    if request.path in tickets.VIDEO_PATHS and tickets.verify(local_token(), given):
        return None
    abort(401)


# ---- the site ---------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "live_tracking.html")


@app.route("/live")
def live_view():
    return render_template("live.html", label=service.settings.label)


# ---- live camera ------------------------------------------------------------
MAX_STREAM_FPS = 60.0


def requested_fps():
    """Frame rate for this viewer; ?fps= lets one on the LAN ask for more."""
    try:
        return min(max(float(request.args["fps"]), 0.0), MAX_STREAM_FPS)
    except (KeyError, ValueError):
        return None                         # the configured default


def _stream(mask):
    if remote.enabled():                    # a serverless host cannot relay a stream
        abort(404)
    return Response(service.frames(mask=mask, fps=requested_fps()),
                    mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


def stream_plan():
    """Where the browser should get its video.

    On the tracker itself the stream is simply local. On the public site it is
    on the tracker's own URL: relaying it here would cost a round trip per
    frame, so the page is sent straight there with a ticket instead.
    """
    if not remote.enabled():
        return {"stream": "mjpeg", "origin": "", "ticket": ""}
    return {"stream": "direct", "origin": remote.origin(), "ticket": tickets.mint(remote.token())}


@app.route("/api/config")
def api_config():
    """Tells the page where to open the stream, and how to authenticate to it."""
    return uncached({**stream_plan(), "remote": remote.enabled(),
                     "label": service.settings.label})


@app.route("/api/stream-ticket")
def api_stream_ticket():
    """A fresh ticket. The page asks for one whenever it reopens the stream."""
    return uncached(stream_plan())


@app.route("/api/video.mjpg")
def api_video():
    return _stream(mask=False)


@app.route("/api/mask.mjpg")
def api_mask():
    return _stream(mask=True)


@app.route("/api/snapshot.jpg")
def api_snapshot():
    """One annotated frame. Used by viewers that cannot take the MJPEG stream."""
    if remote.enabled():
        body, content_type = remote.fetch("/api/snapshot.jpg")
        if body is None:
            abort(502)
        return Response(body, mimetype=content_type, headers={"Cache-Control": "no-store"})
    frame = service.snapshot()
    if frame is None:
        abort(503)
    return Response(frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.route("/api/vision")
def api_vision():
    if remote.enabled():
        state = remote.fetch_json("/api/vision")
        if state is None:
            return shared({"configured": False, "error": "tracker unreachable",
                           "source": {"name": "Tracker offline", "status": "NO CAMERA"}},
                          UNREACHABLE_CACHE)
        return shared(state, VISION_CACHE)
    return jsonify(service.state())


@app.route("/api/layers", methods=["POST"])
def api_layers():
    for name, on in (request.get_json(force=True, silent=True) or {}).items():
        service.set_layer(name, on)
    return jsonify(service.state().get("layers", {}))


@app.route("/api/relearn", methods=["POST"])
def api_relearn():
    service.relearn()
    return jsonify({"ok": True})


# ---- dashboard data ---------------------------------------------------------
@app.route("/api/dashboard")
def api_dashboard():
    """Full page payload. The frontend loads this once on boot."""
    return shared(tracking.get_snapshot(), DASHBOARD_CACHE)


@app.route("/api/stats")
def api_stats():
    return shared(tracking.get_stats(), DASHBOARD_CACHE)


@app.route("/api/metrics")
def api_metrics():
    return shared(tracking.get_metrics(), DASHBOARD_CACHE)


@app.route("/api/metrics/<metric_id>")
def api_metric(metric_id):
    metric = tracking.get_metric(metric_id)
    if metric is None:
        return jsonify({"error": "unknown metric", "id": metric_id}), 404
    return shared({"id": metric_id, **metric}, DASHBOARD_CACHE)


@app.route("/api/tracks")
def api_tracks():
    return shared(tracking.get_tracks(), DASHBOARD_CACHE)


@app.route("/api/activity")
def api_activity():
    return shared(tracking.get_activity(), DASHBOARD_CACHE)


if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 5000)),
        threaded=True,
        use_reloader=False,
    )

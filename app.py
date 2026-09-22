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
  /api/config           how this instance serves video: stream or snapshots
  /api/video.mjpg       annotated camera stream (tracker machine only)
  /api/mask.mjpg        detector's foreground mask (tracker machine only)
  /api/snapshot.jpg     latest annotated frame, one JPEG
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
import tracking
from live_tank.service import service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__, template_folder="live_tank/web/templates",
            static_folder="live_tank/web/static", static_url_path="/static")
CORS(app)

# Warm the camera up at boot so the first visitor sees live numbers, not the
# sample data. No-op when no camera is configured or LIVE_TANK_OFFLINE=1.
service.start()


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
    if given != local_token():
        abort(401)
    return None


# ---- the site ---------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(".", "live_tracking.html")


@app.route("/live")
def live_view():
    return render_template("live.html", label=service.settings.label)


# ---- live camera ------------------------------------------------------------
def _stream(mask):
    if remote.enabled():                    # a serverless host cannot relay a stream
        abort(404)
    return Response(service.frames(mask=mask),
                    mimetype="multipart/x-mixed-replace; boundary=frame",
                    headers={"Cache-Control": "no-store"})


@app.route("/api/config")
def api_config():
    """Tells the page whether to open the stream or poll snapshots."""
    return jsonify({"stream": "snapshot" if remote.enabled() else "mjpeg",
                    "remote": remote.enabled(),
                    "label": service.settings.label})


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
        return jsonify(state or {"configured": False, "error": "tracker unreachable",
                                 "source": {"name": "Tracker offline", "status": "NO CAMERA"}})
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
    return jsonify(tracking.get_snapshot())


@app.route("/api/stats")
def api_stats():
    return jsonify(tracking.get_stats())


@app.route("/api/metrics")
def api_metrics():
    return jsonify(tracking.get_metrics())


@app.route("/api/metrics/<metric_id>")
def api_metric(metric_id):
    metric = tracking.get_metric(metric_id)
    if metric is None:
        return jsonify({"error": "unknown metric", "id": metric_id}), 404
    return jsonify({"id": metric_id, **metric})


@app.route("/api/tracks")
def api_tracks():
    return jsonify(tracking.get_tracks())


@app.route("/api/activity")
def api_activity():
    return jsonify(tracking.get_activity())


if __name__ == "__main__":
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "0") == "1",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", 5000)),
        threaded=True,
        use_reloader=False,
    )

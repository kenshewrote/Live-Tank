# Live Tank

Live largemouth bass tracking for an aquaculture tank. One Flask app serves a
tracking dashboard, embeds the tank camera with detection overlays drawn on it,
and exposes the tracker's numbers as JSON.

- **Dashboard** (`/`) — live camera panel, KPI cards, charts and a per-fish log.
- **Full-screen view** (`/live`) — the camera filling the screen with boxes,
  swim trails, motion vectors and the detector's foreground mask.
- **Fish identities** — each fish keeps a number (LMB 01, LMB 02 …) and gets it
  back when it leaves the frame and returns.

With no camera configured the site still runs and serves sample dashboard data,
so it can be deployed anywhere.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-tracker.txt

cp .env.example .env          # add the camera host, user and password
.venv/bin/python app.py       # http://127.0.0.1:5000
```

Without `requirements-tracker.txt` the site still runs; it just serves the
sample dashboard instead of opening a camera.

Run the tests with `.venv/bin/python -m unittest discover tests`.

## How the tracking works

There is no trained fish detector in this repository, so fish are found by
comparing each frame against a learned picture of the empty tank. The stages,
all in `live_tank/`:

1. **capture** — reads the camera on its own thread over RTSP, keeps only the
   newest frame, and reconnects on its own.
2. **detector** — learns the empty tank (a median over the first seconds), then
   treats pixels that are darker, more saturated or more olive than the water as
   fish. It fits a rotated box to each body and splits a blob that covers two
   fish. It rejects reflections in the glass, the tank's frame bars when the
   camera sways, dark fittings such as suction cups, and "ghosts" left where a
   fish used to rest.
3. **tracking** — DeepSORT (Kalman filter + Hungarian matching) follows each
   fish frame to frame. See `THIRD_PARTY_NOTICES.md`.
4. **identity** — turns short tracks into a stable population. A returning fish
   is matched to a missing identity mainly on where it was last seen; a new
   number is only issued when more separate fish are visible than there are
   identities, for several seconds together.
5. **render** — draws boxes, labels, trails and vectors into the frame, and
   erases the camera's clock overlay.
6. **service / dashboard** — one shared pipeline per process, a rolling history
   for the charts, and the mapping into the dashboard payload.

## Repository map

```
app.py                  Flask routes: site, live video, dashboard APIs
remote.py               fetching video and data from a tracker on another machine
tracker_origin.txt      that tracker's public URL, for hosts without env settings
tracking.py             dashboard payload: live when a camera answers, else sample data
live_tracking.html      the dashboard page (markup, styles and script in one file)
live_tank/
  config.py             settings from .env and the environment
  capture.py            threaded RTSP/file frame source
  detector.py           background-model fish detector and its filters
  identity.py           persistent fish identities and re-identification
  pipeline.py           frame -> detections -> tracks -> annotated JPEG + state
  render.py             overlay drawing
  service.py            the running tracker shared by the web app
  dashboard.py          tracker state -> dashboard payload
  tracking/             vendored DeepSORT core
  web/                  full-screen view (template, css, js)
tests/test_app.py       route, payload and mapping tests
```

## Configuration

Everything is environment driven; `.env` is read at startup and is gitignored.
See `.env.example` for the full list. The common ones:

| Variable | Meaning |
| --- | --- |
| `CAM_HOST`, `CAM_USER`, `CAM_PASS` | camera address and login |
| `CAM_CHANNEL` | `101` main stream, `103` 720p, `102` 640×360 |
| `SOURCE` | use a video file or another URL instead of the camera |
| `FISH_LABEL` | label drawn on each fish (default `LMB`) |
| `PX_PER_CM` | set once the tank is calibrated to report cm/s instead of px/s |
| `LIVE_TANK_OFFLINE` | `1` never opens the camera (used by the tests) |
| `LIVE_TANK_TOKEN` | on the tracker: token every `/api` call must carry |
| `TRACKER_ORIGIN`, `TRACKER_TOKEN` | on the public site: where the tracker is, and its token |

## HTTP API

| Route | Returns |
| --- | --- |
| `GET /api/config` | whether this instance streams video or serves snapshots |
| `GET /api/video.mjpg` | annotated camera stream (MJPEG), tracker machine only |
| `GET /api/snapshot.jpg` | latest annotated frame, one JPEG |
| `GET /api/mask.mjpg` | detector foreground mask (MJPEG) |
| `GET /api/vision` | tracker state: source, counts, per-fish rows, layers |
| `POST /api/layers` | turn overlay layers on and off, e.g. `{"trails": false}` |
| `POST /api/relearn` | relearn the empty tank background |
| `GET /api/dashboard` | whole dashboard payload |
| `GET /api/stats` | KPI cards |
| `GET /api/metrics`, `GET /api/metrics/<id>` | chart series per KPI |
| `GET /api/tracks` | per-fish rows |
| `GET /api/activity` | movement vs activity comparison |

## Deployment

The same app runs in two roles.

**1. The tracker**, on a machine that can see the tank. It opens the camera,
runs the tracking and serves the video. Install both requirement files and set
`LIVE_TANK_TOKEN` whenever the machine is reachable from outside your network:

```bash
pip install -r requirements.txt -r requirements-tracker.txt
python app.py
```

**2. The public site** (Vercel, Render), which has no camera. `Procfile` and
`render.yaml` run it under gunicorn; `vercel.json` covers Vercel. Point it at
the tracker with two environment variables in the host's settings:

| Variable | Value |
| --- | --- |
| `TRACKER_ORIGIN` | public URL of the tracker, e.g. `https://tank.example.com` |
| `TRACKER_TOKEN` | the same string as the tracker's `LIVE_TANK_TOKEN`, if it uses one |

If you cannot set variables on the host, put the tracker's URL in
`tracker_origin.txt` instead; it is committed, and `TRACKER_ORIGIN` overrides
it. A machine that has its own camera ignores both and serves its own tracking,
so the tracker never proxies itself.

The site then fetches the dashboard and video frames from the tracker **server
side**, so the token never reaches a visitor's browser. Only `requirements.txt`
is installed there, which keeps the deployment inside cloud function size
limits — the vision packages stay in `requirements-tracker.txt`.

With no `TRACKER_ORIGIN` the site serves the sample dashboard and says "No
camera configured" on the live panel, so it always deploys cleanly.

### Publishing the tracker

The tracker sits on a private network, so give it a public URL with a tunnel,
for example Cloudflare Tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:5000          # quick, random URL
```

A quick tunnel's URL changes every restart; a reserved ngrok domain or a named
Cloudflare tunnel stays put. Put whichever you use in `tracker_origin.txt`.

Set `LIVE_TANK_TOKEN` on the tracker if the feed should not be public: the
tunnel makes it reachable by anyone who has the URL. With a token set, tunnel
traffic must carry it and only the API is served, never the site itself.

### Video on the public site

A serverless host cannot relay a continuous MJPEG stream, so `/api/config`
tells the page which way to take the video:

- **`mjpeg`** — the tracker itself, or any host on the tank's network: the full
  stream at camera rate.
- **`snapshot`** — a site proxying a remote tracker: one annotated frame roughly
  every 0.7 s, which keeps cloud bandwidth sane.

Because the tracker keeps state in memory, run a single worker process.

## Known limits

- Detection is by motion and colour, not a trained model. A fish sitting still
  through the first ten seconds is not detected until it moves, and two fish
  crossing in an X before either was tracked alone can share one box until they
  separate.
- Speeds are in pixels per second until `PX_PER_CM` is set.
- Identities are conservative by design: a genuinely new fish takes a few
  seconds of being clearly visible before it gets its own number.

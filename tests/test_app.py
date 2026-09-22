import os
import pathlib
import tempfile
import unittest
import unittest.mock

# The camera stays shut during tests, so the dashboard serves placeholder data.
os.environ["LIVE_TANK_OFFLINE"] = "1"

import remote

# Tests decide for themselves whether there is a remote tracker, so ignore the
# committed tracker_origin.txt.
remote.ORIGIN_FILE = pathlib.Path(tempfile.gettempdir()) / "live-tank-no-such-origin"

from app import app


class LiveTankPageTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_index_ok_live_tank(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("Live Tank", html)
        self.assertIn("Tracking readout", html)
        self.assertIn('id="stats"', html)

    def test_index_has_stats_and_api_links(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("Fish in view", html)
        self.assertIn("Avg movement", html)
        self.assertIn('data-metric="view"', html)
        self.assertIn('data-metric="movement"', html)
        self.assertIn('class="dock"', html)
        self.assertIn('id="dockScrim"', html)
        self.assertIn('class="kpi-open"', html)
        self.assertIn("stat-panel", html)
        self.assertIn('id="detailPanel"', html)
        self.assertIn('id="fishPanel"', html)
        self.assertIn('id="kpiModalClone"', html)
        self.assertEqual(html.count('class="kpi-open"'), 10)
        self.assertIn('href="#stats"', html)
        self.assertIn('id="activity"', html)
        self.assertIn('id="log"', html)
        self.assertIn("Movement vs activity", html)
        self.assertIn("Per-fish comparison", html)
        self.assertIn("/api/dashboard", html)
        self.assertIn("/api/stats", html)
        self.assertIn("/api/tracks", html)
        self.assertIn("/api/activity", html)

    def test_index_embeds_the_live_camera(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="live"', html)
        self.assertIn('id="liveVideo"', html)
        self.assertIn("/api/video.mjpg", html)
        self.assertIn("/api/vision", html)
        self.assertIn('href="/live"', html)
        self.assertIn('href="#live"', html)

    def test_index_omits_aquaguard(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("AquaGuard", html)
        self.assertNotIn("aquaguard", html.lower())

    def test_predict_routes_gone(self):
        self.assertEqual(self.client.get("/api/predict_latest").status_code, 404)
        self.assertEqual(self.client.post("/api/predict_manual").status_code, 404)


class LiveViewTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_full_screen_view_renders(self):
        res = self.client.get("/live")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn("/api/video.mjpg", html)
        self.assertIn('id="spark"', html)
        self.assertIn('id="mask"', html)

    def test_vision_state_reports_no_camera_when_offline(self):
        data = self.client.get("/api/vision").get_json()
        self.assertFalse(data["configured"])
        self.assertEqual(data["source"]["status"], "NO CAMERA")

    def test_layer_and_relearn_routes_accept_posts(self):
        self.assertEqual(self.client.post("/api/layers", json={"trails": False}).status_code, 200)
        self.assertEqual(self.client.post("/api/relearn").status_code, 200)

    def test_config_reports_streaming_mode(self):
        data = self.client.get("/api/config").get_json()
        self.assertEqual(data["stream"], "mjpeg")     # no remote tracker configured
        self.assertFalse(data["remote"])

    def test_snapshot_unavailable_without_a_camera(self):
        self.assertEqual(self.client.get("/api/snapshot.jpg").status_code, 503)


class TokenGuardTests(unittest.TestCase):
    """LIVE_TANK_TOKEN guards traffic arriving through a tunnel, not the LAN."""

    TUNNELLED = {"X-Forwarded-For": "203.0.113.9"}

    def setUp(self):
        self.client = app.test_client()
        os.environ["LIVE_TANK_TOKEN"] = "secret-token"

    def tearDown(self):
        os.environ.pop("LIVE_TANK_TOKEN", None)

    def test_tunnelled_api_needs_the_token(self):
        self.assertEqual(self.client.get("/api/vision", headers=self.TUNNELLED).status_code, 401)

    def test_tunnelled_token_in_header_or_query_is_accepted(self):
        headers = {**self.TUNNELLED, "X-Tank-Token": "secret-token"}
        self.assertEqual(self.client.get("/api/vision", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/vision?t=secret-token", headers=self.TUNNELLED).status_code, 200)

    def test_tunnel_does_not_serve_the_site(self):
        self.assertEqual(self.client.get("/", headers=self.TUNNELLED).status_code, 404)
        self.assertEqual(self.client.get("/live", headers=self.TUNNELLED).status_code, 404)

    def test_the_lab_network_is_not_asked_for_a_token(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/live").status_code, 200)
        self.assertEqual(self.client.get("/api/vision").status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard").status_code, 200)


class RemoteTrackerTests(unittest.TestCase):
    """With TRACKER_ORIGIN set the site proxies instead of tracking itself."""

    def setUp(self):
        self.client = app.test_client()
        os.environ["TRACKER_ORIGIN"] = "https://tank.example.com"

    def tearDown(self):
        os.environ.pop("TRACKER_ORIGIN", None)

    def test_a_machine_with_its_own_camera_never_proxies(self):
        remote._own_camera.cache_clear()
        try:
            with unittest.mock.patch.object(remote, "_own_camera", lambda: True):
                self.assertFalse(remote.enabled())
        finally:
            remote._own_camera.cache_clear()

    def test_origin_file_is_used_when_env_is_unset(self):
        os.environ.pop("TRACKER_ORIGIN", None)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write("# a comment\nhttps://tank.example.org/\n")
            path = handle.name
        original = remote.ORIGIN_FILE
        try:
            remote.ORIGIN_FILE = pathlib.Path(path)
            self.assertEqual(remote.origin(), "https://tank.example.org")
        finally:
            remote.ORIGIN_FILE = original
            os.unlink(path)

    def test_env_beats_the_file(self):
        os.environ["TRACKER_ORIGIN"] = "https://from-env.example.com/"
        self.assertEqual(remote.origin(), "https://from-env.example.com")

    def test_config_switches_to_snapshots(self):
        data = self.client.get("/api/config").get_json()
        self.assertEqual(data["stream"], "snapshot")
        self.assertTrue(data["remote"])

    def test_stream_routes_are_not_served(self):
        self.assertEqual(self.client.get("/api/video.mjpg").status_code, 404)
        self.assertEqual(self.client.get("/api/mask.mjpg").status_code, 404)

    def test_unreachable_tracker_is_reported_not_crashed(self):
        data = self.client.get("/api/vision").get_json()
        self.assertFalse(data["configured"])
        self.assertEqual(data["source"]["status"], "NO CAMERA")
        self.assertEqual(self.client.get("/api/snapshot.jpg").status_code, 502)

    def test_dashboard_falls_back_to_sample_data(self):
        data = self.client.get("/api/dashboard").get_json()
        self.assertEqual(data["source"], "placeholder")


class TrackingApiTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_dashboard_payload(self):
        res = self.client.get("/api/dashboard")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["source"], "placeholder")
        self.assertIn("updated_at", data)
        self.assertEqual(len(data["cards"]), 5)
        self.assertEqual({card["id"] for card in data["cards"]}, {
            "view", "movement", "peak", "confidence", "tracks"
        })
        self.assertIn("movement", data["metrics"])
        self.assertGreaterEqual(len(data["tracks"]), 1)
        self.assertIn("values", data["comparison"]["movement"])
        self.assertIn("values", data["comparison"]["activity"])
        self.assertGreaterEqual(len(data["comparison"]["by_fish"]), 1)

    def test_stats_cards(self):
        data = self.client.get("/api/stats").get_json()
        view = next(card for card in data["cards"] if card["id"] == "view")
        self.assertEqual(view["value"], 12)
        self.assertEqual(view["display"], "12")

    def test_metric_by_id(self):
        res = self.client.get("/api/metrics/movement")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["id"], "movement")
        self.assertEqual(data["value"], "4.6 cm/s")
        self.assertEqual(len(data["series"]), 12)

    def test_unknown_metric(self):
        res = self.client.get("/api/metrics/not-a-metric")
        self.assertEqual(res.status_code, 404)

    def test_tracks_and_activity(self):
        tracks = self.client.get("/api/tracks").get_json()["tracks"]
        self.assertEqual(tracks[0]["id"], "Fish 03")
        self.assertIn("activity", tracks[0])
        activity = self.client.get("/api/activity").get_json()["comparison"]
        self.assertEqual(len(activity["movement"]["values"]), len(activity["activity"]["values"]))
        self.assertEqual(activity["by_fish"][0]["id"], "Fish 03")


class DashboardMappingTests(unittest.TestCase):
    """The live payload must keep the shape the page expects."""

    def test_zones_and_trend(self):
        from live_tank import dashboard

        self.assertEqual(dashboard.zone_of({"pos": (0.5, 0.1)}), "Surface")
        self.assertEqual(dashboard.zone_of({"pos": (0.5, 0.5)}), "Mid-water")
        self.assertEqual(dashboard.zone_of({"pos": (0.5, 0.9)}), "Bottom")
        self.assertEqual(dashboard.trend([2, 4]), (100.0, "up"))
        self.assertEqual(dashboard.trend([4, 2]), (50.0, "down"))
        self.assertEqual(dashboard.trend([]), (0.0, "up"))
        self.assertEqual(len(dashboard.axis_labels()), 4)

    def test_card_values(self):
        from live_tank import dashboard
        from live_tank.config import load_settings

        settings = load_settings()
        fish = [
            {"speed_px": 40, "speed": 40, "confidence": 98},
            {"speed_px": 10, "speed": 10, "confidence": 90},
        ]
        values = dashboard.card_values(fish, {"onscreen": 2, "known": 3}, settings)
        self.assertEqual(values["view"], 2)
        self.assertEqual(values["tracks"], 3)
        self.assertEqual(values["movement"], 25)
        self.assertEqual(values["peak"], 40)
        self.assertEqual(values["confidence"], 94)   # percent, not 9400

    def test_fish_row_shape(self):
        from live_tank import dashboard
        from live_tank.config import load_settings

        row = dashboard._fish_row(
            {"id": 3, "conf": 0.91, "speed": 5, "age_s": 90, "matched": True,
             "pos": (0.4, 0.8), "heading": 200, "size": [100, 40]},
            load_settings())
        self.assertEqual(row["id"], "LMB 03")
        self.assertEqual(row["zone"], "Bottom")
        self.assertEqual(row["confidence"], 91)
        self.assertEqual(row["status"], "idle")
        self.assertEqual(row["dwell_min"], 1.5)


if __name__ == "__main__":
    unittest.main()

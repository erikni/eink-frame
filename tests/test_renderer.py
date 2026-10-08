"""Behavioral checks for calendar lifecycle, scheduling, bitplanes and HTTP auth."""

import importlib.util
import json
import struct
import threading
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

import yaml
from PIL import Image

from renderer import app

CFG = app.validate(json.loads(Path("config.example.json").read_text(encoding="utf-8")))
ZONE = ZoneInfo("Europe/Prague")


class RendererTests(unittest.TestCase):
    """Exercise externally visible dashboard behavior and protocol boundaries."""

    def test_quote_helpers_and_source_errors(self):
        """Read configured helpers only for empty agendas and expose failures."""
        now = datetime(2026, 10, 8, 12, tzinfo=ZONE)
        responses = [{"state": "Vlastní citát"}, {"state": "Autor"}]
        with patch.object(app, "collect", return_value=([], [], [])):
            with patch.object(app, "api", side_effect=responses) as fetch:
                data = app.collect_dashboard(CFG, now, False)
            self.assertEqual((data.quote, data.author), ("Vlastní citát", "Autor"))
            self.assertEqual(
                fetch.call_args_list[0].args[0],
                "/api/states/" + CFG["empty_calendar_quote_entity"],
            )
            with patch.object(app, "api", return_value={"state": "unavailable"}):
                failed = app.collect_dashboard(CFG, now, False)
            self.assertEqual(failed.errors, [app.QUOTE_ERROR])
            self.assertEqual(failed.quote, "")
        with patch.object(app, "collect", return_value=([], [], [app.CALENDAR_ERROR])):
            with patch.object(app, "api") as fetch:
                app.collect_dashboard(CFG, now, False)
            fetch.assert_not_called()
        with patch.object(app, "api") as fetch:
            demo = app.collect_dashboard(CFG, now, True, True)
        fetch.assert_not_called()
        self.assertEqual(demo.quote, app.DEFAULT_QUOTE)

    def test_custom_entities_keep_combined_temperatures(self):
        """Changing IDs must preserve the role-based outdoor temperature row."""
        config = json.loads(json.dumps(CFG))
        for index, metric in enumerate(config["metrics"]):
            metric["entity"] = f"sensor.custom_{index}"
        rows = app.metric_rows(config, ["12,4 °C", "22 °C", "16,8 °C"])
        self.assertEqual([value for _, value in rows], ["12.4 → 16.8 °C", "22 °C"])
        rows = app.metric_rows(config, ["12,40 °C", "22 °C", "12,4 °C"])
        self.assertEqual(rows[0][1], "12.4 °C")

    def test_default_ha_url(self):
        """Use the documented local HA address when HA_URL is absent."""
        with patch.dict(app.os.environ, {"HA_TOKEN": "test-only"}, clear=True):
            with patch.object(app, "urlopen") as fetch:
                fetch.return_value.__enter__.return_value.read.return_value = b"{}"
                self.assertEqual(app.api("/api/"), {})
        self.assertEqual(
            fetch.call_args.args[0].full_url, "http://homeassistant.local:8123/api/"
        )

    def test_bitplanes_and_crc(self):
        """Verify active-low bit order, both planes and CRC over the complete body."""
        image = Image.new("RGB", (800, 480), "white")
        image.putpixel((0, 0), (0, 0, 0))
        image.putpixel((7, 0), app.RED)
        data = app.packet(image, 1800)
        self.assertEqual(len(data), 96016)
        magic, width, height, sleep, crc = struct.unpack(">4sHHII", data[:16])
        self.assertEqual((magic, width, height, sleep), (b"EIF1", 800, 480, 1800))
        self.assertEqual(data[16], 0x7F)
        self.assertEqual(data[16 + 48000], 0xFE)
        self.assertEqual(crc, zlib.crc32(data[16:]))
        self.assertNotEqual(crc, zlib.crc32(data[17:]))

    def test_schedule_boundaries_and_event(self):
        """Wake at day/night boundaries and nearer calendar transitions."""
        now = datetime(2026, 10, 8, 21, 45, tzinfo=ZONE)
        self.assertEqual(app.next_wake(now, CFG), 900)
        now = now.replace(hour=22, minute=1)
        self.assertEqual(app.next_wake(now, CFG), 7140)
        event = {"start": now.replace(minute=12), "end": now.replace(minute=20)}
        self.assertEqual(app.next_wake(now, CFG, [event]), 660)

    def test_dst_spring_and_fall(self):
        """Reject skipped spring slots and consider both repeated autumn hours."""
        # Czech spring jump: 01:59 -> 03:00. Next night slot 04:00.
        now = datetime(2026, 3, 29, 1, 59, tzinfo=ZONE)
        self.assertEqual(app.next_wake(now, CFG), 3660)
        # Both occurrences of 02:00 are legal slots during autumn switch.
        now = datetime(2026, 10, 25, 2, 1, tzinfo=ZONE, fold=0)
        self.assertEqual(app.next_wake(now, CFG), 3540)

    def test_calendar_and_missing_metric(self):
        """Retain all-day events, hide expired entries and mark unavailable sensors."""
        now = datetime(2026, 10, 8, 12, tzinfo=ZONE)

        def fake(path):
            """Return controlled HA responses without contacting a live server."""
            if path.startswith("/api/calendars/"):
                return [
                    {
                        "summary": "Celý den",
                        "start": {"date": "2026-10-08"},
                        "end": {"date": "2026-10-09"},
                    },
                    {
                        "summary": "Minulost",
                        "start": {"dateTime": "2026-10-08T08:00:00+02:00"},
                        "end": {"dateTime": "2026-10-08T09:00:00+02:00"},
                    },
                ]
            if "outdoor_temperature" in path:
                return {"state": "12.4", "attributes": {}}
            return {"state": "unavailable"}

        with patch.object(app, "api", side_effect=fake):
            events, values, errors = app.collect(CFG, now, False)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["all_day"])
        self.assertEqual(values, ["12,4 °C", "—", "—"])
        self.assertTrue(errors)

    def test_calendar_lifecycle_and_background(self):
        """Use inclusive starts, exclusive ends and red ongoing-event backgrounds."""
        now = datetime(2026, 10, 8, 8, 30, tzinfo=ZONE)
        events, values, errors = app.collect(CFG, now, True)
        current = events[0]
        self.assertTrue(app.is_running(current, now))
        self.assertTrue(app.is_running(current, current["start"]))
        self.assertFalse(app.is_running(current, current["end"]))
        self.assertNotIn(current, app.visible_events(events, current["end"]))
        image = app.render(CFG, now, app.DashboardData(events, values, errors), True)
        self.assertEqual(image.getpixel((210, 72)), app.RED)
        self.assertEqual(image.getpixel((1, 479)), (0, 0, 0))
        future = app.render(
            CFG,
            current["start"].replace(hour=7),
            app.DashboardData(events, values, errors),
            True,
        )
        self.assertEqual(future.getpixel((210, 72)), (255, 255, 255))
        all_day = {
            "summary": "Celý den",
            "all_day": True,
            "start": datetime(2026, 10, 8, tzinfo=ZONE),
            "end": datetime(2026, 10, 9, tzinfo=ZONE),
        }
        self.assertTrue(app.is_running(all_day, now))
        self.assertEqual(app.visible_events([all_day], all_day["end"]), [])

    def test_max_temperature_entity(self):
        """Read the configured forecast/maximum helper rather than computing it."""
        now = datetime(2026, 10, 8, 12, tzinfo=ZONE)

        def fake(path):
            """Return controlled HA responses without contacting a live server."""
            if path.startswith("/api/calendars/"):
                return []
            if "outdoor_effective_temperature" in path:
                return {"state": "16.8", "attributes": {}}
            return {"state": "12.4", "attributes": {}}

        with patch.object(app, "api", side_effect=fake):
            _, values, errors = app.collect(CFG, now, False)
        self.assertEqual(values[-1], "16,8 °C")
        self.assertEqual(errors, [])

    def test_addon_config_matches_entities(self):
        """Keep app option translation consistent with the standalone example."""
        spec = importlib.util.spec_from_file_location(
            "entrypoint", "addon/eink_frame/entrypoint.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        options = yaml.safe_load(
            Path("addon/eink_frame/config.yaml").read_text(encoding="utf-8")
        )["options"]
        self.assertEqual(module.config(options), CFG)

    def test_http_auth_and_payload(self):
        """Require a token and serve complete CRC-valid binary and PNG images."""
        with patch.dict(app.os.environ, {"FRAME_TOKEN": "test-token"}):
            server = app.create_server(app.Dashboard(CFG, True), "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        urlopen = build_opener(ProxyHandler({})).open
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen(url + "/frame.bin")
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            req = Request(
                url + "/frame.bin", headers={"Authorization": "Bearer test-token"}
            )
            with urlopen(req) as response:
                data = response.read()
                self.assertEqual(len(data), 96016)
                self.assertEqual(
                    zlib.crc32(data[16:]), struct.unpack(">I", data[12:16])[0]
                )
            req = Request(
                url + "/preview.png", headers={"Authorization": "Bearer test-token"}
            )
            with urlopen(req) as response:
                self.assertEqual(response.read(8), b"\x89PNG\r\n\x1a\n")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_render_palette_and_size(self):
        """Render only the three colors supported by the selected display."""
        now = datetime(2026, 10, 8, 7, tzinfo=ZONE)
        events, values, errors = app.collect(CFG, now, True)
        image = app.render(CFG, now, app.DashboardData(events, values, errors), True)
        self.assertEqual(image.size, (800, 480))
        self.assertEqual(set(image.getdata()), {(255, 255, 255), (0, 0, 0), app.RED})


if __name__ == "__main__":
    unittest.main()

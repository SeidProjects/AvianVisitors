import datetime as dt
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo

import export_snapshots as exporter
import run_sync
import seasonality as annual


class SeasonalityTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE detections (Date TEXT, Time TEXT, Sci_Name TEXT, Com_Name TEXT, Confidence REAL, File_Name TEXT)")
        self.zone = ZoneInfo("America/New_York")
        self.instant = dt.datetime.fromisoformat("2026-10-11T16:00:00+00:00")

    def tearDown(self):
        self.db.close()

    def add(self, date="2026-10-11", time="09:00:00", sci="Example bird", file="clip.mp3"):
        self.db.execute("INSERT INTO detections VALUES (?, ?, ?, ?, ?, ?)",
                        (date, time, sci, "Example common name", .8, file))

    def collect(self, **kwargs):
        self.db.commit()
        self.db.execute("BEGIN")
        try:
            return annual.collect_seasonality(self.db, self.instant, self.zone, **kwargs)
        finally:
            self.db.rollback()

    def test_window_leap_clamp_and_civil_day_count(self):
        start, end = annual.annual_window(dt.date(2024, 2, 29))
        self.assertEqual(start.isoformat(), "2023-03-01")
        self.assertEqual((end - start).days + 1, 366)
        start, end = annual.annual_window(dt.date(2025, 2, 28))
        self.assertEqual(start.isoformat(), "2024-02-29")
        self.assertEqual((end - start).days + 1, 366)
        start, end = annual.annual_window(dt.date(2026, 10, 11))
        self.assertEqual(start.isoformat(), "2025-10-12")
        self.assertEqual((end - start).days + 1, 365)

    def test_empty_calendar_preserves_unknown_coverage(self):
        aggregate = self.collect()
        self.assertEqual(aggregate["species"], [])
        self.assertEqual(aggregate["totals"], {"detections": 0})
        self.assertEqual(aggregate["coverage"], {"basis": "detections_do_not_establish_uptime",
            "default": "unknown", "partial_dates": ["2026-10-11"], "first_observation_date": None})

    def test_complete_counts_include_every_occurrence_beyond_500(self):
        for _ in range(1051):
            self.add(date="2026-10-10")
        aggregate = self.collect()
        self.assertEqual(aggregate["totals"], {"detections": 1051})
        self.assertEqual(aggregate["species"][0]["counts"], [[363, 1051]])
        self.assertEqual(aggregate["species"][0]["detected_days"], 1)
        self.assertEqual(aggregate["coverage"]["first_observation_date"], "2026-10-10")

    def test_future_and_invalid_dates_and_times_excluded(self):
        self.add()
        self.add(time="12:00:01")
        self.add(date="2026-10-12")
        self.add(date="2026-02-30")
        self.add(time="25:00:00")
        self.add(date="2025-10-11")
        aggregate = self.collect()
        self.assertEqual(aggregate["excluded_invalid_rows"], 3)
        self.assertEqual(aggregate["species"][0]["total"], 1)

    def test_dst_gap_rejected_and_repeated_hour_accepted(self):
        self.add(date="2026-03-08", time="02:30:00")
        self.add(date="2026-03-08", time="03:30:00")
        self.instant = dt.datetime.fromisoformat("2026-11-01T06:30:00+00:00")
        self.add(date="2026-11-01", time="01:20:00")
        aggregate = self.collect()
        self.assertEqual(aggregate["excluded_invalid_rows"], 1)
        self.assertEqual(aggregate["totals"], {"detections": 2})
        self.assertEqual(aggregate["end_date"], "2026-11-01")

    def test_recorder_date_is_independent_of_utc_and_visitor_date(self):
        self.instant = dt.datetime.fromisoformat("2026-10-12T02:30:00+00:00")
        self.add(time="22:29:59")
        aggregate = self.collect()
        self.assertEqual(aggregate["end_date"], "2026-10-11")
        self.assertEqual(aggregate["species"][0]["counts"], [[364, 1]])

    def test_media_and_filenames_are_not_read_or_required(self):
        self.add(file="private/recorder/source.mp3")
        self.add(file=None)
        selects = []
        self.db.set_trace_callback(selects.append)
        aggregate = self.collect()
        self.assertEqual(aggregate["totals"], {"detections": 2})
        self.assertTrue(all("File_Name" not in sql and "Confidence" not in sql for sql in selects))
        self.assertNotIn(b"private/recorder", exporter._json_bytes(aggregate))
        self.assertNotIn(b"media", exporter._json_bytes(aggregate))

    def test_limits_reject_complete_source_instead_of_truncating(self):
        self.add()
        self.add(sci="Second bird")
        with self.assertRaisesRegex(ValueError, "source exceeds row limit"):
            self.collect(max_rows=1)
        with self.assertRaisesRegex(ValueError, "source exceeds species limit"):
            self.collect(max_species=1)

    def test_caller_transaction_and_shared_guard_required(self):
        self.db.commit()
        with self.assertRaisesRegex(ValueError, "read transaction"):
            annual.collect_seasonality(self.db, self.instant, self.zone)
        self.add()
        class Guard:
            calls = 0
            def check(inner):
                inner.calls += 1
        guard = Guard()
        self.collect(guard=guard)
        self.assertGreaterEqual(guard.calls, 3)

    def test_explicit_optional_flag_preserves_original_cache_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / "birds.db"
            path.touch()
            sync = SimpleNamespace(state_dir=root / "state", endpoint_url="https://storage.example",
                bucket="bird-media", public_origin="https://media.birds.example")
            environment = {"AVIAN_SYNC_DB_PATH": str(path), "AVIAN_SYNC_TIMEZONE": self.zone.key}
            disabled = run_sync.FrontendConfig.from_env(environment, sync)
            self.assertFalse(disabled.seasonality)
            environment["AVIAN_SYNC_SEASONALITY"] = "1"
            enabled = run_sync.FrontendConfig.from_env(environment, sync)
            self.assertTrue(enabled.seasonality)
            self.assertEqual(enabled.cache_config(sync), disabled.cache_config(sync))
            environment["AVIAN_SYNC_SEASONALITY"] = "true"
            with self.assertRaisesRegex(ValueError, "invalid boolean"):
                run_sync.FrontendConfig.from_env(environment, sync)


if __name__ == "__main__":
    unittest.main()

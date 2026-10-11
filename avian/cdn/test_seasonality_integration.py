"""Offline checks across annual counts, public serialization and cache admission."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from zoneinfo import ZoneInfo

import export_snapshots as exporter
import publish_cache
import seasonality


ORIGIN = "https://media.example.net"
SINGLE = "Example bird"
NOW = dt.datetime.fromisoformat("2026-10-11T16:00:00+00:00")


class SeasonalityIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE detections(Date TEXT, Time TEXT, Sci_Name TEXT, Com_Name TEXT, Confidence REAL, File_Name TEXT)")
        self.temporary = tempfile.TemporaryDirectory()
        self.output = Path(self.temporary.name)

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def add(self, date="2026-10-10", time="08:00:00", sci=SINGLE, file="private-source-alias.mp3", confidence=.91):
        self.db.execute("INSERT INTO detections VALUES(?,?,?,?,?,?)", (date, time, sci, "Example Bird", confidence, file))

    def collect(self, *, enabled=True, generation="fixture", instant=NOW):
        self.db.commit()
        self.db.execute("BEGIN")
        try:
            return exporter.collect_snapshot(self.db, instant, ZoneInfo("America/New_York"),
                generation, seasonality=enabled)
        finally:
            self.db.rollback()

    def publish(self, collected, *, limit=4 * 1024 * 1024):
        responses, paths, metadata = collected
        return exporter.publish_local(self.output, metadata["generation"], responses, paths, metadata,
            max_candidate_bytes=limit, max_output_bytes=8 * 1024 * 1024, min_free_disk_bytes=0)

    def read(self, path):
        return json.loads((self.output / path).read_bytes())

    def cache_config(self):
        return publish_cache._config({"endpoint_url": "https://objects.example.net", "bucket": "test-birds",
            "media_origin": ORIGIN, "single_publisher": True})

    def test_disabled_feature_preserves_schema_two_routes(self):
        self.add()
        collected = self.collect(enabled=False)
        self.assertEqual(collected[2]["schema_version"], 2)
        self.assertNotIn("capabilities", collected[2])
        index = self.publish(collected)
        self.assertEqual(set(index["routes"]), {"stats", "lifelist", "timeseries", "firstseen", "recent", "species"})
        publish_cache._index(exporter._json_bytes(index), self.cache_config())

    def test_complete_daily_counts_exceed_latest_five_hundred_window(self):
        for number in range(601):
            date = "2026-10-09" if number < 100 else "2026-10-10"
            self.add(date=date, file="private-source-alias-%04d.mp3" % number)
        collected = self.collect()
        details = [payload for name, payload in collected[0].items() if name.startswith("species/")]
        self.assertEqual(len(details[0]["detections"]), 500)
        index = self.publish(collected)
        aggregate = self.read(index["routes"]["seasonality"])
        bird = aggregate["species"][0]
        self.assertEqual(bird["total"], 601)
        self.assertEqual(bird["detected_days"], 2)
        start = dt.date.fromisoformat(aggregate["start_date"])
        counts = {str(start + dt.timedelta(days=offset)): count for offset, count in bird["counts"]}
        self.assertEqual(counts, {"2026-10-09": 100, "2026-10-10": 501})
        self.assertEqual(aggregate["coverage"]["default"], "unknown")
        self.assertNotIn("recording_days", index["routes"])

    def test_feature_adds_one_shared_aggregate_with_exact_hash_closure(self):
        self.add()
        self.add(sci="Another bird", file="other-private-source-alias.mp3")
        disabled = self.publish(self.collect(enabled=False, generation="off"))
        enabled = self.publish(self.collect(generation="on"))
        self.assertEqual(enabled["schema_version"], 3)
        self.assertEqual(enabled["capabilities"], ["seasonality-v1"])
        self.assertEqual(set(enabled["routes"]), set(disabled["routes"]) | {"seasonality"})
        for route in disabled["routes"]:
            self.assertEqual(enabled["routes"][route], disabled["routes"][route])
        self.assertEqual(len(enabled["objects"]), len(disabled["objects"]) + 1)
        path = enabled["routes"]["seasonality"]
        body = (self.output / path).read_bytes()
        descriptor = enabled["objects"][path]
        self.assertEqual(hashlib.sha256(body).hexdigest(), descriptor["sha256"])
        self.assertEqual(len(body), descriptor["bytes"])
        self.assertNotIn(b"private-source-alias", body)
        for forbidden in (b"_recording_files", b'"file"', b'"media"', ORIGIN.encode()):
            self.assertNotIn(forbidden, body)
        parsed, identities = publish_cache._index(exporter._json_bytes(enabled), self.cache_config())
        self.assertEqual(parsed["generation"], "on")
        self.assertEqual(len(identities), len(enabled["objects"]) + 1)
        self.assertLess(sum(value["bytes"] for value in enabled["objects"].values()) + 2 * len(exporter._json_bytes(enabled)), 4 * 1024 * 1024)

    def test_clock_only_refresh_reuses_the_annual_aggregate(self):
        self.add()
        first = self.publish(self.collect(generation="first"))
        second = self.publish(self.collect(generation="second", instant=NOW + dt.timedelta(minutes=1)))
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(first["routes"]["seasonality"], second["routes"]["seasonality"])

    def test_rejected_complete_candidate_preserves_selected_index(self):
        self.add()
        selected = self.publish(self.collect(generation="selected"))
        before = (self.output / "snapshot-index.json").read_bytes()
        for number in range(25):
            self.add(file="additional-%04d.mp3" % number)
        with self.assertRaisesRegex(ValueError, "candidate serialization limit"):
            self.publish(self.collect(generation="rejected"), limit=200)
        self.assertEqual((self.output / "snapshot-index.json").read_bytes(), before)
        self.assertEqual(json.loads(before)["generation"], selected["generation"])
        self.assertFalse((self.output / "generations" / "rejected").exists())

    def test_cache_rejects_unsupported_capabilities_and_unlisted_aggregate(self):
        self.add()
        index = self.publish(self.collect())
        original = index["routes"]["seasonality"]
        index["routes"]["seasonality"] = "objects/" + "f" * 64 + ".json"
        with self.assertRaisesRegex(ValueError, "exactly"):
            publish_cache._index(exporter._json_bytes(index), self.cache_config())
        index["routes"]["seasonality"] = original
        index["capabilities"] = ["seasonality-v1", "unknown-feature"]
        with self.assertRaisesRegex(ValueError, "capabilities"):
            publish_cache._index(exporter._json_bytes(index), self.cache_config())

    def test_recorder_calendar_excludes_invalid_future_and_nonexistent_times(self):
        self.add(date="2026-10-11", time="12:00:01")
        self.add(date="2026-10-11", time="12:00:00", file="now.mp3")
        self.add(date="2026-02-30", time="12:00:00", file="bad-date.mp3")
        self.add(date="2026-03-08", time="02:30:00", file="spring-gap.mp3")
        self.add(date="2025-11-02", time="01:30:00", file="repeat-hour.mp3")
        self.add(date="2025-11-02", time="01:30:00", file="repeat-hour.mp3", confidence=.92)
        index = self.publish(self.collect())
        aggregate = self.read(index["routes"]["seasonality"])
        self.assertEqual(aggregate["excluded_invalid_rows"], 3)
        self.assertEqual(sum(bird["total"] for bird in aggregate["species"]), 3)
        start = dt.date.fromisoformat(aggregate["start_date"])
        counts = {str(start + dt.timedelta(days=offset)): count for offset, count in aggregate["species"][0]["counts"]}
        self.assertEqual(counts["2025-11-02"], 2)
        self.assertEqual(counts["2026-10-11"], 1)
        self.assertEqual(seasonality.annual_window(dt.date(2024, 2, 29)), (dt.date(2023, 3, 1), dt.date(2024, 2, 29)))
        self.assertEqual(seasonality.annual_window(dt.date(2025, 2, 28)), (dt.date(2024, 2, 29), dt.date(2025, 2, 28)))

    def test_oversized_or_binary_source_fields_do_not_enter_the_aggregate(self):
        self.add()
        self.add(date="2026-10-10" + "x" * 65536)
        self.add(time="08:00:00" + "x" * 65536)
        self.add(time=sqlite3.Binary(b"08:00:00"))
        self.add(sci="x" * 65536)
        self.db.commit()
        self.db.execute("BEGIN")
        try:
            aggregate = seasonality.collect_seasonality(self.db, NOW, ZoneInfo("America/New_York"))
        finally:
            self.db.rollback()
        self.assertEqual(aggregate["totals"]["detections"], 1)
        self.assertEqual(aggregate["excluded_invalid_rows"], 4)
        self.assertLess(len(exporter._json_bytes(aggregate)), 2048)


if __name__ == "__main__":
    unittest.main()

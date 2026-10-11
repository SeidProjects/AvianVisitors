import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

import export_snapshots as exporter


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.db_path = self.source / "birds.db"
        self.output = self.root / "staged"
        self.writer = sqlite3.connect(self.db_path)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("CREATE TABLE detections (Date TEXT, Time TEXT, Sci_Name TEXT, Com_Name TEXT, Confidence REAL, File_Name TEXT)")
        self.sci = "Cyanocitta cristata"
        self.best = "Blue_Jay-97-2026-10-11-bird’s-call.mp3"
        self.latest = "Blue_Jay-42-2026-10-11-newest.mp3"
        self.add("2026-10-11", "10:20:00", self.sci, .97, self.best)
        self.add("2026-10-11", "10:55:00", self.sci, .42, self.latest)
        self.add("2020-07-04", "08:00:00", "Corvus brachyrhynchos", .7, "old.mp3")
        self.now = dt.datetime.fromisoformat("2026-10-11T15:00:00+00:00")

    def tearDown(self):
        self.writer.close()
        self.temporary.cleanup()

    def add(self, date, time, sci, confidence, file):
        self.writer.execute("INSERT INTO detections VALUES (?, ?, ?, ?, ?, ?)",
                            (date, time, sci, "Blue Jay" if sci == self.sci else "American Crow", confidence, file))
        self.writer.commit()

    def export(self, **kwargs):
        return exporter.export(self.db_path, self.output, instant=self.now,
                               generation=kwargs.pop("generation", "fixture"), **kwargs)

    def read(self, index, action, key=None):
        route = index["routes"][action]
        if key is not None:
            route = route[str(key)]
        payload = json.loads((self.output / route).read_text(encoding="utf-8"))
        return exporter.hydrate_payload(payload, index, action)

    def media_fixture(self, include_best=True):
        url = "https://media.birds.example/recordings/sha-new.mp3"
        entry = {"url": url, "sha256": "a" * 64, "bytes": 123}
        recordings = {self.latest: entry}
        if include_best:
            recordings[self.best] = {**entry, "url": "https://media.birds.example/recordings/bird%E2%80%99s-call.mp3"}
        value = {"schema_version": 1, "generation": "media-fixture",
                 "generated_at": "2026-10-11T14:59:00Z", "recordings": recordings,
                 "latest_recording": {self.sci: {"file": self.latest, "url": url}},
                 "images": {self.sci: {"pose1": {**entry, "url": "https://media.birds.example/images/jay.png", "source": "illustrations"}}}}
        path = self.root / "media.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_exact_picker_windows_shapes_and_unicode_media_identity(self):
        index = self.export(media_manifest=self.media_fixture(), media_origin="https://media.birds.example")
        self.assertEqual(set(index["routes"]["recent"]), {"1", "12", "24", "168", "1000000"})
        recent = self.read(index, "recent", 1)["species"][0]
        self.assertEqual(recent["top_file"], self.best)
        self.assertEqual(recent["latest_media"]["file"], self.latest)
        self.assertNotEqual(recent["top_media"]["url"], recent["latest_media"]["url"])
        self.assertIn("%E2%80%99", recent["top_media"]["url"])
        self.assertNotIn("pose2", recent["media_images"])
        self.assertEqual(self.read(index, "stats")["totals"], {"detections": 3, "species": 2})
        self.assertEqual(self.read(index, "recent", 1000000)["species"][-1]["sci"], "Corvus brachyrhynchos")
        self.assertEqual(index["media_manifest_generation"], "media-fixture")
        self.assertEqual(index["snapshot_count"], 11)

    def test_missing_uploaded_media_does_not_remove_detection_or_fall_back(self):
        index = self.export(media_manifest=self.media_fixture(include_best=False), media_origin="https://media.birds.example")
        recent = self.read(index, "recent", 1)["species"][0]
        self.assertEqual(recent["n"], 2)
        self.assertEqual(recent["top_media"], {"available": False, "url": None})
        self.assertTrue(recent["latest_media"]["available"])
        details = self.read(index, "species", self.sci)
        self.assertFalse(details["detections"][1]["media"]["available"])
        self.assertEqual(details["detections"][1]["file"], self.best)

    def test_no_manifest_marks_media_unavailable(self):
        index = self.export()
        self.assertFalse(self.read(index, "recent", 1)["species"][0]["latest_media"]["available"])
        self.assertEqual(self.read(index, "lifelist")["species"][0]["media_images"], {})

    def test_private_image_evidence_never_enters_public_snapshots(self):
        path = self.media_fixture()
        value = json.loads(path.read_text(encoding="utf-8"))
        image = value["images"][self.sci]["pose1"]
        image.update(private_path="synthetic-private-image-evidence", account={"secret": "synthetic-private-secret"}, available=False)
        path.write_text(json.dumps(value), encoding="utf-8")
        index = self.export(media_manifest=path, media_origin="https://media.birds.example")
        for action, key in (("lifelist", None), ("recent", 1), ("species", self.sci)):
            payload = self.read(index, action, key)
            bird = payload if action == "species" else next(row for row in payload["species"] if row["sci"] == self.sci)
            entry = bird["media_images"]["pose1"]
            self.assertEqual(set(entry), {"available", "url", "sha256", "bytes", "source"})
            self.assertTrue(entry["available"])
            self.assertNotIn("synthetic-private", json.dumps(payload))
        image["source"] = "synthetic-private-source-path"
        result = exporter.species_media(value, self.sci)["media_images"]["pose1"]
        self.assertNotIn("source", result)

    def test_latest_recording_accepts_file_only_or_matching_url(self):
        path = self.media_fixture()
        with_url = self.export(media_manifest=path, media_origin="https://media.birds.example")
        expected = self.read(with_url, "recent", 1)["species"][0]["latest_media"]
        media = json.loads(path.read_text(encoding="utf-8"))
        del media["latest_recording"][self.sci]["url"]
        path.write_text(json.dumps(media, ensure_ascii=False), encoding="utf-8")
        file_only = self.export(generation="file-only", media_manifest=path, media_origin="https://media.birds.example")
        self.assertEqual(self.read(file_only, "recent", 1)["species"][0]["latest_media"], expected)
        self.assertTrue(expected["available"])

    def test_explicit_latest_recording_url_mismatch_rejected(self):
        path = self.media_fixture()
        media = json.loads(path.read_text(encoding="utf-8"))
        media["latest_recording"][self.sci]["url"] = media["recordings"][self.best]["url"]
        path.write_text(json.dumps(media, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "latest recording"):
            self.export(media_manifest=path, media_origin="https://media.birds.example")
        self.assertFalse(self.output.exists())

    def test_single_read_transaction_survives_concurrent_writer(self):
        original = exporter._rows
        fired = False

        def interleaved(db, sql, bind=None):
            nonlocal fired
            values = original(db, sql, bind)
            if not fired and "COUNT(*) AS detections" in sql:
                fired = True
                self.add("2026-10-11", "10:59:00", "New species", .8, "new.mp3")
            return values

        with mock.patch.object(exporter, "_rows", side_effect=interleaved):
            index = self.export()
        self.assertTrue(fired)
        self.assertEqual(self.read(index, "stats")["totals"]["detections"], 3)
        self.assertEqual(len(self.read(index, "lifelist")["species"]), 2)
        self.assertEqual(len(index["routes"]["species"]), 2)
        self.assertEqual(self.writer.execute("SELECT COUNT(*) FROM detections").fetchone()[0], 4)

    def test_local_day_and_historical_clock_are_not_host_timezone(self):
        self.now = dt.datetime.fromisoformat("2020-07-04T12:00:00+00:00")
        index = self.export()
        self.assertEqual(self.read(index, "stats")["today"]["detections"], 1)
        self.assertEqual(self.read(index, "stats")["as_of"], "2020-07-04T08:00:00-04:00")
        self.assertEqual(index["published_at"], "2020-07-04T12:00:00Z")
        self.assertEqual(index["timezone"], "America/New_York")
        # PHP includes future dates too; exporter preserves this behavior.
        self.assertEqual(len(self.read(index, "timeseries", 30)["daily"]), 2)

    def test_midnight_bug_preserved_until_explicit_fix(self):
        self.add("2026-10-10", "23:50:00", self.sci, .5, "previous-day.mp3")
        self.add("2026-10-11", "00:10:00", self.sci, .5, "today.mp3")
        self.now = dt.datetime.fromisoformat("2026-10-11T04:15:00+00:00")
        compatibility = self.export()
        self.assertEqual(self.read(compatibility, "stats")["last_hour"]["detections"], 0)
        fixed = self.export(generation="fixed", fix_last_hour=True)
        self.assertEqual(self.read(fixed, "stats")["last_hour"]["detections"], 2)

    def test_species_limit_500_summary_still_counts_all(self):
        self.writer.executemany("INSERT INTO detections VALUES (?, ?, ?, ?, ?, ?)",
                                [("2026-10-09", "08:00:00", self.sci, "Blue Jay", .3, "older-%s.mp3" % n) for n in range(505)])
        self.writer.commit()
        index = self.export()
        detail = self.read(index, "species", self.sci)
        self.assertEqual(len(detail["detections"]), 500)
        self.assertEqual(detail["summary"]["total"], 507)
        self.assertEqual(detail["detections"][0]["file"], self.latest)

    def test_query_count_does_not_grow_per_species(self):
        original = exporter.collect_snapshot
        counts = []

        def traced(db, *args, **kwargs):
            selects = []
            db.set_trace_callback(lambda sql: selects.append(sql) if sql.startswith("SELECT") else None)
            result = original(db, *args, **kwargs)
            counts.append(len(selects))
            return result

        with mock.patch.object(exporter, "collect_snapshot", side_effect=traced):
            self.export()
            self.writer.executemany("INSERT INTO detections VALUES (?, ?, ?, ?, ?, ?)",
                                    [("2026-10-11", "10:40:00", "Species %s" % n, "Bird", .5, "file-%s.mp3" % n) for n in range(30)])
            self.writer.commit()
            self.export(generation="many-species")
        self.assertEqual(counts[0], counts[1])
        self.assertLess(counts[1], 25)

    def test_readonly_handle_rejects_mutation(self):
        original = exporter.collect_snapshot

        def attempted_write(db, *args, **kwargs):
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM detections")
            return original(db, *args, **kwargs)

        with mock.patch.object(exporter, "collect_snapshot", side_effect=attempted_write):
            index = self.export()
        self.assertEqual(self.read(index, "stats")["totals"]["detections"], 3)

    def test_old_pointer_survives_failed_pointer_swap(self):
        self.export()
        previous = (self.output / "snapshot-index.json").read_bytes()
        with mock.patch.object(exporter.os, "replace", side_effect=OSError("simulated pointer failure")):
            with self.assertRaises(OSError):
                self.export(generation="second")
        self.assertEqual((self.output / "snapshot-index.json").read_bytes(), previous)
        self.assertTrue((self.output / "generations/second/manifest.json").is_file())
        self.assertFalse(list(self.output.glob(".snapshot-index-*.json")))

    def test_invalid_schema_keeps_previous_generation(self):
        self.export()
        previous = (self.output / "snapshot-index.json").read_bytes()
        self.writer.execute("ALTER TABLE detections RENAME COLUMN File_Name TO broken")
        self.writer.commit()
        with self.assertRaises(ValueError):
            self.export(generation="invalid")
        self.assertEqual((self.output / "snapshot-index.json").read_bytes(), previous)
        self.assertFalse((self.output / "generations/invalid").exists())

    def test_generation_path_and_media_origin_are_validated(self):
        with self.assertRaises(ValueError):
            self.export(generation="../outside")
        with self.assertRaises(ValueError):
            self.export(media_manifest=self.media_fixture(), media_origin="https://wrong.example")
        with self.assertRaises(ValueError):
            self.export(media_manifest=self.media_fixture())

    def test_clock_only_generation_reuses_all_data_objects_without_rewriting(self):
        first = self.export()
        first_paths = set((self.output / "objects").glob("*.json"))
        first_mtimes = {path: path.stat().st_mtime_ns for path in first_paths}
        self.now += dt.timedelta(seconds=10)
        second = self.export(generation="clock-advanced")
        self.assertEqual(second["schema_version"], 2)
        self.assertNotEqual(first["as_of"], second["as_of"])
        self.assertEqual(first["routes"], second["routes"])
        self.assertEqual(second["publication"]["new_data_objects"], 0)
        self.assertEqual(second["publication"]["new_data_bytes"], 0)
        self.assertEqual(set((self.output / "objects").glob("*.json")), first_paths)
        self.assertEqual({path: path.stat().st_mtime_ns for path in first_paths}, first_mtimes)
        for path in first_paths:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("as_of", raw)
            self.assertNotIn("snapshot", raw)
        self.assertEqual(self.read(second, "stats")["as_of"], second["as_of"])
        self.assertEqual(self.read(second, "stats")["snapshot"]["generation"], "clock-advanced")

    def test_one_species_detail_change_creates_only_changed_data_object(self):
        first = self.export()
        before_count = len(list((self.output / "objects").glob("*.json")))
        self.writer.execute("UPDATE detections SET File_Name=? WHERE File_Name=?",
                            ("changed-detail.mp3", self.latest))
        self.writer.commit()
        second = self.export(generation="one-detail-changed")
        self.assertEqual(second["publication"]["new_data_objects"], 1)
        self.assertEqual(len(list((self.output / "objects").glob("*.json"))), before_count + 1)
        self.assertNotEqual(first["routes"]["species"][self.sci], second["routes"]["species"][self.sci])
        self.assertEqual(first["routes"]["species"]["Corvus brachyrhynchos"],
                         second["routes"]["species"]["Corvus brachyrhynchos"])
        for action in ("stats", "lifelist", "timeseries", "firstseen", "recent"):
            self.assertEqual(first["routes"][action], second["routes"][action])

    def test_corrupt_existing_object_fails_before_pointer_change(self):
        first = self.export()
        prior_pointer = (self.output / "snapshot-index.json").read_bytes()
        object_path = self.output / first["routes"]["stats"]
        object_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "bytes/hash verification"):
            self.export(generation="corrupt-reuse")
        self.assertEqual((self.output / "snapshot-index.json").read_bytes(), prior_pointer)
        self.assertFalse((self.output / "generations/corrupt-reuse").exists())

    def test_object_filename_hash_and_manifest_length_match_actual_bytes(self):
        index = self.export()
        import hashlib
        for path, description in index["objects"].items():
            raw = (self.output / path).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            self.assertEqual(path, "objects/" + digest + ".json")
            self.assertEqual(description, {"sha256": digest, "bytes": len(raw)})

    def test_quota_stop_precedes_any_candidate_json(self):
        with self.assertRaisesRegex(ValueError, "quota would be exceeded"):
            self.export(max_output_bytes=1)
        self.assertFalse(list(self.output.rglob("*.json")))
        self.assertFalse(list(self.output.rglob(".pending-*")))

    def test_retained_bytes_and_two_manifest_copies_count_at_peak(self):
        self.export()
        previous = (self.output / "snapshot-index.json").read_bytes()
        from storage_guard import output_usage
        occupied = output_usage(self.output)["allocated_bytes"]
        self.now += dt.timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "quota would be exceeded"):
            self.export(generation="quota-stop", max_output_bytes=occupied + len(previous))
        self.assertEqual((self.output / "snapshot-index.json").read_bytes(), previous)
        self.assertFalse((self.output / "generations/quota-stop").exists())

    def test_candidate_limit_and_disk_reserve_preserve_pointer(self):
        self.export()
        previous = (self.output / "snapshot-index.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "serialization limit"):
            self.export(generation="small-candidate", max_candidate_bytes=1)
        self.assertEqual((self.output / "snapshot-index.json").read_bytes(), previous)
        with self.assertRaisesRegex(ValueError, "disk reserve"):
            self.export(generation="disk-low", min_free_disk_bytes=10**30)
        self.assertEqual((self.output / "snapshot-index.json").read_bytes(), previous)

    def test_linked_output_and_parallel_publisher_are_rejected(self):
        self.export()
        previous = (self.output / "snapshot-index.json").read_bytes()
        from storage_guard import output_lock
        with output_lock(self.output):
            with self.assertRaises(BlockingIOError):
                self.export(generation="parallel")
        external = self.root / 'unexpected.json'
        external.write_text('{}')
        (self.output / 'linked.json').symlink_to(external)
        with self.assertRaisesRegex(ValueError, "link or special"):
            self.export(generation="linked")
        self.assertEqual((self.output / "snapshot-index.json").read_bytes(), previous)

    def test_distinct_outer_runtime_lock_and_output_lock_can_nest(self):
        import fcntl
        state = self.root / 'publisher-state'
        state.mkdir()
        with (state / 'publisher.lock').open('a') as outer:
            fcntl.flock(outer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            index = self.export()
            self.assertEqual(index['generation'], 'fixture')
            self.assertTrue((self.output / '.snapshot-publisher.lock').is_file())
            fcntl.flock(outer, fcntl.LOCK_UN)


if __name__ == "__main__":
    unittest.main()

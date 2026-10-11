#!/usr/bin/env python3
"""Read-only BirdNET-Pi SQLite snapshot exporter; no network or DB writes."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

from storage_guard import DEFAULT_QUOTA, DEFAULT_RESERVE, output_lock, output_usage, preflight

WINDOWS = (1, 12, 24, 168, 1000000)
REQUIRED_COLUMNS = {"Date", "Time", "Sci_Name", "Com_Name", "Confidence", "File_Name"}


def _rows(db, sql, bind=None):
    return [dict(row) for row in db.execute(sql, bind or {})]


def _iter_rows(db, sql, bind=None):
    for row in db.execute(sql, bind or {}):
        yield dict(row)


def _one(db, sql, bind=None):
    values = _rows(db, sql, bind)
    return values[0] if values else None


def iso_utc(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_media(path, origin):
    if path is None:
        return None
    if not origin:
        raise ValueError("--media-origin is required with --media-manifest")
    origin_parts = urlsplit(origin)
    if (origin_parts.scheme != "https" or not origin_parts.netloc or
            origin_parts.username or origin_parts.password or
            origin_parts.path not in ("", "/") or origin_parts.query or origin_parts.fragment):
        raise ValueError("media origin must be an HTTPS origin without a path or credentials")
    canonical_origin = "https://" + origin_parts.netloc
    media = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(media, dict) or media.get("schema_version") != 1:
        raise ValueError("unsupported media manifest schema")
    for name in ("recordings", "latest_recording", "images"):
        if not isinstance(media.get(name), dict):
            raise ValueError("media manifest requires recordings, latest_recording and images maps")

    def check_entry(entry):
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise ValueError("media object must have a URL")
        parsed = urlsplit(entry["url"])
        if (parsed.scheme != "https" or "https://" + parsed.netloc != canonical_origin or
                parsed.username or parsed.password or parsed.fragment):
            raise ValueError("media object URL is outside the configured CDN origin")
        digest = entry.get("sha256")
        size = entry.get("bytes")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                isinstance(size, bool) or not isinstance(size, int) or size < 0):
            raise ValueError("media object must contain a SHA-256 and non-negative byte count")

    for file_name, entry in media["recordings"].items():
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("recording names must be nonempty strings")
        check_entry(entry)
    for poses in media["images"].values():
        if not isinstance(poses, dict):
            raise ValueError("image poses must be a map")
        for pose, entry in poses.items():
            if pose not in ("pose1", "pose2"):
                raise ValueError("unknown image pose")
            check_entry(entry)
    for latest in media["latest_recording"].values():
        if not isinstance(latest, dict) or not isinstance(latest.get("file"), str):
            raise ValueError("latest recording requires a file name")
        matched = media["recordings"].get(latest["file"])
        if not matched or ("url" in latest and latest["url"] != matched["url"]):
            raise ValueError("latest recording must reference a published recording URL")
    return media


def recording_media(media, file_name):
    entry = media["recordings"].get(file_name) if media else None
    result = {"available": bool(entry), "url": entry["url"] if entry else None}
    if entry:
        result.update({"sha256": entry["sha256"], "bytes": entry["bytes"]})
    return result


def species_media(media, sci):
    latest = media["latest_recording"].get(sci) if media else None
    latest_result = recording_media(media, latest["file"] if latest else None)
    latest_result["file"] = latest["file"] if latest else None
    images = media["images"].get(sci, {}) if media else {}
    public_images = {}
    for pose, entry in images.items():
        # A publisher's private manifest may contain paths or other evidence.
        # Only the browser's media contract belongs in public snapshots.
        image = {"available": True, "url": entry["url"],
                 "sha256": entry["sha256"], "bytes": entry["bytes"]}
        if entry.get("source") in ("illustrations", "cutouts", "generated"):
            image["source"] = entry["source"]
        public_images[pose] = image
    return {"latest_media": latest_result,
            "media_images": public_images}


def collect_snapshot(db, instant, timezone, generation, media=None, fix_last_hour=False):
    """Caller holds a read transaction. Bind a single clock to all queries.

    Queries intentionally match the existing PHP behavior, including future
    timestamps in recent windows and ambiguous GROUP BY common-name selection.
    """
    local = instant.astimezone(timezone)
    local_sql = local.strftime("%Y-%m-%d %H:%M:%S")
    as_of = local.isoformat(timespec="seconds")
    published_at = iso_utc(instant)
    bound = {"now": local_sql}
    metadata = {"schema_version": 2, "generation": generation,
                "published_at": published_at, "as_of": as_of,
                "timezone": timezone.key, "compatibility": {
                    "last_hour": "rolling" if fix_last_hour else "php_today_only"},
                "media_manifest_generation": media.get("generation") if media else None}
    results = {}

    def store(name, response):
        response["snapshot"] = dict(metadata)
        results[name] = response

    totals = _one(db, "SELECT COUNT(*) AS detections, COUNT(DISTINCT Sci_Name) AS species FROM detections")
    today = _one(db, "SELECT COUNT(*) AS detections, COUNT(DISTINCT Sci_Name) AS species FROM detections WHERE Date = DATE(:now)", bound)
    last_condition = ("Date||' '||Time >= datetime(:now, '-1 hour') AND Date||' '||Time <= :now"
                      if fix_last_hour else "Date = DATE(:now) AND Time >= TIME(:now, '-1 hour')")
    last_hour = _one(db, "SELECT COUNT(*) AS detections FROM detections WHERE " + last_condition, bound)
    week = _one(db, "SELECT COUNT(*) AS detections, COUNT(DISTINCT Sci_Name) AS species FROM detections WHERE Date >= DATE(:now, '-7 day')", bound)
    started = _one(db, "SELECT MIN(Date) AS d FROM detections")["d"]
    store("stats", {"totals": totals, "today": today, "last_hour": last_hour,
                    "week": week, "started": started, "as_of": as_of})

    life = _rows(db, "SELECT Sci_Name AS sci, Com_Name AS com, MIN(Date||' '||Time) AS first_seen, MAX(Date||' '||Time) AS last_seen, COUNT(*) AS n, MAX(Confidence) AS best_conf FROM detections GROUP BY Sci_Name ORDER BY first_seen ASC")
    for row in life:
        row.update(species_media(media, row["sci"]))
    store("lifelist", {"species": life, "as_of": as_of})

    daily = _rows(db, "SELECT Date AS date, COUNT(*) AS detections, COUNT(DISTINCT Sci_Name) AS species FROM detections WHERE Date >= DATE(:now, '-29 day') GROUP BY Date ORDER BY Date", bound)
    by_hour = _rows(db, "SELECT CAST(strftime('%H', Time) AS INT) AS hour, COUNT(*) AS detections FROM detections WHERE Date >= DATE(:now, '-30 day') GROUP BY hour ORDER BY hour", bound)
    store("timeseries/30", {"days": 30, "daily": daily, "by_hour": by_hour, "as_of": as_of})

    first = _rows(db, "SELECT Sci_Name AS sci, Com_Name AS com, MIN(Date||' '||Time) AS first_seen, COUNT(*) AS total FROM detections GROUP BY Sci_Name ORDER BY first_seen DESC LIMIT 10")
    store("firstseen/10", {"species": first, "as_of": as_of})

    for hours in WINDOWS:
        recent_bound = {**bound, "hours": hours}
        species = _rows(db, "SELECT Sci_Name AS sci, Com_Name AS com, COUNT(*) AS n, MAX(Confidence) AS best_conf, MAX(Date||' '||Time) AS last_seen FROM detections WHERE (julianday(:now) - julianday(Date||' '||Time)) * 24 <= :hours GROUP BY Sci_Name ORDER BY last_seen DESC", recent_bound)
        # Batch one best-row scan per window instead of one query per species.
        # Confidence ties have unspecified order in the PHP baseline too.
        best_by_sci = {}
        for candidate in _iter_rows(db, "SELECT Sci_Name AS sci, File_Name AS file, Date AS d, Time AS t, Confidence AS conf FROM detections WHERE (julianday(:now) - julianday(Date||' '||Time)) * 24 <= :hours ORDER BY Confidence DESC", recent_bound):
            best_by_sci.setdefault(candidate["sci"], candidate)
        for row in species:
            # Sci_Name=NULL did not match PHP's Sci_Name = :sn predicate.
            best = best_by_sci.get(row["sci"]) if row["sci"] is not None else None
            row["top_file"] = best["file"] if best else None
            row["top_at"] = best["d"] + " " + best["t"] if best else None
            row["top_media"] = recording_media(media, row["top_file"])
            row.update(species_media(media, row["sci"]))
        store("recent/" + str(hours), {"hours": hours, "species": species, "as_of": as_of})

    sci_paths = {}
    detail_rows = {row["sci"]: [] for row in life if row["sci"] is not None}
    for row in _iter_rows(db, "SELECT Sci_Name AS sci, Date AS d, Time AS t, File_Name AS file, Confidence AS conf FROM detections WHERE Sci_Name IS NOT NULL ORDER BY Date DESC, Time DESC"):
        sci = row.pop("sci")
        if len(detail_rows[sci]) < 500:
            row["media"] = recording_media(media, row["file"])
            detail_rows[sci].append(row)
    # The lifelist's grouped MAX(Confidence) uses the same SQLite bare-column
    # common-name selection as each PHP per-species aggregate summary.
    summaries = {row["sci"]: {"com": row["com"], "total": row["n"],
                             "first_seen": row["first_seen"], "last_seen": row["last_seen"],
                             "best_conf": row["best_conf"]} for row in life if row["sci"] is not None}
    for sci in sorted(detail_rows):
        token = hashlib.sha256(sci.encode("utf-8")).hexdigest()
        path = "species/" + token
        sci_paths[sci] = path
        store(path, {"sci": sci, "summary": summaries[sci], "detections": detail_rows[sci],
                     **species_media(media, sci)})
    return results, sci_paths, metadata


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _bytes_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _json_write(path, value):
    _bytes_write(path, _json_bytes(value))


def stable_payload(response):
    """Clock/generation envelopes belong in the small index, not every object."""
    return {key: value for key, value in response.items() if key not in ("snapshot", "as_of")}


def hydrate_payload(response, index, action):
    """Reference adapter for restoring the reviewed facade's additive metadata.

    The future frontend should copy the payload and inject the selected index's
    metadata once it has pinned and validated that index. No network access here.
    """
    hydrated = dict(response)
    if action != "species":
        hydrated["as_of"] = index["as_of"]
    hydrated["snapshot"] = {key: index[key] for key in (
        "schema_version", "generation", "published_at", "as_of", "timezone",
        "compatibility", "media_manifest_generation")}
    return hydrated


def _store_object(objects, payload):
    raw = _json_bytes(stable_payload(payload))
    digest = hashlib.sha256(raw).hexdigest()
    destination = objects / (digest + ".json")
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_file():
            raise ValueError("existing content object is not a regular file")
        existing = destination.read_bytes()
        if existing != raw or hashlib.sha256(existing).hexdigest() != digest:
            raise ValueError("existing content object failed bytes/hash verification")
        return "objects/" + digest + ".json", {"sha256": digest, "bytes": len(raw)}, False
    temporary = objects / (".pending-" + uuid.uuid4().hex + ".json")
    try:
        _bytes_write(temporary, raw)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "objects/" + digest + ".json", {"sha256": digest, "bytes": len(raw)}, True


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_local(output, generation, responses, sci_paths, metadata, *,
                  max_output_bytes=DEFAULT_QUOTA, min_free_disk_bytes=DEFAULT_RESERVE,
                  max_candidate_bytes=32 * 1024 * 1024):
    """Nondeleting quota preflight under a shared local publisher lock."""
    with output_lock(output):
        return _publish_local_unlocked(output, generation, responses, sci_paths, metadata,
                                       max_output_bytes, min_free_disk_bytes, max_candidate_bytes)


def _publish_local_unlocked(output, generation, responses, sci_paths, metadata,
                            max_output_bytes, min_free_disk_bytes, max_candidate_bytes):
    output = Path(output)
    output_usage(output)  # Reject links/special files before reading reusable objects.
    objects = output / "objects"
    objects.mkdir(exist_ok=True)
    generations = output / "generations"
    generations.mkdir(exist_ok=True)
    destination = generations / generation
    if destination.exists() or destination.is_symlink():
        raise ValueError("generation already exists")
    if not isinstance(max_candidate_bytes, int) or isinstance(max_candidate_bytes, bool) or max_candidate_bytes < 1:
        raise ValueError("invalid candidate serialization limit")
    paths, object_metadata, prepared = {}, {}, {}
    new_lengths = []
    serialized_bytes = 0
    for key, response in responses.items():
        raw = _json_bytes(stable_payload(response))
        digest = hashlib.sha256(raw).hexdigest()
        path = "objects/" + digest + ".json"
        paths[key] = path
        object_metadata[path] = {"sha256": digest, "bytes": len(raw)}
        if path in prepared:
            continue
        serialized_bytes += len(raw)
        if serialized_bytes > max_candidate_bytes:
            raise ValueError("candidate serialization limit exceeded; previous index retained")
        existing = objects / (digest + ".json")
        if existing.exists() or existing.is_symlink():
            if existing.is_symlink() or not existing.is_file():
                raise ValueError("existing content object is not a regular file")
            if existing.stat().st_size != len(raw):
                raise ValueError("existing content object failed bytes/hash verification")
            content = existing.read_bytes()
            if content != raw or hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("existing content object failed bytes/hash verification")
            prepared[path] = (raw, False)
        else:
            prepared[path] = (raw, True)
            new_lengths.append(len(raw))
    route = lambda key: paths[key]
    manifest = {**metadata, "routes": {
        "stats": route("stats"), "lifelist": route("lifelist"),
        "timeseries": {"30": route("timeseries/30")},
        "firstseen": {"10": route("firstseen/10")},
        "recent": {str(hours): route("recent/" + str(hours)) for hours in WINDOWS},
        "species": {sci: route(key) for sci, key in sci_paths.items()}},
        "objects": object_metadata, "snapshot_count": len(responses),
        "publication": {"data_objects": len(object_metadata),
                        "new_data_objects": len(new_lengths), "new_data_bytes": sum(new_lengths)}}
    manifest_raw = _json_bytes(manifest)
    if serialized_bytes + 2 * len(manifest_raw) > max_candidate_bytes:
        raise ValueError("candidate serialization limit exceeded; previous index retained")
    preflight(output, new_lengths, len(manifest_raw), max_output_bytes, min_free_disk_bytes)
    # No candidate bytes are written before the complete preflight succeeds.
    temporary = Path(tempfile.mkdtemp(prefix=".pending-", dir=generations))
    pointer_tmp = None
    try:
        for path, (raw, new) in prepared.items():
            if not new:
                continue
            object_tmp = objects / (".pending-" + uuid.uuid4().hex + ".json")
            try:
                _bytes_write(object_tmp, raw)
                os.replace(object_tmp, output / path)
            finally:
                if object_tmp.exists():
                    object_tmp.unlink()
        _sync_directory(objects)
        _bytes_write(temporary / "manifest.json", manifest_raw)
        _sync_directory(temporary)
        temporary.rename(destination)
        _sync_directory(generations)
        pointer_tmp = output / (".snapshot-index-" + uuid.uuid4().hex + ".json")
        _bytes_write(pointer_tmp, manifest_raw)
        os.replace(pointer_tmp, output / "snapshot-index.json")
        _sync_directory(output)
        return manifest
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
        if pointer_tmp and pointer_tmp.exists():
            pointer_tmp.unlink()


def export(db_path, output, *, instant=None, timezone="America/New_York", generation=None,
           media_manifest=None, media_origin=None, fix_last_hour=False,
           max_output_bytes=DEFAULT_QUOTA, min_free_disk_bytes=DEFAULT_RESERVE,
           max_candidate_bytes=32 * 1024 * 1024):
    db_path = Path(db_path).resolve(strict=True)
    output = Path(output).resolve()
    if output == db_path.parent or output == db_path or output in db_path.parents:
        raise ValueError("output directory must be separate from the database directory")
    generation = generation or uuid.uuid4().hex
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", generation):
        raise ValueError("generation must be a safe opaque identifier")
    instant = instant or dt.datetime.now(dt.timezone.utc)
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("as-of instant must include a timezone offset")
    zone = ZoneInfo(timezone)
    media = load_media(media_manifest, media_origin)
    # Do not use immutable=1: the live Pi database may be in WAL mode.
    db = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=2.0)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(detections)")}
        if not REQUIRED_COLUMNS <= columns:
            raise ValueError("database detections schema does not match the reviewed PHP baseline")
        responses, paths, metadata = collect_snapshot(db, instant, zone, generation, media, fix_last_hour)
        db.rollback()
    finally:
        db.close()
    return publish_local(output, generation, responses, paths, metadata,
                         max_output_bytes=max_output_bytes, min_free_disk_bytes=min_free_disk_bytes,
                         max_candidate_bytes=max_candidate_bytes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timezone", default="America/New_York", help="must match the BirdNET-Pi recording timezone")
    parser.add_argument("--as-of", help="ISO 8601 instant with offset, for reproducible staging")
    parser.add_argument("--generation")
    parser.add_argument("--max-output-bytes", type=int, default=DEFAULT_QUOTA)
    parser.add_argument("--min-free-disk-bytes", type=int, default=DEFAULT_RESERVE)
    parser.add_argument("--max-candidate-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--media-manifest", type=Path)
    parser.add_argument("--media-origin")
    parser.add_argument("--fix-last-hour", action="store_true", help="opt in to a true rolling hour instead of existing PHP midnight behavior")
    args = parser.parse_args()
    try:
        instant = dt.datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else None
        manifest = export(args.db, args.output, instant=instant, timezone=args.timezone,
                          generation=args.generation, media_manifest=args.media_manifest,
                          media_origin=args.media_origin, fix_last_hour=args.fix_last_hour,
                          max_output_bytes=args.max_output_bytes, min_free_disk_bytes=args.min_free_disk_bytes,
                          max_candidate_bytes=args.max_candidate_bytes)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, "snapshot export failed: " + str(error) + "\n")
    print(json.dumps({"generation": manifest["generation"], "snapshot_count": manifest["snapshot_count"],
                      "published_at": manifest["published_at"],
                      "new_data_objects": manifest["publication"]["new_data_objects"],
                      "new_data_bytes": manifest["publication"]["new_data_bytes"]}, separators=(",", ":")))


if __name__ == "__main__":
    main()

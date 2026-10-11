"""Bounded recorder-local daily occurrences, captured in a read transaction.

This module opens no recording files, makes no network calls and does not infer
recorder uptime from detections. The compact annual aggregate is independent of
audio availability and the existing species detail view's latest-500-row window.
"""
import calendar
import datetime as dt
import re

MAX_ROWS = 200000
MAX_SPECIES = 500
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TIME = re.compile(r"[0-9]{2}:[0-9]{2}:[0-9]{2}")


def annual_window(end_date):
    """Inclusive trailing 12 civil months, clamping the prior Feb 29 to Feb 28."""
    if type(end_date) is not dt.date or end_date.year <= 1:
        raise ValueError("annual window requires a supported civil date")
    prior = dt.date(end_date.year - 1, end_date.month,
                    min(end_date.day, calendar.monthrange(end_date.year - 1, end_date.month)[1]))
    return prior + dt.timedelta(days=1), end_date


def _limit(value, maximum, label):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError("invalid bounded seasonality " + label)
    return value


def collect_seasonality(db, instant, timezone, *, guard=None,
                        max_rows=MAX_ROWS, max_species=MAX_SPECIES):
    """Count every valid observation in one bounded scan, not latest 500 rows.

    ``db`` must be inside the caller's read transaction. Source Date/Time have
    recorder-local wall-clock precision: a repeated DST hour stays ambiguous.
    All occurrence rows count, including multiple detections of the same clip.
    Filenames, confidence and media receipts are neither read nor required.
    """
    _limit(max_rows, MAX_ROWS, "row limit")
    _limit(max_species, MAX_SPECIES, "species limit")
    if not db.in_transaction:
        raise ValueError("seasonality requires the caller's read transaction")
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("seasonality as-of requires an offset")
    local = instant.astimezone(timezone)
    start, end = annual_window(local.date())
    species, counts = {}, {}
    invalid = 0
    if guard is not None:
        guard.check()
    # CASE prevents corrupt oversized names from allocating unbounded Python
    # strings. Lexical bounds match canonical ISO dates; strict parsing below
    # rejects invalid dates/times rather than letting SQLite normalize them.
    cursor = db.execute("""SELECT
        CASE WHEN typeof(Date)='text' AND length(Date)=10 THEN Date ELSE NULL END AS d,
        CASE WHEN typeof(Time)='text' AND length(Time)=8 THEN Time ELSE NULL END AS t,
        CASE WHEN typeof(Sci_Name)='text' AND length(Sci_Name) BETWEEN 1 AND 200
             THEN Sci_Name ELSE NULL END AS sci,
        CASE WHEN typeof(Com_Name)='text' AND length(Com_Name) BETWEEN 1 AND 200
             THEN Com_Name ELSE NULL END AS com
        FROM detections WHERE Date >= :start AND Date <= :end
        LIMIT :limit""", {"start": start.isoformat(), "end": end.isoformat(), "limit": max_rows + 1})
    for position, raw in enumerate(cursor):
        if position >= max_rows:
            raise ValueError("complete seasonality source exceeds row limit")
        if guard is not None and position % 256 == 0:
            guard.check()
        row = dict(raw)
        date, time, sci = row["d"], row["t"], row["sci"]
        try:
            if (not isinstance(date, str) or not DATE.fullmatch(date) or
                    not isinstance(time, str) or not TIME.fullmatch(time) or not sci):
                raise ValueError("invalid source timestamp/species")
            civil = dt.date.fromisoformat(date)
            clock = dt.time.fromisoformat(time)
            wall = dt.datetime.combine(civil, clock)
            if not any(wall.replace(tzinfo=timezone, fold=fold).astimezone(dt.timezone.utc)
                       .astimezone(timezone).replace(tzinfo=None) == wall for fold in (0, 1)):
                raise ValueError("nonexistent recorder-local source time")
            if (not start <= civil <= end or
                    civil == end and clock > local.time().replace(tzinfo=None, microsecond=0)):
                raise ValueError("future source observation")
        except (TypeError, ValueError):
            invalid += 1
            continue
        if sci not in species:
            if len(species) >= max_species:
                raise ValueError("complete seasonality source exceeds species limit")
            species[sci] = row["com"] or sci
        elif row["com"]:
            species[sci] = min(species[sci], row["com"])
        counts.setdefault(sci, {}).setdefault(date, 0)
        counts[sci][date] += 1
    if guard is not None:
        guard.check()
    public_species = []
    observed_dates = set()
    for sci in sorted(species):
        sparse = [[(dt.date.fromisoformat(date) - start).days, count]
                  for date, count in sorted(counts[sci].items())]
        observed_dates.update(counts[sci])
        public_species.append({"sci": sci, "com": species[sci],
                               "total": sum(counts[sci].values()),
                               "detected_days": len(sparse), "counts": sparse})
    return {"format": "seasonality-v1", "start_date": start.isoformat(), "end_date": end.isoformat(),
        "coverage": {"basis": "detections_do_not_establish_uptime", "default": "unknown",
            "partial_dates": [end.isoformat()],
            "first_observation_date": min(observed_dates) if observed_dates else None},
        "species": public_species, "totals": {"detections": sum(row["total"] for row in public_species)},
        "excluded_invalid_rows": invalid}

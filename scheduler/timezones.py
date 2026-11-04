"""IANA timezone conversion with deterministic DST gap/fold policies.

Policy: ambiguous wall time chooses the first occurrence (fold=0). A nonexistent
wall time advances to the first valid local instant after the gap. UTC is the
only runtime comparison domain.
"""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def _valid_local_candidates(naive: datetime, zone: ZoneInfo):
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        utc = candidate.astimezone(timezone.utc)
        roundtrip = utc.astimezone(zone).replace(tzinfo=None)
        if roundtrip == naive:
            candidates.append((utc, fold))
    # In ordinary time fold 0 and fold 1 map to the same UTC instant.
    unique = {}
    for utc, fold in candidates:
        unique.setdefault(utc, fold)
    return sorted(unique.items(), key=lambda item: item[0])


def local_datetime_to_utc(local_date: date, local_time: time, tz_name: str) -> datetime:
    """Convert a local wall-clock date/time into aware UTC.

    DST fold: first occurrence. DST gap: first valid local instant after the gap.
    """
    if local_time.tzinfo is not None:
        raise ValueError("local_time must be naive")
    zone = ZoneInfo(tz_name)
    naive = datetime.combine(local_date, local_time.replace(tzinfo=None))
    candidates = _valid_local_candidates(naive, zone)
    if candidates:
        return candidates[0][0]

    # Search forward minute-by-minute (IANA DST transitions are minute aligned in
    # modern tzdb; this also handles 30-minute and non-hour jumps).
    probe = naive
    for _ in range(48 * 60):
        probe += timedelta(minutes=1)
        candidates = _valid_local_candidates(probe, zone)
        if candidates:
            return candidates[0][0]
    raise ValueError(f"Could not resolve nonexistent local time {naive} in {tz_name}")


def utc_to_local(value: datetime, tz_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError("UTC datetime must be timezone-aware")
    return value.astimezone(ZoneInfo(tz_name))

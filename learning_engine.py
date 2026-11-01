"""Persistent decision records and conservative audience-feedback learning helpers."""
from datetime import datetime, timezone

VERSION = "learning-v1"
MAX_CANDIDATE_DECISIONS = 2000
SNAPSHOT_THRESHOLDS = (("24h", 24), ("72h", 72), ("7d", 168))


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def record_candidate_decisions(state, episode_id, channel, candidates, selected_ranges, recorded_at=None):
    """Append compact records for every model-reviewed candidate, not only selected clips."""
    timestamp = _now(recorded_at).isoformat(timespec="seconds")
    selected = {(round(float(a), 1), round(float(b), 1)) for a, b in selected_ranges}
    rows = state.setdefault("candidate_decisions", [])
    for rank, candidate in enumerate(sorted(candidates, key=lambda c: -float(c.get("final", 0))), 1):
        start, end = float(candidate.get("start", 0)), float(candidate.get("end", 0))
        rows.append({
            "episode_id": str(episode_id), "channel": str(channel),
            "start": round(start, 2), "end": round(end, 2),
            "rank": rank, "selected": (round(start, 1), round(end, 1)) in selected,
            "recorded_at": timestamp, "model_version": VERSION,
            "features": candidate.get("learning_features") or {},
        })
    del rows[:-MAX_CANDIDATE_DECISIONS]
    return len(rows)


def add_feedback(state, clip_id, label, reasons=None, note="", recorded_at=None):
    allowed = {"good", "weak", "needs_context", "bad_cut", "promotional",
               "visual_issue", "caption_issue", "other"}
    if label not in allowed:
        raise ValueError("unsupported feedback label")
    clip = next((item for item in state.get("clips", []) if item.get("id") == clip_id), None)
    if clip is None:
        raise ValueError(f"clip {clip_id} was not found")
    feedback = clip.setdefault("human_feedback", [])
    feedback.append({
        "label": label, "reasons": [str(x)[:80] for x in (reasons or [])[:8]],
        "note": str(note or "")[:500], "recorded_at": _now(recorded_at).isoformat(timespec="seconds"),
    })
    clip["human_label"] = label
    return clip


def add_metric_snapshot(clip, metrics, posted_at, sampled_at=None):
    """Store one first-available snapshot for each maturity band; never fabricate missing data."""
    if not metrics:
        return []
    posted = _now(posted_at)
    sampled = _now(sampled_at)
    age_hours = max(0.0, (sampled - posted).total_seconds() / 3600)
    snapshots = clip.setdefault("metric_snapshots", [])
    known = {s.get("band") for s in snapshots}
    added = []
    for band, threshold in SNAPSHOT_THRESHOLDS:
        if age_hours >= threshold and band not in known:
            snapshot = {
                "band": band, "age_hours": round(age_hours, 1),
                "sampled_at": sampled.isoformat(timespec="seconds"),
                "metrics": {k: v for k, v in metrics.items()
                            if isinstance(v, (int, float)) and not isinstance(v, bool)},
            }
            snapshots.append(snapshot)
            added.append(snapshot)
    return added


def learning_counts(state):
    clips = state.get("clips", [])
    return {
        "candidate_decisions": len(state.get("candidate_decisions", [])),
        "human_feedback": sum(len(c.get("human_feedback", [])) for c in clips),
        "published": sum(c.get("status") == "posted" for c in clips),
        "with_performance": sum(bool(c.get("metrics")) for c in clips),
    }

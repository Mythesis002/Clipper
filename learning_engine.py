"""Persistent decision records and conservative audience-feedback learning helpers."""
from datetime import datetime, timezone
import math

VERSION = "learning-v1"
MAX_CANDIDATE_DECISIONS = 600
SNAPSHOT_THRESHOLDS = (("24h", 24, 72), ("72h", 72, 168), ("7d", 168, 240))
NUMERIC_FEATURES = (
    "heat", "hook", "density", "editorial_score", "standalone_heuristic",
    "payoff_heuristic", "context_dependency", "llm", "vid",
)
CATEGORICAL_FEATURES = {
    "hook_type": ("question", "number_or_stat", "personal_story", "contrast", "strong_statement", "unknown"),
    "duration_bucket": ("15-19s", "20-29s", "30-39s", "40s+"),
}


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
    for band, threshold, upper_bound in SNAPSHOT_THRESHOLDS:
        if threshold <= age_hours < upper_bound and band not in known:
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



def _feature_vector(features):
    features = features or {}
    vector = {}
    for key in NUMERIC_FEATURES:
        try:
            value = float(features.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        vector[key] = value if math.isfinite(value) else 0.0
    for key, values in CATEGORICAL_FEATURES.items():
        selected = str(features.get(key) or "")
        for value in values:
            vector[f"{key}={value}"] = 1.0 if selected == value else 0.0
    return vector


def _sigmoid(value):
    value = max(-30.0, min(30.0, float(value)))
    return 1.0 / (1.0 + math.exp(-value))


def _labelled_examples(state):
    examples = []
    positive_labels = {"good"}
    negative_labels = {"weak", "needs_context", "bad_cut", "promotional"}
    for clip in state.get("clips", []):
        label = clip.get("human_label")
        target = None
        if label in positive_labels:
            target = 1.0
        elif label in negative_labels:
            target = 0.0
        elif clip.get("status") == "posted" and clip.get("performance_score") is not None:
            try:
                score = float(clip["performance_score"])
            except (TypeError, ValueError):
                score = 0.5
            if score >= 0.75:
                target = 1.0
            elif score <= 0.25:
                target = 0.0
        if target is not None:
            examples.append((_feature_vector(clip.get("features") or {}), target))
    return examples


def train_editorial_ranker(state, min_samples=20, min_per_class=6,
                           epochs=300, learning_rate=0.08, l2=0.04):
    """Fit a small regularised logistic ranker only when both classes have real evidence.

    Training labels come from explicit human feedback or clearly strong/weak mature outcomes.
    Merely not selecting a candidate is never treated as a negative example.
    """
    examples = _labelled_examples(state or {})
    positives = sum(target for _, target in examples)
    negatives = len(examples) - positives
    if len(examples) < min_samples or positives < min_per_class or negatives < min_per_class:
        return None

    columns = list(_feature_vector({}).keys())
    means = {}
    scales = {}
    for column in columns:
        values = [row[column] for row, _ in examples]
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / max(len(values), 1)
        means[column] = mean
        scales[column] = max(math.sqrt(variance), 0.25)

    weights = {column: 0.0 for column in columns}
    intercept = 0.0
    for _ in range(max(1, int(epochs))):
        gradients = {column: 0.0 for column in columns}
        bias_gradient = 0.0
        for row, target in examples:
            z = intercept + sum(
                weights[column] * ((row[column] - means[column]) / scales[column])
                for column in columns
            )
            error = _sigmoid(z) - target
            bias_gradient += error
            for column in columns:
                standard = (row[column] - means[column]) / scales[column]
                gradients[column] += error * standard
        count = float(len(examples))
        intercept -= learning_rate * bias_gradient / count
        for column in columns:
            gradient = gradients[column] / count + l2 * weights[column]
            weights[column] -= learning_rate * gradient

    return {
        "version": "editorial-logistic-v1",
        "samples": len(examples), "positive_samples": int(positives),
        "negative_samples": int(negatives),
        "columns": columns, "means": means, "scales": scales,
        "weights": weights, "intercept": intercept,
    }


def predict_editorial_score(candidate, model):
    """Return a calibrated 0..1 preference estimate, or None without a valid model."""
    if not isinstance(model, dict) or model.get("version") != "editorial-logistic-v1":
        return None
    row = _feature_vector(candidate)
    try:
        z = float(model["intercept"])
        for column in model["columns"]:
            standard = (row.get(column, 0.0) - model["means"][column]) / model["scales"][column]
            z += model["weights"][column] * standard
        return round(_sigmoid(z), 4)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None

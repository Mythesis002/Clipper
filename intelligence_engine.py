"""Episode-level and semantic editorial intelligence helpers.

These functions are deterministic. Gemini supplies structured interpretation; this module
validates and converts it into timestamped, bounded candidate windows.
"""
from bisect import bisect_left, bisect_right
import re

VERSION = "editorial-v1"


def _safe_float(value, default=None):
    try:
        result = float(value)
        return result if result >= 0 else default
    except (TypeError, ValueError):
        return default


def timed_transcript(words, bucket_seconds=20, max_chars=90000):
    """Compress word timestamps into readable, timestamped transcript blocks."""
    buckets = {}
    for stamp, word in words or []:
        try:
            second = max(0, int(float(stamp) // bucket_seconds) * bucket_seconds)
        except (TypeError, ValueError):
            continue
        buckets.setdefault(second, []).append(str(word))
    lines = []
    for start in sorted(buckets):
        mm, ss = divmod(start, 60)
        text = " ".join(buckets[start]).strip()
        if text:
            lines.append(f"[{mm:02d}:{ss:02d}] {text}")
    output = "\n".join(lines)
    if len(output) <= max_chars:
        return output
    # Preserve episode coverage instead of silently keeping only the beginning.
    stride = max(2, (len(output) + max_chars - 1) // max_chars)
    selected = lines[::stride]
    return "\n".join(selected)[:max_chars]


def episode_understanding_prompt(title, duration, transcript):
    return (
        "You are an expert podcast story editor. Analyse this timestamped transcript and return "
        "only valid JSON. Do not invent events, names, facts or timestamps. All timestamps must "
        "refer to the supplied transcript. Identify the episode's main themes and the most useful "
        "editorial moments: complete personal stories, conflicts, turning points, explanations, "
        "strong opinions, questions with satisfying answers, practical insights and memorable "
        "reactions. Mark promotional intent when a passage is a sponsor read, paid integration, "
        "affiliate pitch, direct product/service sales pitch or creator self-promotion. A casual "
        "mention of a product is not automatically an advertisement. Provide evidence from the "
        "transcript and confidence from 0 to 1. Return at most 30 moments. Use this schema: "
        '{"summary":"...", "topics":[{"name":"...", "start":0, "end":0}], '
        '"moments":[{"start":0,"end":0,"kind":"story|insight|explanation|opinion|debate|question_answer|'
        'reaction|promotion|other","topic":"...","summary":"...","has_setup":true,'
        '"has_payoff":true,"promotion_type":"none|sponsor_read|affiliate|product_pitch|self_promotion|'
        'unclear","evidence":"short exact phrase from transcript","confidence":0.0}]}. '
        "Episode title: " + str(title) + "\nDuration seconds: " + str(int(duration)) +
        "\nTimestamped transcript:\n" + transcript
    )


def validate_episode_map(data, duration):
    """Drop malformed/out-of-range moments and normalise Gemini's episode-map response."""
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return {"summary": "", "topics": [], "moments": []}
    topics = []
    for item in data.get("topics", []) if isinstance(data.get("topics"), list) else []:
        if not isinstance(item, dict):
            continue
        start, end = _safe_float(item.get("start")), _safe_float(item.get("end"))
        if start is None or end is None or end <= start or start >= duration:
            continue
        topics.append({"name": str(item.get("name") or "Unlabelled topic")[:100],
                       "start": min(start, duration), "end": min(end, duration)})
    moments = []
    allowed = {"story", "insight", "explanation", "opinion", "debate",
               "question_answer", "reaction", "promotion", "other"}
    promotion_types = {"none", "sponsor_read", "affiliate", "product_pitch",
                       "self_promotion", "unclear"}
    for item in data.get("moments", []) if isinstance(data.get("moments"), list) else []:
        if not isinstance(item, dict):
            continue
        start, end = _safe_float(item.get("start")), _safe_float(item.get("end"))
        if start is None or end is None or end <= start or start >= duration:
            continue
        kind = str(item.get("kind") or "other").lower()
        promotion_type = str(item.get("promotion_type") or "none").lower()
        confidence = _safe_float(item.get("confidence"), 0.0)
        moments.append({
            "start": min(start, duration), "end": min(end, duration),
            "kind": kind if kind in allowed else "other",
            "topic": str(item.get("topic") or "")[:100],
            "summary": str(item.get("summary") or "")[:320],
            "has_setup": bool(item.get("has_setup")),
            "has_payoff": bool(item.get("has_payoff")),
            "promotion_type": promotion_type if promotion_type in promotion_types else "unclear",
            "evidence": str(item.get("evidence") or "")[:240],
            "confidence": min(max(confidence or 0.0, 0.0), 1.0),
        })
    return {"summary": str(data.get("summary") or "")[:1200],
            "topics": sorted(topics, key=lambda x: x["start"]),
            "moments": sorted(moments, key=lambda x: x["start"])[:30]}


def semantic_candidate_windows(episode_map, words, min_seconds, max_seconds, duration):
    """Create additional candidates from model-identified story/idea spans, aligned to words."""
    if not words:
        return []
    times = [float(item[0]) for item in words]
    candidates = []
    seen = set()
    for moment in episode_map.get("moments", []):
        if moment.get("kind") == "promotion":
            continue
        start = max(0.0, float(moment["start"]))
        end = min(float(duration), float(moment["end"]))
        if end - start < min_seconds:
            center = (start + end) / 2
            start = max(0.0, center - min_seconds / 2)
            end = min(float(duration), start + min_seconds)
            start = max(0.0, end - min_seconds)
        if end - start > max_seconds:
            # Keep the end of the identified idea when it contains its payoff.
            start = max(start, end - max_seconds)
        i0 = bisect_left(times, start)
        i1 = bisect_left(times, end)
        if i1 <= i0:
            continue
        aligned_start = times[i0]
        aligned_end = times[min(i1, len(times)) - 1] + 0.25
        if aligned_end - aligned_start < min_seconds - 1:
            continue
        key = (round(aligned_start), round(aligned_end))
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "start": aligned_start, "end": min(aligned_end, duration),
            "text": " ".join(str(w) for _, w in words[i0:i1]),
            "semantic_kind": moment.get("kind", "other"),
            "semantic_topic": moment.get("topic", ""),
            "semantic_summary": moment.get("summary", ""),
            "semantic_confidence": moment.get("confidence", 0.0),
            "promotion_type": moment.get("promotion_type", "none"),
            "promotion_confidence": (moment.get("confidence", 0.0)
                                     if moment.get("promotion_type", "none") != "none" else 0.0),
            "semantic_candidate": True,
        })
    return candidates


def attach_semantic_context(candidates, episode_map):
    moments = episode_map.get("moments", [])
    for candidate in candidates:
        midpoint = (float(candidate["start"]) + float(candidate["end"])) / 2
        match = next((m for m in moments if m["start"] <= midpoint <= m["end"]), None)
        if match:
            candidate.setdefault("semantic_kind", match["kind"])
            candidate.setdefault("semantic_topic", match["topic"])
            candidate.setdefault("semantic_summary", match["summary"])
            candidate.setdefault("semantic_confidence", match["confidence"])
            candidate.setdefault("promotion_type", match["promotion_type"])
            if match["promotion_type"] != "none":
                candidate.setdefault("promotion_confidence", match["confidence"])
    return candidates


def editorial_gate(candidate, promotion_policy="manual_review", threshold=0.72):
    """Return a transparent gate decision; uncertain promotion signals are never auto-excluded."""
    kind = candidate.get("content_type") or candidate.get("semantic_kind") or "other"
    promo_type = candidate.get("promotion_type") or "none"
    confidence = max(float(candidate.get("promotion_confidence") or 0.0),
                     float(candidate.get("semantic_confidence") or 0.0)
                     if promo_type != "none" else 0.0)
    explicit = promo_type not in ("none", "", "unclear")
    is_promo = explicit and confidence >= threshold
    if is_promo and promotion_policy == "exclude":
        return {"eligible": False, "review_required": False, "reason": "promotional_content"}
    if is_promo and promotion_policy == "manual_review":
        return {"eligible": True, "review_required": True, "reason": "promotional_content"}
    if is_promo and promotion_policy == "allow":
        return {"eligible": True, "review_required": False, "reason": "promotional_content_allowed"}
    if kind in ("other", "unclear") and candidate.get("classification_confidence", 0) < 0.35:
        return {"eligible": True, "review_required": True, "reason": "low_classification_confidence"}
    return {"eligible": True, "review_required": False, "reason": ""}


def candidate_feature_record(candidate):
    """Compact, JSON-safe feature snapshot for later evaluation and learning."""
    keys = ("start", "end", "heat", "hook", "density", "pre", "final",
            "editorial_score", "standalone_heuristic", "payoff_heuristic",
            "context_dependency", "llm", "vid", "content_type", "semantic_kind",
            "semantic_topic", "semantic_confidence", "promotion_type",
            "promotion_confidence", "classification_confidence", "hook_type",
            "title", "reject", "editorial_review_required", "editorial_gate_reason")
    out = {}
    for key in keys:
        value = candidate.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    out["transcript_excerpt"] = str(candidate.get("text") or "")[:700]
    return out

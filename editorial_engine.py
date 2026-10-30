"""Editorial intelligence and performance-learning primitives for podcast clips.

Pure functions live here so the scoring and analytics behaviour can be tested without
YouTube, Gemini, Instagram, model weights, or media downloads.
"""
from collections import defaultdict
import re


_CONTEXT_LEADS = {
    "he", "she", "it", "they", "this", "that", "these", "those", "there",
    "which", "and", "but", "so", "because", "also", "however", "then",
    "तो", "और", "लेकिन", "क्योंकि", "इसलिए", "वह", "उसने", "ये", "यह",
}
_WEAK_ENDINGS = {
    "and", "but", "because", "so", "which", "that", "to", "for", "with",
    "और", "लेकिन", "क्योंकि", "इसलिए", "तो",
}
_PAYOFF_CUES = (
    "that's why", "that is why", "which means", "the result", "as a result",
    "i learned", "i realised", "i realized", "the lesson", "so now",
    "मतलब", "इसलिए", "नतीजा", "सीखा",
)
_STORY_CUES = ("when i", "i remember", "one day", "my father", "my mother",
               "मेरे साथ", "एक बार", "जब मैं")
_QUESTION_START = {"why", "how", "what", "when", "who", "kya", "kaise", "kyun", "kyu"}
_NUMBER = re.compile(r"\d+(?:\.\d+)?%?")


def _words(text):
    return re.findall(r"[\w'%]+", (text or "").lower(), flags=re.UNICODE)


def transcript_context(words, start, end, before_s=8.0, after_s=6.0):
    """Reconstruct the nearby transcript around a cut, without leaking its own text twice."""
    before = [word for timestamp, word in words if start - before_s <= timestamp < start]
    segment = [word for timestamp, word in words if start <= timestamp <= end + 0.05]
    after = [word for timestamp, word in words if end < timestamp <= end + after_s]
    return {
        "context_before": " ".join(before)[-700:],
        "segment_text": " ".join(segment),
        "context_after": " ".join(after)[:500],
    }


def hook_type(text):
    """Classify the opening style so the system can test formats against measured outcomes."""
    opening = " ".join(_words(text)[:20])
    toks = _words(opening)
    if not toks:
        return "unknown"
    if toks[0] in _QUESTION_START or "?" in (text or "")[:120]:
        return "question"
    if _NUMBER.search(opening):
        return "number_or_stat"
    if any(cue in opening for cue in _STORY_CUES):
        return "personal_story"
    contrast = {"but", "however", "instead", "although", "lekin", "magar"}
    if any(t in contrast for t in toks[:6]):
        return "contrast"
    return "strong_statement"


def editorial_signals(text, context_before="", context_after=""):
    """Estimate whether a cut opens independently and reaches a satisfying ending."""
    toks = _words(text)
    if not toks:
        return {
            "hook_type": "unknown", "context_dependency": 1.0,
            "standalone_heuristic": 0.0, "payoff_heuristic": 0.0,
            "sentence_count": 0,
        }

    leading = toks[:5]
    leading_context = any(t in _CONTEXT_LEADS for t in leading[:2])
    # A deictic opening ("this is why") is weaker when the preceding transcript
    # contains the actual noun/entity that the clip assumes the viewer already knows.
    before_tokens = set(_words(context_before)[-35:])
    dangling_reference = leading_context and any(
        t in {"he", "she", "it", "they", "this", "that", "these", "those", "वह", "यह"}
        for t in leading[:2]
    )
    dependency = 0.55 if leading_context else 0.0
    if dangling_reference and before_tokens:
        dependency += 0.20
    if len(toks) < 7:
        dependency += 0.15
    dependency = min(dependency, 1.0)

    ending = toks[-8:]
    weak_end = bool(ending and ending[-1] in _WEAK_ENDINGS)
    punctuation_close = bool(re.search(r"[.!?।][\"')\]]?\s*$", (text or "").strip()))
    payoff_cue = any(cue in (text or "").lower() for cue in _PAYOFF_CUES)
    payoff = 0.35
    if punctuation_close:
        payoff += 0.25
    if payoff_cue:
        payoff += 0.30
    if weak_end:
        payoff -= 0.35
    if len(toks) < 10:
        payoff -= 0.10
    payoff = min(max(payoff, 0.0), 1.0)

    standalone = min(max(0.72 - dependency * 0.65 + payoff * 0.28, 0.0), 1.0)
    sentences = len(re.findall(r"[.!?।]+", text or ""))
    return {
        "hook_type": hook_type(text),
        "context_dependency": round(dependency, 3),
        "standalone_heuristic": round(standalone, 3),
        "payoff_heuristic": round(payoff, 3),
        "sentence_count": sentences,
    }


def enrich_candidates_with_context(candidates, words):
    for candidate in candidates:
        context = transcript_context(words, candidate["start"], candidate["end"])
        candidate.update(context)
        signals = editorial_signals(
            context["segment_text"] or candidate.get("text", ""),
            context["context_before"], context["context_after"],
        )
        candidate.update(signals)
        # Keep a single canonical transcript slice for ranking, prompts and captions.
        if context["segment_text"]:
            candidate["text"] = context["segment_text"]
    return candidates


def speaker_summary(turns, start, end):
    """Summarise diarization turns intersecting a clip. Turns are {start,end,speaker} mappings."""
    clipped = []
    for turn in turns or []:
        try:
            a = max(float(start), float(turn["start"]))
            b = min(float(end), float(turn["end"]))
            speaker = str(turn["speaker"])
        except (KeyError, TypeError, ValueError):
            continue
        if b - a > 0.05:
            clipped.append((a, b, speaker))
    clipped.sort(key=lambda x: (x[0], x[1]))
    if not clipped:
        return {"speaker_count": None, "speaker_turns": None,
                "speaker_switches": None, "dominant_speaker_ratio": None,
                "speaker_overlap_s": None, "speaker_score": None}

    duration_by_speaker = defaultdict(float)
    for a, b, speaker in clipped:
        duration_by_speaker[speaker] += b - a
    total_talk = sum(duration_by_speaker.values())
    dominant_ratio = max(duration_by_speaker.values()) / max(total_talk, 1e-6)
    switches = sum(clipped[i][2] != clipped[i - 1][2] for i in range(1, len(clipped)))
    overlap = 0.0
    for i, left in enumerate(clipped):
        for right in clipped[i + 1:]:
            if right[0] >= left[1]:
                break
            if left[2] != right[2]:
                overlap += max(0.0, min(left[1], right[1]) - max(left[0], right[0]))
    count = len(duration_by_speaker)
    switch_density = switches / max((end - start) / 10.0, 1.0)
    score = 1.0
    if count > 2:
        score -= min(0.25, 0.08 * (count - 2))
    if switch_density > 2.0:
        score -= min(0.20, (switch_density - 2.0) * 0.04)
    if overlap > 0.8:
        score -= min(0.25, overlap / max(end - start, 1.0) * 0.6)
    return {
        "speaker_count": count,
        "speaker_turns": len(clipped),
        "speaker_switches": switches,
        "dominant_speaker_ratio": round(dominant_ratio, 3),
        "speaker_overlap_s": round(overlap, 2),
        "speaker_score": round(min(max(score, 0.0), 1.0), 3),
    }


def duration_bucket(seconds):
    seconds = float(seconds)
    if seconds < 20:
        return "15-19s"
    if seconds < 30:
        return "20-29s"
    if seconds < 40:
        return "30-39s"
    return "40s+"


def performance_quality(metrics, duration):
    """Evidence-weighted quality from retention and reach-normalised engagement.

    Missing metrics are omitted and remaining weights renormalised. Instagram's average-watch
    time can be returned in milliseconds; larger values are normalised to seconds.
    """
    metrics = metrics or {}
    duration = max(float(duration or 0), 1.0)

    def number(key):
        try:
            value = float(metrics.get(key))
            return max(value, 0.0)
        except (TypeError, ValueError):
            return None

    evidence = []
    avg_watch = number("ig_reels_avg_watch_time")
    if avg_watch is not None and avg_watch > 0:
        if avg_watch > max(180.0, duration * 3.0):
            avg_watch /= 1000.0
        retention = min(avg_watch / duration, 1.5)
        evidence.append((0.50, min(retention / 0.75, 1.0)))

    reach = number("reach")
    if reach is not None and reach > 0:
        for metric, weight, target in (
            ("shares", 0.25, 0.025),
            ("saved", 0.15, 0.020),
            ("likes", 0.10, 0.080),
        ):
            value = number(metric)
            if value is not None:
                evidence.append((weight, min((value / reach) / target, 1.0)))

    skip = number("reels_skip_rate")
    if skip is not None:
        if skip > 1.0:
            skip /= 100.0
        skip = min(max(skip, 0.0), 1.0)
        evidence.append((0.10, 1.0 - skip))

    total_weight = sum(weight for weight, _ in evidence)
    score = (sum(weight * value for weight, value in evidence) / total_weight
             if total_weight else None)
    return {
        "score": round(min(max(score, 0.0), 1.0), 4) if score is not None else None,
        "avg_watch_s": round(avg_watch, 3) if avg_watch is not None else None,
        "retention_pct": round(min(avg_watch / duration, 1.5) * 100, 2) if avg_watch is not None else None,
        "share_rate": round((number("shares") or 0.0) / reach, 5) if reach else None,
        "save_rate": round((number("saved") or 0.0) / reach, 5) if reach else None,
        "like_rate": round((number("likes") or 0.0) / reach, 5) if reach else None,
        "skip_rate": round(skip, 4) if skip is not None else None,
        "evidence_count": len(evidence),
    }

def format_priors(clips, min_samples=4):
    """Empirical format scores with a small-sample guard and shrinkage toward neutral."""
    grouped = defaultdict(list)
    for clip in clips or []:
        if clip.get("status") != "posted" or "performance_score" not in clip:
            continue
        features = clip.get("features") or {}
        score = clip.get("performance_score")
        try:
            score = min(max(float(score), 0.0), 1.0)
        except (TypeError, ValueError):
            continue
        for key in ("hook_type", "duration_bucket", "speaker_count_bucket"):
            value = features.get(key)
            if value:
                grouped[(key, str(value))].append(score)
    out = {}
    for (key, value), scores in grouped.items():
        if len(scores) < min_samples:
            continue
        mean = sum(scores) / len(scores)
        weight = min(len(scores) / (len(scores) + 8.0), 0.4)
        out[f"{key}:{value}"] = {
            "n": len(scores), "mean": round(mean, 4),
            "prior": round(0.5 + (mean - 0.5) * weight, 4),
        }
    return out


def apply_format_priors(candidates, priors):
    """Apply only a low-strength empirical prior once enough comparable posts exist."""
    for candidate in candidates:
        keys = [
            ("hook_type", candidate.get("hook_type")),
            ("duration_bucket", duration_bucket(candidate.get("end", 0) - candidate.get("start", 0))),
            ("speaker_count_bucket", (
                "1" if candidate.get("speaker_count") == 1 else
                "2" if candidate.get("speaker_count") == 2 else
                "3+" if isinstance(candidate.get("speaker_count"), int) else None
            )),
        ]
        values = [priors[f"{key}:{value}"]["prior"] for key, value in keys
                  if value is not None and f"{key}:{value}" in priors]
        if values:
            candidate["performance_prior"] = round(sum(values) / len(values), 4)
    return candidates

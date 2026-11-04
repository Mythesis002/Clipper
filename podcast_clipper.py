#!/usr/bin/env python3
"""
podcast_clipper.py - automated podcast -> Instagram Reels pipeline, built for GitHub Actions.

Design: the repo stores only a small JSON state file (state/state.json). Videos are NOT stored.
A clip is just {video_id, start, end, words, title, caption}. It is downloaded + rendered at the
moment it is posted, so every run can start on a clean, stateless runner.

Commands
  tick      the one Actions runs: (refill if empty) -> wait for slot -> post -> (refill) -> metrics
  produce   analyse one new episode and queue its best 3 segments   (--dry-run to only print)
  publish   post the next queued clip                              (--force: no slot wait/guard)
  metrics   pull Instagram insights for posted reels (feeds the learning loop)
  status    show queue + results
  check     verify tokens / tools / YouTube access / Gemini

Optional:
  set diarization.enabled=true, HF_TOKEN, and accept the model terms to use
  pyannote Community-1 speaker diarization on rendered clip audio.

Only process channels you have permission to clip (see config.yaml).
"""

import argparse
import base64
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time
from importlib.metadata import PackageNotFoundError, version as package_version
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests
import yaml
import yt_dlp
from yt_dlp.utils import download_range_func

from editorial_engine import (
    apply_format_priors, editorial_signals, enrich_candidates_with_context,
    format_priors, performance_quality, speaker_summary,
)
from intelligence_engine import (
    attach_semantic_context, candidate_feature_record, editorial_gate,
    episode_understanding_prompt, semantic_candidate_windows, timed_transcript,
    validate_episode_map,
)
from editing_engine import build_edit_plan, validate_edit_plan
from learning_engine import (
    add_feedback, add_metric_snapshot, learning_counts, predict_editorial_score,
    record_candidate_decisions, train_editorial_ranker,
)

BASE = Path(__file__).resolve().parent
WORK = BASE / "work"
STATE_FILE = BASE / "state" / "state.json"
REVIEW_DIR = BASE / "review"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_CONNECT_TIMEOUT = 15
GEMINI_TEXT_READ_TIMEOUT = 60
GEMINI_MULTIMODAL_READ_TIMEOUT = 180
GEMINI_MAX_ATTEMPTS = 3
BGUTIL_PLUGIN_VERSION = "2.0.2"
BGUTIL_PROVIDER_URL = "http://127.0.0.1:4416"
log = logging.getLogger("clipper")


# =============================================================================
# Utilities: config, state, logging helpers
# =============================================================================
def load_config(path=None):
    with open(path or BASE / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config.yaml must contain a YAML mapping")

    required = ("channels", "clip", "queue", "discovery", "llm", "instagram",
                "schedule", "learning")
    missing = [key for key in required if key not in cfg]
    if missing:
        raise ValueError("config.yaml missing required sections: " + ", ".join(missing))

    for key in ("clip", "queue", "discovery", "llm", "instagram", "schedule", "learning"):
        if not isinstance(cfg[key], dict):
            raise ValueError(f"config.yaml section {key!r} must be a mapping")
    cfg.setdefault("intelligence", {})
    if not isinstance(cfg["intelligence"], dict):
        raise ValueError("config.yaml section 'intelligence' must be a mapping")
    intelligence = cfg["intelligence"]
    if intelligence.get("promotion_policy", "manual_review") not in ("manual_review", "exclude", "allow"):
        raise ValueError("intelligence.promotion_policy must be manual_review, exclude or allow")
    threshold = float(intelligence.get("promotion_threshold", 0.72))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("intelligence.promotion_threshold must be between 0 and 1")
    max_chars = int(intelligence.get("max_transcript_chars", 90000))
    if max_chars < 1000 or max_chars > 250000:
        raise ValueError("intelligence.max_transcript_chars must be between 1000 and 250000")
    configured_gemini_model = str(cfg["llm"].get("model", GEMINI_MODEL)).strip()
    if configured_gemini_model != GEMINI_MODEL:
        raise ValueError(
            f"llm.model must be {GEMINI_MODEL}; this Clipper build is pinned to that model"
        )
    if "qa" in cfg and not isinstance(cfg["qa"], dict):
        raise ValueError("config.yaml section 'qa' must be a mapping")
    if "diarization" in cfg:
        if not isinstance(cfg["diarization"], dict):
            raise ValueError("config.yaml section 'diarization' must be a mapping")
        if type(cfg["diarization"].get("enabled", False)) is not bool:
            raise ValueError("diarization.enabled must be true or false")
        if cfg["diarization"].get("model", "pyannote/speaker-diarization-community-1") != "pyannote/speaker-diarization-community-1":
            raise ValueError("only pyannote/speaker-diarization-community-1 is currently supported")

    try:
        min_s, max_s = int(cfg["clip"]["min_s"]), int(cfg["clip"]["max_s"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("clip.min_s and clip.max_s must be integers") from None
    if min_s < 5 or max_s < min_s or max_s > 90:
        raise ValueError("clip.min_s/max_s must satisfy 5 <= min_s <= max_s <= 90")

    for key in ("clips_per_episode", "refill_when_below"):
        try:
            if int(cfg["queue"].get(key, 0)) < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"queue.{key} must be a positive integer") from None
    try:
        publish_candidate_limit = int(cfg["queue"].get("publish_candidate_limit", 10))
        if publish_candidate_limit < 1 or publish_candidate_limit > 100:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("queue.publish_candidate_limit must be an integer between 1 and 100") from None

    discovery = cfg["discovery"]
    if int(discovery.get("scan_latest", 0)) < 1 or int(discovery.get("max_episode_attempts", 1)) < 1:
        raise ValueError("discovery.scan_latest and max_episode_attempts must be positive")
    if float(discovery.get("min_duration_min", 0)) <= 0:
        raise ValueError("discovery.min_duration_min must be positive")
    if float(discovery.get("min_age_days", 0)) < 0 or float(discovery.get("max_age_days", 0)) < float(discovery.get("min_age_days", 0)):
        raise ValueError("discovery age window is invalid")

    if not isinstance(cfg["channels"], list):
        raise ValueError("channels must be a list")
    names = set()
    for ch in cfg["channels"]:
        if not isinstance(ch, dict) or not ch.get("name") or not ch.get("handle"):
            raise ValueError("every channel must be a mapping with name and handle")
        if ch["name"] in names:
            raise ValueError(f"duplicate channel name: {ch['name']}")
        names.add(ch["name"])
        if "permission" in ch and type(ch["permission"]) is not bool:
            raise ValueError(f"channel {ch['name']!r} permission must be true or false")

    try:
        ZoneInfo(cfg.get("timezone", ""))
    except Exception:
        raise ValueError("timezone must be a valid IANA timezone (for example Asia/Kolkata)") from None
    slots = cfg.get("post_times")
    if not isinstance(slots, list) or not slots:
        raise ValueError("post_times must be a non-empty list of HH:MM values")
    for slot in slots:
        try:
            datetime.strptime(str(slot), "%H:%M")
        except ValueError:
            raise ValueError(f"invalid post time {slot!r}; expected HH:MM") from None

    explore = float(cfg["learning"].get("explore_rate", -1))
    if not 0 <= explore <= 1:
        raise ValueError("learning.explore_rate must be between 0 and 1")
    for key, default in (("min_channel_samples", 3), ("min_format_samples", 4)):
        if int(cfg["learning"].get(key, default)) < 1:
            raise ValueError(f"learning.{key} must be a positive integer")

    qa = cfg.get("qa", {})
    for key in ("max_black_pct", "max_silence_pct"):
        value = float(qa.get(key, 12 if key == "max_black_pct" else 25))
        if not 0 <= value <= 100:
            raise ValueError(f"qa.{key} must be between 0 and 100")
    if float(qa.get("max_silence_segment_s", 2.5)) <= 0:
        raise ValueError("qa.max_silence_segment_s must be positive")
    if not 0 <= float(qa.get("min_ai_score", 6.5)) <= 10:
        raise ValueError("qa.min_ai_score must be between 0 and 10")
    if qa.get("ai_unavailable", "manual_review") not in ("manual_review", "technical_only"):
        raise ValueError("qa.ai_unavailable must be manual_review or technical_only")
    return cfg

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"episodes": {}, "clips": [], "next_id": 1}


def save_state(st):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


def now_utc():
    return datetime.now(timezone.utc)


def now_iso():
    return now_utc().isoformat(timespec="seconds")


def hms(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def gha(level, msg):
    """GitHub Actions annotation (shows up on the run page / in the notification email)."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{level}::{msg}")

def env_value(name, default=""):
    """Read a secret/config environment value without accidental surrounding whitespace."""
    return str(os.environ.get(name, default) or "").strip()


def graph(cfg):
    return f"https://graph.facebook.com/{cfg['instagram'].get('graph_version', 'v25.0')}"


def jr(state, event, **detail):
    """Run journal: what the system decided and why (kept in state.json, shown in the run summary)."""
    j = state.setdefault("journal", [])
    j.append({"t": now_iso(), "event": event, **detail})
    del j[:-80]


def views_of(clip):
    m = clip.get("metrics") or {}
    return m.get("views", m.get("plays"))


# =============================================================================
# 1. DISCOVER
# =============================================================================
def ydl_opts(cfg, **extra):
    o = {"quiet": True, "no_warnings": True, "retries": 5, "extractor_retries": 3,
         "socket_timeout": 30}
    cookies = env_value("YT_COOKIES_FILE") or str(cfg.get("cookies_file") or "").strip()
    if cookies and Path(cookies).exists():
        o["cookiefile"] = cookies
    proxy = env_value("YT_PROXY")
    if proxy:
        o["proxy"] = proxy
    o.update(extra)
    return o


def list_recent(cfg, handle, n):
    opts = ydl_opts(cfg, extract_flat=True, playlistend=n, skip_download=True)
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(f"https://www.youtube.com/{handle}/videos", download=False)
    return info.get("entries") or []


def full_info(cfg, video_id):
    # Metadata/caption discovery should still work when YouTube temporarily exposes
    # no directly downloadable formats for the selected player client. The later
    # download probe remains strict and must successfully fetch real media.
    with yt_dlp.YoutubeDL(ydl_opts(cfg, skip_download=True, ignore_no_formats_error=True)) as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def channel_stats(state):
    """Bayesian-shrunk per-channel performance score plus view count for diagnostics."""
    acc = {}
    for c in state["clips"]:
        if c.get("status") != "posted":
            continue
        v = views_of(c)
        score = c.get("performance_score")
        bucket = acc.setdefault(c.get("channel", "unknown"), {"views": [], "quality": []})
        if v is not None:
            bucket["views"].append(float(v))
        try:
            if score is not None:
                bucket["quality"].append(min(max(float(score), 0.0), 1.0))
        except (TypeError, ValueError):
            pass

    out = {}
    for channel, values in acc.items():
        quality = values["quality"]
        n = len(quality)
        # Neutral Beta-style prior prevents one unusually strong Reel from dominating.
        avg_quality = (sum(quality) + 0.5 * 5.0) / (n + 5.0)
        out[channel] = {
            "n": len(values["views"]),
            "avg_views": sum(values["views"]) / len(values["views"]) if values["views"] else 0.0,
            "quality_n": n,
            "avg_quality": avg_quality,
        }
    return out



def authorized_channels(cfg):
    """Return only channels explicitly enabled with the boolean permission: true."""
    return [c for c in cfg["channels"] if c.get("permission") is True]


def order_channels(cfg, state, rng=random):
    """Balance source coverage first, then use learned quality to break ties.

    Episode counts include successfully queued and skipped/failed attempts, so a
    source that repeatedly yields unusable episodes cannot monopolize discovery.
    """
    chans = authorized_channels(cfg)
    stats = channel_stats(state)
    last = {}
    episode_counts = {}
    for ep in state.get("episodes", {}).values():
        name = ep.get("channel")
        if not name:
            continue
        episode_counts[name] = episode_counts.get(name, 0) + 1
        last[name] = max(last.get(name, ""), ep.get("processed_at", ""))

    min_samples = int(cfg.get("learning", {}).get("min_channel_samples", 3))
    explore = rng.random() < cfg.get("learning", {}).get("explore_rate", 0.0)

    def priority(channel):
        name = channel["name"]
        # Fairness is the primary criterion. Among equally served channels,
        # process the least recently used source before applying quality ranking.
        coverage = (episode_counts.get(name, 0), last.get(name, ""))
        if explore:
            return coverage
        quality = stats.get(name, {})
        mature = quality.get("quality_n", 0) >= min_samples
        return (*coverage, mature, -quality.get("avg_quality", 0.5))

    return sorted(chans, key=priority)


def done_ids(state):
    """Episodes not to pick again. A transient failure carries retry_after and becomes eligible later."""
    now = now_iso()
    return {v for v, e in state["episodes"].items() if not (e.get("retry_after") and e["retry_after"] < now)}


SKIP_TITLE = re.compile(r"\b(trailer|teaser|promo|shorts?|announcement)\b", re.I)


def pick_episode(cfg, state):
    chans = order_channels(cfg, state)
    if not chans:
        log.warning("No authorized channels are enabled (set permission: true only for sources you are allowed to clip).")
        return None, None
    done = done_ids(state)
    d = cfg["discovery"]
    for ch in chans:
        log.info("scanning %s", ch["name"])
        try:
            entries = list_recent(cfg, ch["handle"], d["scan_latest"])
        except Exception as ex:
            log.warning("could not list %s: %s", ch["name"], ex)
            continue
        scored = []
        for e in entries:
            vid, dur = e.get("id"), e.get("duration")
            if not vid or vid in done or SKIP_TITLE.search(e.get("title") or ""):
                continue
            if dur is not None and dur < d["min_duration_min"] * 60:
                continue
            try:
                info = full_info(cfg, vid)
            except Exception as ex:
                log.warning("info failed for %s: %s", vid, str(ex)[:200])
                continue
            if (info.get("duration") or 0) < d["min_duration_min"] * 60:
                continue
            if info.get("live_status") in ("is_live", "is_upcoming"):
                continue
            ts = info.get("timestamp")
            age = (time.time() - ts) / 86400 if ts else 999
            if not (d["min_age_days"] <= age <= d["max_age_days"]):
                continue
            if not info.get("heatmap"):
                log.info("  %s: no Most-Replayed data; using transcript/editorial scoring", vid)
            scored.append(((info.get("view_count") or 0) / max(age, 1), info))
        if scored:
            scored.sort(key=lambda x: -x[0])
            return ch, scored[0][1]
    return None, None


# =============================================================================
# 2. ANALYSE
# =============================================================================
def parse_json3(data):
    """YouTube json3 captions -> [(start_seconds, word)]."""
    words = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        base = ev.get("tStartMs", 0)
        dur = ev.get("dDurationMs", 0)
        if len(segs) == 1:                        # manual captions: a whole phrase per event
            toks = segs[0].get("utf8", "").replace("\n", " ").split()
            for i, w in enumerate(toks):
                words.append(((base + dur * i / max(len(toks), 1)) / 1000.0, w))
            continue
        for s in segs:                            # auto captions: per-word offsets
            w = s.get("utf8", "").replace("\n", " ").strip()
            if w:
                words.append(((base + s.get("tOffsetMs", 0)) / 1000.0, w))
    words.sort(key=lambda x: x[0])
    return words


def caption_candidates(info, langs):
    """json3 caption tracks, best first.
    Manual subtitles beat auto captions; the video's OWN language beats everything
    (so a Hindi podcast never gets auto-translated English captions burned in);
    YouTube's machine translations (tlang=) are a last resort."""
    own = (info.get("language") or "").split("-")[0] or None
    wanted = [own] if own else [x for x in langs if x]
    out = []
    for pool_rank, pool in enumerate(("subtitles", "automatic_captions")):
        for key, fmts in (info.get(pool) or {}).items():
            is_orig = key.endswith("-orig")
            base = key.removesuffix("-orig")
            lang_rank = next((i for i, l in enumerate(wanted)
                              if base == l or base.startswith(l + "-")), None)
            if lang_rank is None and not (is_orig and own is None):
                continue
            for fmt in fmts or []:
                if fmt.get("ext") != "json3" or not fmt.get("url"):
                    continue
                translated = "tlang=" in fmt["url"]
                out.append(((translated, pool_rank, 0 if is_orig else 1, lang_rank or 0),
                            pool, key, fmt))
    out.sort(key=lambda x: x[0])
    return out


def dedupe_words(words):
    """Drop exact repeats (same word within 50 ms) that rolling captions sometimes produce."""
    out = []
    for t, w in words:
        if out and out[-1][1] == w and abs(out[-1][0] - t) < 0.05:
            continue
        out.append((t, w))
    return out


def fetch_words(cfg, info):
    langs = cfg["discovery"].get("caption_langs", ["en"])
    for _, pool, key, fmt in caption_candidates(info, langs):
        try:
            r = requests.get(fmt["url"], timeout=40)
            r.raise_for_status()
            words = dedupe_words(parse_json3(r.json()))
            if len(words) > 200:
                log.info("transcript: %s/%s (%d words)", pool, key, len(words))
                return words
        except Exception as ex:
            log.warning("caption fetch failed (%s/%s): %s", pool, key, ex)
    log.warning("no usable transcript; arbitrary time-grid clips are disabled until speech can be evaluated")
    return []


def phrase_starts(words, pause=0.45, weak_pause=0.25):
    """Times where a new phrase/sentence begins. A strong pause or sentence punctuation always
    counts; a shorter pause counts when the next word is a question/hook word ("why", "how",
    "never"...), because speakers pause only briefly before a rhetorical question."""
    if not words:
        return []
    starters = QUESTION_WORDS | HOOK_WORDS
    starts = [words[0][0]]
    for i in range(1, len(words)):
        pt, pw = words[i - 1]
        gap = words[i][0] - (pt + min(0.6, 0.07 * len(pw) + 0.15))
        w = re.sub(r"\W", "", words[i][1].lower())
        if gap >= pause or pw[-1] in ".?!\u0964" or (gap >= weak_pause and w in starters):
            starts.append(words[i][0])
    return starts


def heat_per_second(heatmap, duration):
    """Normalised replay signal; missing/invalid heatmap data is neutral, not fatal."""
    n = max(1, int(np.ceil(float(duration))))
    if not heatmap:
        return np.full(n, 0.5, dtype=float)
    valid = []
    for h in heatmap:
        try:
            start, end, value = float(h["start_time"]), float(h["end_time"]), float(h["value"])
            if np.isfinite(start) and np.isfinite(end) and np.isfinite(value) and end >= start:
                valid.append(((start + end) / 2, value))
        except (KeyError, TypeError, ValueError):
            continue
    if not valid:
        return np.full(n, 0.5, dtype=float)
    valid.sort(key=lambda x: x[0])
    mids = np.asarray([x[0] for x in valid], dtype=float)
    vals = np.asarray([x[1] for x in valid], dtype=float)
    y = np.interp(np.arange(n), mids, vals)
    med, k = float(np.median(y)), max(1, int(len(y) * 0.03))
    y[:k] = med
    y[-k:] = med
    span = float(y.max() - y.min())
    if span <= 1e-9:
        return np.full(n, 0.5, dtype=float)
    return (y - y.min()) / span



HOOK_WORDS = {"never", "biggest", "secret", "mistake", "truth", "nobody", "stop", "worst",
              "best", "rich", "failed", "fired", "broke", "lie", "wrong", "scam", "crore",
              "lakh", "million", "billion", "hardest", "shocking", "real", "why", "regret",
              "sabse", "galti", "sach", "jhooth", "paisa", "ameer"}
QUESTION_WORDS = {"why", "how", "what", "when", "who", "kya", "kaise", "kyun", "kyu", "kab"}
FILLER_START = {"and", "but", "so", "because", "also", "or", "then", "which", "that", "aur",
                "lekin", "toh", "par", "kyunki", "matlab", "like", "yeah", "right"}


def hook_score(opening_text):
    """Conservative opening heuristic. It is a ranking signal, never a viral predictor."""
    text = re.sub(r"\s+", " ", opening_text or "").strip()
    toks = re.findall(r"[\w'%]+", text.lower())[:18]
    if not toks:
        return 0.0
    score = 0.38
    # Cold-open independence: penalize pronouns, conjunctions and setup fragments.
    dependent = {"this", "that", "it", "they", "he", "she", "because", "which",
                 "and", "but", "so", "then", "there", "here"}
    if toks[0] in FILLER_START:
        score -= 0.18
    if toks[0] in dependent:
        score -= 0.13
    if len(toks) >= 2 and toks[1] in {"because", "and", "but", "so"}:
        score -= 0.06
    # Curiosity/tension signals are modest bonuses, not clickbait shortcuts.
    if toks[0] in QUESTION_WORDS or "?" in text[:120]:
        score += 0.09
    curiosity_terms = {"secret", "mistake", "truth", "reason", "actually", "wrong",
                       "risk", "fear", "failed", "failure", "changed", "never", "why",
                       "problem", "surprising", "impossible", "hidden"}
    if any(t in curiosity_terms for t in toks):
        score += 0.07
    # Concrete detail and complete-thought signals.
    if any(re.search(r"\d", t) for t in toks):
        score += 0.08
    if any(t in HOOK_WORDS for t in toks):
        score += 0.04
    if len(toks) < 5:
        score -= 0.12
    elif 7 <= len(toks) <= 18:
        score += 0.08
    if len(text) > 160 and not any(mark in text for mark in ".?!।"):
        score -= 0.07
    if text and text[0].islower():
        score -= 0.04
    if len(toks) <= 7 and any(t in {"shocking", "insane", "crazy", "viral"} for t in toks):
        score -= 0.08
    return float(min(max(score, 0.0), 1.0))


def trimmed_end(wt, words, e):
    """Pull a window end back to just after the last spoken word (no dead air)."""
    i = bisect_left(wt, e) - 1
    if i < 0:
        return e
    t, w = words[i]
    return min(e, t + min(0.6, 0.07 * len(w) + 0.15) + 0.35)


def score_candidates(cfg, heat, words, duration):
    # Do not manufacture time-grid clips when speech transcription is unavailable.
    # Editorial context, payoff and reliable captions cannot be evaluated without words.
    if not words:
        return []
    mn, mx = cfg["clip"]["min_s"], cfg["clip"]["max_s"]
    wt = [w[0] for w in words]
    starts = phrase_starts(words)
    raw = []
    if starts:
        for s in starts:
            if s + mn > duration:
                break
            j0, j1 = bisect_left(starts, s + mn), bisect_right(starts, s + mx)
            for j in range(j0, j1):
                e = trimmed_end(wt, words, min(starts[j], duration))
                if e - s >= mn - 1:
                    raw.append((s, e))
    else:
        return []

    def hmean(c):
        a = min(int(c[0]), len(heat) - 1)
        return heat[a:max(a + 1, min(int(c[1]), len(heat)))].mean()

    raw.sort(key=lambda c: -hmean(c))
    out = []
    for s, e in raw[:800]:
        a, b = int(s), min(int(e), len(heat))
        seg = heat[a:b]
        if len(seg) < 5:
            continue
        # Heatmap is evidence from the original long-form audience, not a short-form
        # success label. Keep it influential but do not let it drown out the opening.
        heat_part = 0.55 * float(seg.mean()) + 0.20 * float(seg.max()) + 0.25 * float(seg[:5].mean())
        i0, i1 = bisect_left(wt, s), bisect_left(wt, e)
        text = " ".join(w for _, w in words[i0:i1])
        hook = hook_score(" ".join(w for _, w in words[i0:i0 + 18])) if words else 0.3
        dens = min((i1 - i0) / (e - s) / 2.8, 1.0) if words else 0.6
        # Balanced pre-rank: hook matters almost as much as replay evidence.
        pre = 0.42 * heat_part + 0.40 * hook + 0.18 * dens
        out.append({"start": float(s), "end": float(e), "heat": float(heat_part),
                    "hook": hook, "density": float(dens), "pre": float(pre), "text": text})
    out.sort(key=lambda c: -c["pre"])
    return out


def diversify(cands, k, gap, variants=1):
    """Shortlist for the LLM: up to k distinct MOMENTS (>= gap seconds apart), each with up to
    `variants` different framings (starts >= 3 s apart) so Gemini can pick the best opening."""
    groups = []
    for c in cands:                                   # cands are sorted best-first by pre-score
        for g in groups:
            a = g[0]
            if not (c["end"] + gap <= a["start"] or c["start"] >= a["end"] + gap):
                if len(g) < variants and all(abs(c["start"] - x["start"]) >= 3 for x in g):
                    g.append(c)
                break
        else:
            if len(groups) < k:
                groups.append([c])
    return [c for g in groups for c in g]


# ---------------------------------------------------------------- Gemini
class GeminiError(Exception):
    pass


_MODEL_CACHE = {}


def gemini_key():
    # A copied GitHub secret can include a trailing newline or spaces. Strip each
    # candidate separately so a whitespace-only primary value does not hide fallback.
    return env_value("GEMINI_API_KEY") or env_value("GOOGLE_API_KEY")


def _gemini_post(model, body, key, timeout=(GEMINI_CONNECT_TIMEOUT, GEMINI_TEXT_READ_TIMEOUT),
                 max_attempts=GEMINI_MAX_ATTEMPTS):
    """POST to one pinned model, retrying transient network and server failures."""
    url = f"{GEMINI_BASE}/models/{model}:generateContent"
    retryable_statuses = (408, 429, 500, 502, 503, 504)

    for attempt in range(max_attempts):
        try:
            response = requests.post(
                url, json=body, headers={"x-goog-api-key": key}, timeout=timeout
            )
        except (requests.Timeout, requests.ConnectionError) as ex:
            if attempt + 1 >= max_attempts:
                raise GeminiError(
                    f"{model} request timed out or lost its connection after "
                    f"{max_attempts} attempts: {ex}"
                ) from ex
            wait = min(2 ** (attempt + 1), 10)
            log.warning(
                "Gemini %s network attempt %d/%d failed (%s); retrying in %ds",
                model, attempt + 1, max_attempts, type(ex).__name__, wait,
            )
            time.sleep(wait)
            continue

        if response.status_code in retryable_statuses and attempt + 1 < max_attempts:
            try:
                retry_after = float(response.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                retry_after = 0
            wait = min(retry_after or 2 ** (attempt + 1), 30)
            log.warning(
                "Gemini %s returned HTTP %d on attempt %d/%d; retrying in %ds",
                model, response.status_code, attempt + 1, max_attempts, wait,
            )
            time.sleep(wait)
            continue
        return response

    raise GeminiError(f"{model} request failed after {max_attempts} attempts")


def gemini_text(cfg, parts, temperature=0.2, max_tokens=8192, response_schema=None):
    """Call only Gemini 2.5 Flash; never switch or auto-discover another model."""
    key = gemini_key()
    if not key:
        raise GeminiError("no GEMINI_API_KEY")

    llm = cfg["llm"]
    configured_model = str(llm.get("model", GEMINI_MODEL)).strip()
    if configured_model != GEMINI_MODEL:
        raise GeminiError(
            f"Clipper is pinned to {GEMINI_MODEL}, but llm.model is "
            f"{configured_model!r}; update config.yaml"
        )

    generation_config = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
        "responseMimeType": "application/json",
    }
    if response_schema is not None:
        generation_config["responseSchema"] = response_schema
    body = {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": generation_config}
    multimodal = any(
        isinstance(part, dict) and ("fileData" in part or "inlineData" in part)
        for part in parts
    )
    read_timeout = (
        GEMINI_MULTIMODAL_READ_TIMEOUT if multimodal else GEMINI_TEXT_READ_TIMEOUT
    )
    timeout = (GEMINI_CONNECT_TIMEOUT, read_timeout)
    response = _gemini_post(GEMINI_MODEL, body, key, timeout=timeout)

    if response.status_code == 404:
        raise GeminiError(
            f"{GEMINI_MODEL} returned HTTP 404; this API key/project may not have "
            "access to the model or the model may no longer be served"
        )
    if not response.ok:
        raise GeminiError(
            f"{GEMINI_MODEL} HTTP {response.status_code}: {response.text[:300]}"
        )

    data = response.json()
    block = (data.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise GeminiError(f"prompt blocked: {block}")
    cands = data.get("candidates") or []
    if not cands:
        raise GeminiError("no candidates in response")
    out_parts = (cands[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in out_parts if not p.get("thought"))
    if not text.strip():
        raise GeminiError(f"empty response (finishReason={cands[0].get('finishReason')})")
    _MODEL_CACHE["model"] = GEMINI_MODEL
    return text


def parse_json(text):
    """Parse plain, fenced, or prose-wrapped JSON without greedy regex extraction."""
    if not isinstance(text, str) or not text.strip():
        raise GeminiError("response was empty or not text")
    decoder = json.JSONDecoder()
    source = text.strip()
    # Prefer a complete top-level JSON value, even when a model adds a short preamble.
    try:
        return json.loads(source)
    except ValueError:
        pass
    for index, char in enumerate(source):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(source[index:])
            if isinstance(value, (dict, list)):
                return value
        except ValueError:
            continue
    raise GeminiError("response was not valid JSON")


def _score(x, default=None):
    try:
        value = float(x)
        return min(max(value, 0.0), 10.0) if np.isfinite(value) else default
    except (TypeError, ValueError):
        return default


def _truthy(x):
    return x is True or (isinstance(x, str) and x.strip().lower() == "true")


def build_rerank_prompt(ep_title, cands, episode_map=None):
    payload = [{
        "id": i,
        "t": hms(c["start"]),
        "clip": c["text"][:850],
        "context_before_not_in_clip": c.get("context_before", "")[-300:],
        "context_after_not_in_clip": c.get("context_after", "")[:200],
        "semantic": {
            "kind": c.get("semantic_kind"),
            "topic": c.get("semantic_topic"),
            "summary": c.get("semantic_summary"),
            "confidence": c.get("semantic_confidence"),
            "promotion_type": c.get("promotion_type"),
            "promotion_evidence": c.get("promotion_evidence"),
        },
        "heuristics": {
            "opening_type": c.get("hook_type"),
            "context_dependency": c.get("context_dependency"),
            "standalone_estimate": c.get("standalone_heuristic"),
            "payoff_estimate": c.get("payoff_heuristic"),
        },
    } for i, c in enumerate(cands)]
    summary = (episode_map or {}).get("summary", "")
    return (
        f"You are a senior short-form podcast editor choosing complete moments from {ep_title!r}.\n"
        f"Episode context: {summary[:1200]}\n"
        "The clip field is the ONLY content that will appear in the Reel. Context fields are provided "
        "only to detect dangling references; they are NOT part of the clip.\n"
        "Treat the first 1-3 seconds as a separate scroll-stop test: is the first spoken line "
        "specific, immediately understandable without prior context, curiosity-provoking, and paid off "
        "later in the clip? Score opening hook separately from overall quality. Score 0-10: hook, standalone "
        "clarity, payoff, emotion, specificity, context independence, curiosity, practical/emotional value, "
        "and story structure. Prefer complete ideas with satisfying payoff. Do not reward clickbait, outrage, "
        "misleading edits or unverified allegations.\n"
        "Classify content_type as one of story, insight, explanation, opinion, debate, question_answer, "
        "reaction, promotion, or other. Identify promotional intent separately: none, sponsor_read, "
        "affiliate, product_pitch, self_promotion or unclear. A brief natural mention of a product is "
        "NOT automatically promotional. Mark promotional only when the speaker is actually selling, "
        "endorsing as an integration, reading a sponsor message or directing viewers to buy/use a "
        "product or service. Give promotion_confidence and classification_confidence between 0 and 1. "
        "Include a short evidence phrase. Do not infer sponsorship from a brand name alone.\n"
        "Set reject=true for broken/incomplete segments, misleading or unsafe content. Set "
        "promotion_type and content_type accurately even when the clip is otherwise excellent.\n"
        "Create a truthful title in the same language/script as the clip. Return ONLY JSON array: "
        '[{"id":0,"hook":0,"standalone":0,"payoff":0,"emotion":0,"specificity":0,'
        '"context_independence":0,"curiosity":0,"value":0,"story_structure":0,'
        '"content_type":"story","classification_confidence":0.0,"promotion_type":"none",'
        '"promotion_confidence":0.0,"promotion_evidence":"","reject":false,'
        '"title":"","caption_hook":"","hashtags":["#x"]}]\n\n'
        + json.dumps(payload, ensure_ascii=False))

def apply_rerank(cands, data):
    """Merge Gemini's editorial scorecard and content classification into candidates."""
    if isinstance(data, dict):
        data = data.get("clips") or data.get("results") or []
    applied = 0
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        try:
            i = int(d["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if not 0 <= i < len(cands):
            continue
        scores = {key: _score(d.get(key)) for key in
                  ("hook", "standalone", "payoff", "emotion", "specificity",
                   "context_independence", "curiosity", "value", "story_structure")}
        if any(scores[key] is None for key in ("hook", "standalone", "emotion")):
            continue
        c = cands[i]
        scores["payoff"] = scores["payoff"] if scores["payoff"] is not None else scores["standalone"]
        scores["specificity"] = scores["specificity"] if scores["specificity"] is not None else scores["hook"]
        scores["context_independence"] = (scores["context_independence"]
            if scores["context_independence"] is not None
            else max(0.0, 10.0 * (1.0 - c.get("context_dependency", 0.5))))
        scores["curiosity"] = scores["curiosity"] if scores["curiosity"] is not None else scores["hook"]
        scores["value"] = scores["value"] if scores["value"] is not None else scores["specificity"]
        scores["story_structure"] = (scores["story_structure"]
            if scores["story_structure"] is not None else scores["payoff"])
        c["editorial_scores"] = {key: round(value, 2) for key, value in scores.items()}
        c["llm"] = (
            scores["hook"] * 0.16 + scores["standalone"] * 0.14
            + scores["payoff"] * 0.15 + scores["emotion"] * 0.10
            + scores["specificity"] * 0.10 + scores["context_independence"] * 0.10
            + scores["curiosity"] * 0.10 + scores["value"] * 0.10
            + scores["story_structure"] * 0.05
        ) / 10
        c["context_independence_llm"] = scores["context_independence"] / 10
        c["payoff"] = scores["payoff"] / 10
        c["specificity"] = scores["specificity"] / 10
        c["reject"] = _truthy(d.get("reject"))
        allowed_types = {"story", "insight", "explanation", "opinion", "debate",
                         "question_answer", "reaction", "promotion", "other"}
        content_type = str(d.get("content_type") or c.get("semantic_kind") or "other").lower()
        c["content_type"] = content_type if content_type in allowed_types else "other"
        promotion_types = {"none", "sponsor_read", "affiliate", "product_pitch",
                           "self_promotion", "unclear"}
        proposed_promotion_type = str(d.get("promotion_type") or "").lower()
        if proposed_promotion_type not in promotion_types:
            proposed_promotion_type = "unclear"
        try:
            proposed_promotion_confidence = float(d.get("promotion_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            proposed_promotion_confidence = 0.0
        if proposed_promotion_confidence > 1.0:
            proposed_promotion_confidence /= 10.0
        proposed_promotion_confidence = max(0.0, min(1.0, proposed_promotion_confidence))
        existing_promotion_type = str(c.get("promotion_type") or "none").lower()
        existing_promotion_confidence = float(c.get("promotion_confidence") or 0.0)
        # Conflicting text/video judgements preserve the stronger risk signal for review.
        if (proposed_promotion_type not in ("none", "")
                and proposed_promotion_confidence >= existing_promotion_confidence):
            c["promotion_type"] = proposed_promotion_type
        else:
            c["promotion_type"] = existing_promotion_type
        c["promotion_confidence"] = max(
            existing_promotion_confidence, proposed_promotion_confidence)
        try:
            classification_confidence = float(d.get("classification_confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            classification_confidence = 0.0
        if classification_confidence > 1.0:
            classification_confidence /= 10.0
        c["classification_confidence"] = max(0.0, min(1.0, classification_confidence))
        c["promotion_evidence"] = str(d.get("promotion_evidence") or "")[:240]
        c["title"] = str(d.get("title") or "").strip()[:80]
        c["caption_hook"] = str(d.get("caption_hook") or "").strip()[:140]
        tags = d.get("hashtags") if isinstance(d.get("hashtags"), list) else []
        clean = [re.sub(r"\W", "", str(t)) for t in tags
                 if isinstance(t, (str, int)) and not isinstance(t, bool)]
        c["hashtags"] = ["#" + t for t in clean if t][:6]
        applied += 1
    return applied

def llm_rerank(cfg, cands, ep_title, episode_map=None):
    """Stage 1: Gemini evaluates editorial quality and labels the content intent."""
    if not cfg["llm"]["enabled"] or not gemini_key():
        log.info("Gemini rerank off (disabled or no GEMINI_API_KEY) - heuristics only")
        return cands
    try:
        data = parse_json(gemini_text(
            cfg, [{"text": build_rerank_prompt(ep_title, cands, episode_map)}]))
        n = apply_rerank(cands, data)
        log.info("Gemini scored and classified %d/%d candidates (model %s)",
                 n, len(cands), _MODEL_CACHE.get("model"))
    except Exception as ex:
        log.warning("Gemini editorial evaluation failed, using semantic/heuristic fallback: %s", ex)
    return cands

VERIFY_PROMPT = (
    "Watch this podcast segment with sound. Evaluate whether it starts and ends cleanly, "
    "makes sense without context, has compelling delivery and useful framing. Also detect "
    "whether the segment is primarily a sponsor read, paid integration, affiliate pitch, "
    "product/service sales pitch or creator self-promotion. A passing mention of a product "
    "is not automatically an advertisement. Return promotion_type (none, sponsor_read, "
    "affiliate, product_pitch, self_promotion, unclear), promotion_confidence from 0 to 1, "
    "and a short evidence phrase. Score hook, standalone, delivery and visual from 0 to 10. "
    'Return ONLY JSON: {"hook":0,"standalone":0,"delivery":0,"visual":0,'
    '"promotion_type":"none","promotion_confidence":0.0,"promotion_evidence":"",'
    '"reject":false}')

def video_verify(cfg, video_id, cands):
    """Stage 2: Gemini watches the strongest candidates and checks audiovisual quality."""
    llm = cfg["llm"]
    if not (llm.get("video_verify") and gemini_key()):
        return cands
    ranked = sorted([c for c in cands if not c.get("reject")], key=lambda c: -final_score(c))
    for c in ranked[: llm.get("verify_top", 6)]:
        try:
            parts = [{"fileData": {"fileUri": f"https://www.youtube.com/watch?v={video_id}"},
                      "videoMetadata": {"startOffset": f"{int(c['start'])}s",
                                        "endOffset": f"{int(c['end']) + 1}s"}},
                     {"text": VERIFY_PROMPT}]
            d = parse_json(gemini_text(cfg, parts))
            if isinstance(d, list) and d:
                d = d[0]
            h, s, dl, v = (_score(d.get(k)) for k in ("hook", "standalone", "delivery", "visual"))
            if None in (h, s, dl, v):
                continue
            c["vid"] = (h * 0.35 + s * 0.25 + dl * 0.30 + v * 0.10) / 10
            promotion_type = str(d.get("promotion_type") or "none").lower()
            try:
                video_promotion_confidence = float(d.get(
                    "promotion_confidence", c.get("promotion_confidence", 0)) or 0)
            except (TypeError, ValueError):
                video_promotion_confidence = 0.0
            video_promotion_confidence = max(0.0, min(1.0, video_promotion_confidence))
            existing_confidence = float(c.get("promotion_confidence") or 0.0)
            if (promotion_type in {"sponsor_read", "affiliate", "product_pitch",
                                   "self_promotion", "unclear"}
                    and video_promotion_confidence >= existing_confidence):
                c["promotion_type"] = promotion_type
            c["promotion_confidence"] = max(existing_confidence, video_promotion_confidence)
            if d.get("promotion_evidence"):
                c["promotion_evidence"] = str(d["promotion_evidence"])[:240]
            if _truthy(d.get("reject")):
                c["reject"] = True
        except Exception as ex:
            log.warning("video verify failed for %s: %s", hms(c["start"]), ex)
    return cands

def retime(words, text):
    """Apply caption corrections only when token count is unchanged; keep exact timestamps."""
    new = text.split()
    if not new or not words or len(new) != len(words):
        return None
    return [(t, token) for (t, _), token in zip(words, new)]

def caption_change_ok(old_words, new_words, min_overlap=0.80, max_len_change=0.0):
    """Allow cautious lexical corrections without changing word-to-time alignment."""
    if not old_words or not new_words or len(new_words) != len(old_words):
        return False
    norm = lambda w: re.sub(r"\W", "", w.lower())  # noqa: E731
    old, new = Counter(norm(w) for _, w in old_words), Counter(norm(w) for _, w in new_words)
    return sum((old & new).values()) / len(old_words) >= min_overlap

def polish_captions(cfg, picks, ep_title):
    """Gemini fixes mis-heard words/names/numbers, casing and punctuation in the chosen clips' captions.
    Timing is kept (tokens are re-spread over the original timeline); unsafe rewrites are discarded."""
    if not (cfg["llm"].get("polish_captions") and cfg["clip"]["captions"] and gemini_key()):
        return 0
    items = [{"id": i, "text": " ".join(w for _, w in p["words"])} for i, p in enumerate(picks) if p.get("words")]
    if not items:
        return 0
    prompt = (
        f"These are auto-generated captions (speech recognition errors likely) from clips of a podcast titled "
        f"{ep_title!r}. For each item fix misheard words, names, brands and numbers using context, and add "
        "natural casing and punctuation (. , ? !). Keep the SAME language and script as the input (never "
        "translate, never switch Hinglish to Devanagari or the reverse), keep the same sentences and order, "
        'add nothing. Reply with ONLY a JSON array: [{"id":0,"text":"..."}]\n\n'
        + json.dumps(items, ensure_ascii=False))
    try:
        data = parse_json(gemini_text(cfg, [{"text": prompt}], temperature=0.1))
    except Exception as ex:
        log.warning("caption polish failed, using raw captions: %s", ex)
        return 0
    n = 0
    for d in data if isinstance(data, list) else []:
        try:
            p = picks[int(d["id"])]
            new = retime(p["words"], str(d["text"]))
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if new and caption_change_ok(p["words"], new):
            p["words"], p["polished"] = new, True
            n += 1
        else:
            log.info("caption rewrite for clip %s rejected by safety guard", d.get("id"))
    log.info("polished captions for %d/%d clips", n, len(items))
    return n


def final_score(c):
    """Blend source replay, editorial judgement, video review and mature format learning."""
    parts = [(c.get("pre", 0.5), 0.40)]
    if "llm" in c:
        parts.append((c["llm"], 0.30))
    if "vid" in c:
        parts.append((c["vid"], 0.20))
    if "standalone_heuristic" in c and "payoff_heuristic" in c:
        editorial = (0.55 * c["standalone_heuristic"]
                     + 0.45 * c["payoff_heuristic"])
        parts.append((editorial, 0.10))
    base = sum(value * weight for value, weight in parts) / sum(weight for _, weight in parts)
    prior = c.get("performance_prior")
    if prior is not None:
        # Learned historical format priors are deliberately low influence.
        base = base * 0.92 + float(prior) * 0.08
    learned = c.get("learned_score")
    if learned is not None:
        # Feedback-trained ranker is a small nudge, never a replacement for editorial judgement.
        base = base * 0.85 + float(learned) * 0.15
    return min(max(base, 0.0), 1.0)



def select_top(cands, n, min_gap):
    for c in cands:
        c["final"] = final_score(c)
    chosen = []
    for c in sorted(cands, key=lambda c: -c["final"]):
        if c.get("reject"):
            continue
        if all(c["end"] + min_gap <= k["start"] or c["start"] >= k["end"] + min_gap for k in chosen):
            chosen.append(c)
        if len(chosen) == n:
            break
    return chosen                                 # best first


def make_caption(cfg, ch, info, cand):
    ig = cfg["instagram"]
    first = cand.get("caption_hook") or cand.get("title") or info["title"]
    credit = f"\U0001F399\uFE0F {ch['name']}" + (f" ({ch['ig_handle']})" if ch.get("ig_handle") else "")
    tags = (cand.get("hashtags") or []) + list(ch.get("hashtags") or ig["hashtags"])
    tags = list(dict.fromkeys(t if t.startswith("#") else "#" + t for t in tags))[:ig["max_hashtags"]]
    parts = [first, "", credit, f"Full episode on YouTube: {info['webpage_url']}"]
    if ig.get("cta"):
        parts += ["", ig["cta"]]
    parts += ["", " ".join(tags)]
    return "\n".join(parts)[:2200]


# =============================================================================
# 3. PRODUCE (discover -> analyse -> queue; no video is downloaded here)
# =============================================================================
def understand_episode(cfg, info, words):
    """Build a compact episode map once; failures fall back to deterministic transcript signals."""
    if not cfg.get("intelligence", {}).get("episode_understanding", True):
        return {"summary": "", "topics": [], "moments": []}
    if not cfg.get("llm", {}).get("enabled") or not gemini_key():
        return {"summary": "", "topics": [], "moments": []}
    transcript = timed_transcript(
        words, bucket_seconds=20,
        max_chars=int(cfg.get("intelligence", {}).get("max_transcript_chars", 90000)))
    if not transcript:
        return {"summary": "", "topics": [], "moments": []}
    try:
        prompt = episode_understanding_prompt(info.get("title", ""), info.get("duration", 0), transcript)
        result = parse_json(gemini_text(cfg, [{"text": prompt}], temperature=0.1, max_tokens=5000))
        episode_map = validate_episode_map(result, float(info.get("duration") or 0))
        log.info("episode understanding: %d topics, %d semantic moments",
                 len(episode_map["topics"]), len(episode_map["moments"]))
        return episode_map
    except Exception as ex:
        log.warning("episode understanding unavailable; using transcript candidate fallback: %s",
                    str(ex)[:200])
        return {"summary": "", "topics": [], "moments": []}


def apply_editorial_policy(cfg, candidates):
    policy = cfg.get("intelligence", {}).get("promotion_policy", "manual_review")
    threshold = float(cfg.get("intelligence", {}).get("promotion_threshold", 0.72))
    for candidate in candidates:
        decision = editorial_gate(candidate, promotion_policy=policy, threshold=threshold)
        candidate["editorial_eligible"] = decision["eligible"]
        candidate["editorial_review_required"] = decision["review_required"]
        candidate["editorial_gate_reason"] = decision["reason"]
        if not decision["eligible"]:
            candidate["reject"] = True
    return candidates


def analyse_episode(cfg, ch, info, state=None, decision_sink=None):
    """Episode map -> semantic + transcript candidates -> editorial and video evaluation."""
    words = fetch_words(cfg, info)
    if not words:
        log.warning("no candidate moments: usable timestamped transcript is missing or too short")
        return []
    episode_map = understand_episode(cfg, info, words)
    heat = heat_per_second(info.get("heatmap") or [], info["duration"])
    cands = score_candidates(cfg, heat, words, info["duration"])
    semantic = semantic_candidate_windows(
        episode_map, words, cfg["clip"]["min_s"], cfg["clip"]["max_s"], info["duration"])
    for candidate in semantic:
        a, b = int(candidate["start"]), min(int(candidate["end"]), len(heat))
        seg = heat[a:max(a + 1, b)]
        heat_part = (0.55 * float(seg.mean()) + 0.20 * float(seg.max())
                     + 0.25 * float(seg[:5].mean())) if len(seg) else 0.5
        tokens = candidate["text"].split()
        hook = hook_score(" ".join(tokens[:18]))
        duration_s = max(candidate["end"] - candidate["start"], 1.0)
        density = min(len(tokens) / duration_s / 2.8, 1.0)
        candidate.update({"heat": heat_part, "hook": hook, "density": density,
                          "pre": 0.42 * heat_part + 0.40 * hook + 0.18 * density})
    # Retain the original heuristic windows and add semantically complete ideas/stories.
    existing = {(round(c["start"]), round(c["end"])) for c in cands}
    cands.extend(c for c in semantic
                 if (round(c["start"]), round(c["end"])) not in existing)
    if not cands:
        log.warning("no candidate moments after transcript and semantic analysis")
        return []
    attach_semantic_context(cands, episode_map)
    enrich_candidates_with_context(cands, words)
    for candidate in cands:
        editorial = (0.45 * candidate.get("standalone_heuristic", 0.5)
                     + 0.35 * candidate.get("payoff_heuristic", 0.5)
                     + 0.10 * (1.0 - candidate.get("context_dependency", 0.5))
                     + 0.10 * min(1.0, float(candidate.get("semantic_confidence", 0.0))))
        candidate["editorial_score"] = round(editorial, 4)
        candidate["pre"] = 0.68 * candidate["pre"] + 0.32 * editorial

    if state:
        priors = format_priors(
            state.get("clips", []),
            min_samples=int(cfg["learning"].get("min_format_samples", 4)),
        )
        apply_format_priors(cands, priors)

    cands.sort(key=lambda item: -item["pre"])
    cands = diversify(cands, cfg["llm"]["candidates"], cfg["clip"]["min_gap_s"],
                      variants=cfg["llm"].get("variants", 3) if gemini_key() else 1)
    cands = llm_rerank(cfg, cands, info["title"], episode_map)
    cands = video_verify(cfg, info["id"], cands)
    apply_editorial_policy(cfg, cands)
    ranker = train_editorial_ranker(
        state or {},
        min_samples=int(cfg.get("learning", {}).get("min_ranker_samples", 20)),
        min_per_class=int(cfg.get("learning", {}).get("min_ranker_per_class", 6)),
    )
    if ranker:
        for candidate in cands:
            candidate["learned_score"] = predict_editorial_score(candidate, ranker)
        log.info("feedback ranker active: %d labelled samples (%d positive, %d negative)",
                 ranker["samples"], ranker["positive_samples"], ranker["negative_samples"])
    picks = select_top(cands, cfg["queue"]["clips_per_episode"], cfg["clip"]["min_gap_s"])
    wt = [w[0] for w in words]
    for p in picks:
        i0, i1 = bisect_left(wt, p["start"]), bisect_left(wt, p["end"] + 0.2)
        p["words"] = list(words[i0:i1])
    polish_captions(cfg, picks, info["title"])
    if decision_sink is not None:
        for candidate in cands:
            candidate["learning_features"] = candidate_feature_record(candidate)
        record_candidate_decisions(
            {"candidate_decisions": decision_sink}, info.get("id"), ch.get("name"),
            cands, [(p["start"], p["end"]) for p in picks])
    return picks

def probe_download(cfg, vid, pick):
    """Plan -> verify: prove a 3 s section of this episode can really be downloaded BEFORE queueing it."""
    try:
        raw = download_section(cfg, vid, pick["start"], pick["start"] + 3.0, f"probe_{vid}")
        Path(raw).unlink(missing_ok=True)
        return True
    except Exception as ex:
        log.warning("download probe failed for %s: %s", vid, str(ex)[:200])
        return False


def produce(cfg, dry_run=False):
    """Pick an episode, analyse it, verify it is downloadable, queue its clips. If an episode turns out
    unusable, adapt: record why and move on to the next one (up to max_episode_attempts)."""
    state = load_state()
    if not authorized_channels(cfg):
        message = "No authorized channels enabled; discovery is paused. Enable permission: true only for a source you are licensed or authorized to clip."
        log.warning(message)
        jr(state, "discovery_paused", reason="no authorized channels enabled")
        if not dry_run:
            save_state(state)
        return 0
    for attempt in range(cfg["discovery"].get("max_episode_attempts", 3)):
        ch, info = pick_episode(cfg, state)
        if not info:
            gha("warning", "No suitable episode found (all scanned videos filtered, or YouTube blocked).")
            jr(state, "no_episode", attempt=attempt)
            if not dry_run:
                save_state(state)
            return 0
        log.info("episode: %s | %s (%ds, %s views)", ch["name"], info["title"], info["duration"],
                 info.get("view_count"))
        decision_sink = []
        picks = analyse_episode(cfg, ch, info, state=state, decision_sink=decision_sink)
        if not dry_run and decision_sink:
            decisions = state.setdefault("candidate_decisions", [])
            decisions.extend(decision_sink)
            del decisions[:-600]
        for p in picks:
            log.info("pick %s-%s final=%.3f heat=%.2f hook=%.2f llm=%s vid=%s | %s", hms(p["start"]),
                     hms(p["end"]), p["final"], p["heat"], p["hook"], p.get("llm"), p.get("vid"),
                     p["text"][:90])
        if dry_run:
            return len(picks)
        meta = {"channel": ch["name"], "title": info["title"], "url": info["webpage_url"],
                "views": info.get("view_count"), "processed_at": now_iso()}
        if not picks:
            retry = (now_utc() + timedelta(hours=12)).isoformat(timespec="seconds")
            state["episodes"][info["id"]] = {
                **meta, "clips": 0, "note": "no usable editorial candidates",
                "retry_after": retry,
            }
            jr(state, "episode_skipped", video=info["id"],
               reason="no usable editorial candidates; retry in 12h")
            save_state(state)
            continue
        if not probe_download(cfg, info["id"], picks[0]):
            retry = (now_utc() + timedelta(hours=12)).isoformat(timespec="seconds")
            state["episodes"][info["id"]] = {**meta, "clips": 0, "note": "download probe failed",
                                             "retry_after": retry}
            jr(state, "episode_skipped", video=info["id"], reason="download probe failed; retry in 12h")
            save_state(state)
            continue
        for p in picks:
            state["clips"].append({
                "id": state["next_id"], "video_id": info["id"], "channel": ch["name"],
                "start": round(p["start"], 2), "end": round(p["end"], 2), "score": round(p["final"], 4),
                "features": {k: round(p[k], 3) for k in (
                    "heat", "hook", "density", "editorial_score", "standalone_heuristic",
                    "payoff_heuristic", "context_dependency", "learned_score"
                ) if k in p}
                            | {k: round(p[k], 3) for k in ("llm", "vid") if k in p}
                            | {k: p[k] for k in ("hook_type",) if p.get(k)}
                            | {"duration_bucket": (
                                "15-19s" if p["end"] - p["start"] < 20 else
                                "20-29s" if p["end"] - p["start"] < 30 else
                                "30-39s" if p["end"] - p["start"] < 40 else "40s+"
                            )},
                "title": p.get("title", ""), "caption": make_caption(cfg, ch, info, p),
                "content_type": p.get("content_type") or p.get("semantic_kind") or "other",
                "promotion_type": p.get("promotion_type", "none"),
                "promotion_confidence": round(float(p.get("promotion_confidence", 0.0) or 0.0), 3),
                "promotion_evidence": p.get("promotion_evidence", ""),
                "editorial_review_required": bool(p.get("editorial_review_required")),
                "editorial_gate_reason": p.get("editorial_gate_reason", ""),
                "editorial_scores": p.get("editorial_scores", {}),
                "semantic_topic": p.get("semantic_topic", ""),
                "semantic_summary": p.get("semantic_summary", ""),
                "edit_plan": build_edit_plan(p, cfg),
                "polished": bool(p.get("polished")),
                "words": [[round(t, 2), w] for t, w in p["words"]],
                "status": "queued", "attempts": 0, "created_at": now_iso()})
            state["next_id"] += 1
        state["episodes"][info["id"]] = {**meta, "clips": len(picks)}
        jr(state, "episode_queued", video=info["id"], channel=ch["name"], clips=len(picks))
        save_state(state)
        log.info("queued %d clips", len(picks))
        return len(picks)
    return 0


def queued(state):
    """All queued clips, including ones held because source permission was revoked."""
    return [c for c in state.get("clips", []) if c.get("status") == "queued"]


def queued_for_authorized_channels(state, cfg):
    """Queued clips eligible for current publishing permissions.

    Keep revoked-source clips in state for auditability, but do not let them block
    refill of the active queue after a creator is removed from config.yaml.
    """
    allowed_names = {c["name"] for c in authorized_channels(cfg)}
    return [clip for clip in queued(state) if clip.get("channel") in allowed_names]


def retry_transient_promotion_reviews(state, max_retries=2, now=None):
    """Requeue only transient promotion-screen API failures after a cooldown.

    Uncertain classifications, possible promotions, final-video QA failures, and
    technical failures remain held for human review. Retries are bounded.
    """
    now = now or datetime.now(timezone.utc)
    changed = 0
    for clip in state.get("clips", []):
        if clip.get("status") != "needs_review":
            continue
        if not clip.get("promotion_screen_error"):
            continue
        if clip.get("editorial_review_required") or clip.get("editorial_gate_reason"):
            continue
        retries = int(clip.get("promotion_retry_count", 0))
        if retries >= max_retries:
            continue
        retry_after = clip.get("promotion_retry_after")
        if not retry_after:
            continue
        try:
            due = datetime.fromisoformat(retry_after)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if due > now:
            continue
        clip.update(status="queued", promotion_retry_count=retries + 1,
                    promotion_retry_after=None, error=None)
        clip.pop("qa", None)
        jr(state, "promotion_screen_retry_queued", clip=clip.get("id"),
           retry=clip["promotion_retry_count"])
        log.info("Requeued clip %s for bounded promotion-screen retry (%d/%d).",
                 clip.get("id"), clip["promotion_retry_count"], max_retries)
        changed += 1
    return changed


# =============================================================================
# 4. RENDER (happens at post time)
# =============================================================================
def ass_time(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def group_words(ws, max_words=4, max_gap=0.7):
    """[(t, word)] -> caption groups. Breaks at sentence punctuation, pauses and every max_words."""
    groups, cur = [], []
    for t, w in ws:
        if cur and t - cur[-1][0] > max_gap:
            groups.append(cur)
            cur = []
        cur.append((t, w))
        if len(cur) >= max_words or w[-1:] in ".?!\u0964":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def clean_cap(w):
    return w.replace("{", "").replace("}", "").replace("\\", "").replace("\n", " ").upper()


def build_ass(words, start, end, title, path, edit_plan=None):
    """Render word-synchronised captions using a validated content-specific caption plan."""
    plan = validate_edit_plan(edit_plan or {})
    ws = [(t - start, w) for t, w in words if start - 0.05 <= t < end]
    font_size = plan["caption_font_size"]
    margin_v = plan["caption_margin_v"]
    highlight = "&H0000D7FF&" if plan["caption_style"] in ("emphasis", "kinetic") else "&H0000E5FF&"
    title_duration = min(float(plan.get("title_duration_s", 2.8)), max(end - start, 0.8))
    caption_animation = plan.get("caption_animation", "pop")
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
        "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,Noto Sans,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,"
        f"0,0,1,6,2,2,60,60,{margin_v},1\n"
        "Style: Title,Noto Sans,64,&H0000E5FF,&H000000FF,&H00000000,&H64000000,1,0,0,0,100,100,"
        "0,0,1,6,2,8,70,70,360,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    if title:
        wrapped = "\\N".join(textwrap.wrap(clean_cap(title), 22)[:3])
        title_text = wrapped
        if caption_animation == "fade":
            title_text = "{\\fad(180,220)}" + title_text
        elif caption_animation == "pop":
            title_text = "{\\fscx92\\fscy92\\t(0,180,\\fscx100\\fscy100)}" + title_text
        lines.append(f"Dialogue: 1,{ass_time(0)},{ass_time(title_duration)},Title,,0,0,0,,{title_text}")
    groups = group_words(ws, max_words=plan["caption_group_words"])
    for gi, g in enumerate(groups):
        next_start = groups[gi + 1][0][0] if gi + 1 < len(groups) else None
        for i, (t, w) in enumerate(g):
            if i + 1 < len(g):
                en = min(g[i + 1][0], t + 1.0)
            else:
                en = t + min(0.9, max(0.4, 0.07 * len(w) + 0.25))
                if next_start is not None:
                    en = min(en, next_start)
            en = max(en, t + 0.08)
            text = " ".join(
                (f"{{\\c{highlight}}}{{\\fscx112\\fscy112\\t(0,120,\\fscx100\\fscy100)}}"
                 if caption_animation == "pop" and j == i else
                 f"{{\\c{highlight}}}" if j == i else "")
                + clean_cap(x) + ("{\\c&H00FFFFFF&}" if j == i else "")
                for j, (_, x) in enumerate(g))
            lines.append(f"Dialogue: 0,{ass_time(t)},{ass_time(en)},Default,,0,0,0,,{text}")
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")


def source_url(video_id):
    """Where to cut from. CLIPPER_SOURCE_URL (a local file) is a test hook, never set in production."""
    return env_value("CLIPPER_SOURCE_URL") or f"https://www.youtube.com/watch?v={video_id}"


def _probe_number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _probe_frame_rate(value):
    try:
        if isinstance(value, str) and "/" in value:
            numerator, denominator = value.split("/", 1)
            denominator = float(denominator)
            return float(numerator) / denominator if denominator else None
        return _probe_number(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def probe_media(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                        "-show_format", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("ffprobe failed: " + r.stderr[-200:])
    j = json.loads(r.stdout)
    v = [stream for stream in j["streams"] if stream["codec_type"] == "video"]
    a = [stream for stream in j["streams"] if stream["codec_type"] == "audio"]
    video, audio = (v[0] if v else {}), (a[0] if a else {})
    fmt = j.get("format") or {}
    return {
        "duration": float(fmt.get("duration") or 0),
        "w": video.get("width", 0), "h": video.get("height", 0),
        "has_video": bool(v), "has_audio": bool(a),
        "format_name": fmt.get("format_name", ""),
        "video_codec": video.get("codec_name"),
        "video_pix_fmt": video.get("pix_fmt"),
        "video_fps": _probe_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
        "video_bitrate": _probe_number(video.get("bit_rate")),
        "audio_codec": audio.get("codec_name"),
        "audio_sample_rate": _probe_number(audio.get("sample_rate")),
        "audio_channels": audio.get("channels"),
        "audio_bitrate": _probe_number(audio.get("bit_rate")),
        "size_bytes": _probe_number(fmt.get("size")),
    }


def instagram_media_issues(media):
    """Validate an exported file against the Instagram Graph API Reel media profile."""
    issues = []
    if "mp4" not in str(media.get("format_name") or "").lower():
        issues.append("container must be MP4")
    if media.get("video_codec") not in ("h264", "hevc"):
        issues.append("video codec must be H.264 or HEVC")
    if media.get("video_pix_fmt") != "yuv420p":
        issues.append("video pixel format must be 4:2:0 yuv420p")
    fps = media.get("video_fps")
    if fps is None or not 23 <= fps <= 60:
        issues.append("video frame rate must be between 23 and 60 fps")
    if media.get("video_bitrate") is not None and media["video_bitrate"] > 25_000_000:
        issues.append("video bitrate exceeds 25 Mbps")
    if media.get("audio_codec") != "aac":
        issues.append("audio codec must be AAC")
    sample_rate = media.get("audio_sample_rate")
    if sample_rate is None or not 0 < sample_rate <= 48_000:
        issues.append("audio sample rate must be at most 48 kHz")
    channels = media.get("audio_channels")
    if channels not in (1, 2):
        issues.append("audio must be mono or stereo")
    if media.get("audio_bitrate") is not None and media["audio_bitrate"] > 140_000:
        issues.append("audio bitrate exceeds the 128 kbps target")
    if media.get("size_bytes") is not None and media["size_bytes"] > 250 * 1024 * 1024:
        issues.append("file exceeds the clipper 250 MB safety limit")
    return issues

DL_FORMAT = "bv*[height<=1080]+ba/b[height<=1080]"
DL_ANY_FORMAT = "bv*+ba/b"
DL_STRATEGIES = [                                  # tried in order; the last one that worked goes first
    # The current yt-dlp PO-token guide recommends mweb with a PO-token provider.
    ("mweb-client", {"youtube": {"player_client": ["mweb"]}}, DL_FORMAT),
    # web_safari can expose HLS formats that do not currently need a GVS PO token.
    ("web-safari-client", {"youtube": {"player_client": ["web_safari"]}}, DL_FORMAT),
    ("default", None, DL_FORMAT),
    # Do not restrict the last resort to a single combined format: many videos expose
    # separate video/audio streams only.
    ("any-format", None, DL_ANY_FORMAT),
]
_STRATEGY_CACHE = {}


def download_section(cfg, vid, s, e, stem):
    """Fetch ONLY [s, e]. Walks a ladder of yt-dlp strategies; every result is verified with ffprobe."""
    WORK.mkdir(exist_ok=True)
    src = source_url(vid)
    local = Path(src.removeprefix("file://")) if (src.startswith("file://") or Path(src).exists()) else None
    if local:                                       # test hook: cut a local file with ffmpeg
        out = WORK / f"{stem}_raw.mp4"
        subprocess.run(["ffmpeg", "-y", "-ss", f"{s:.3f}", "-i", str(local), "-t", f"{e - s:.3f}",
                        "-c:v", "libx264", "-c:a", "aac", str(out)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out

    last = None
    for name, extractor_args, fmt in sorted(DL_STRATEGIES, key=lambda x: x[0] != _STRATEGY_CACHE.get("ok")):
        for old in WORK.glob(f"{stem}_raw*"):
            old.unlink()
        opts = ydl_opts(cfg, format=fmt, merge_output_format="mp4",
                        outtmpl=str(WORK / f"{stem}_raw.%(ext)s"),
                        download_ranges=download_range_func(None, [(s, e)]),
                        force_keyframes_at_cuts=True)
        if extractor_args:
            opts["extractor_args"] = extractor_args
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                y.download([src])
            raws = sorted(WORK.glob(f"{stem}_raw*"), key=lambda p: p.stat().st_mtime)
            if not raws:
                raise RuntimeError("section download produced no file")
            m = probe_media(raws[-1])
            want = e - s
            if not (m["has_video"] and m["has_audio"]):
                raise RuntimeError(f"download missing a stream: {m}")
            if not (want - 2.0 <= m["duration"] <= want + 4.0):
                raise RuntimeError(f"section length {m['duration']:.1f}s, expected ~{want:.1f}s")
            if _STRATEGY_CACHE.get("ok") != name:
                log.info("download strategy that works: %s", name)
            _STRATEGY_CACHE["ok"] = name
            return raws[-1]
        except Exception as ex:
            last = ex
            log.warning("download strategy %s failed: %s", name, str(ex)[:200])
    raise RuntimeError(f"could not download section (tried {len(DL_STRATEGIES)} strategies): {last}")


def detect_face_crop_points(cfg, raw, speaker_analysis=None):
    """Track a single visible face across sampled frames and return safe crop keyframes.

    Multiple faces or ambiguous dialogue deliberately use the blurred full-frame fallback.
    The track is visual, not face identity recognition or a claim about who is speaking.
    """
    if not cfg.get("render", {}).get("face_reframe", True):
        return None
    speaker_analysis = speaker_analysis or {}
    if (speaker_analysis.get("speaker_count") not in (None, 1)
            and speaker_analysis.get("dominant_speaker_ratio", 0.0) < 0.75):
        log.info("face reframing skipped: multiple active speakers share this clip")
        return None
    try:
        import cv2
    except ImportError:
        log.warning("OpenCV is unavailable; using the full-frame vertical layout")
        return None

    media = probe_media(raw)
    width, height, duration = media.get("w", 0), media.get("h", 0), media.get("duration", 0)
    if not media.get("has_video") or width <= 0 or height <= 0 or duration <= 0:
        return None
    scaled_width = int(round((width * 1920 / height) / 2) * 2)
    if scaled_width <= 1080 or width / height < 0.85:
        return None

    detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if detector.empty():
        log.warning("OpenCV face detector could not be loaded; using full-frame layout")
        return None

    sample_count = max(8, min(24, int(np.ceil(duration / 0.8))))
    timestamps = np.linspace(min(0.15, duration / 10), max(0.15, duration - 0.15), sample_count)
    capture = cv2.VideoCapture(str(raw))
    if not capture.isOpened():
        return None

    detected, multi_face_frames, readable_frames = [], 0, 0
    try:
        for stamp in timestamps:
            capture.set(cv2.CAP_PROP_POS_MSEC, float(stamp) * 1000)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            readable_frames += 1
            frame_h, frame_w = frame.shape[:2]
            scale = min(1.0, 640.0 / max(frame_w, 1))
            if scale < 1.0:
                frame = cv2.resize(frame, (int(frame_w * scale), int(frame_h * scale)))
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5,
                                              minSize=(28, 28))
            if len(faces) == 1:
                x, y, fw, fh = faces[0]
                center_x_original = (x + fw / 2) / scale
                center_scaled = center_x_original * 1920 / height
                crop_x = int(round(center_scaled - 540))
                crop_x = min(max(crop_x, 0), scaled_width - 1080)
                detected.append((float(stamp), float(crop_x)))
            elif len(faces) > 1:
                multi_face_frames += 1
    except Exception as ex:
        log.warning("face-aware tracking failed; using full-frame layout: %s", str(ex)[:180])
        return None
    finally:
        capture.release()

    if readable_frames < max(5, int(sample_count * 0.55)):
        return None
    if len(detected) < max(5, int(readable_frames * 0.55)):
        return None
    if multi_face_frames > max(1, readable_frames * 0.20):
        return None

    # Median smooth nearby samples to suppress Haar-detector jitter.
    points = []
    for i, (stamp, _) in enumerate(detected):
        nearby = [x for t, x in detected if abs(t - stamp) <= 0.9]
        points.append((stamp, float(np.median(nearby))))
    log.info("tracked one visible face across %d/%d samples", len(points), readable_frames)
    return {"points": points, "max_x": scaled_width - 1080}


def face_crop_expression(track):
    """Build an FFmpeg crop-x expression with linear interpolation between face keyframes."""
    if not track or len(track.get("points") or []) < 2:
        return None
    points = [(float(t), max(0.0, min(float(x), float(track["max_x"]))))
              for t, x in track["points"]]
    tail = f"2*round({points[-1][1]:.3f}/2)"
    for i in range(len(points) - 2, -1, -1):
        t0, x0 = points[i]
        t1, x1 = points[i + 1]
        dt = max(t1 - t0, 1e-3)
        linear = f"2*round(({x0:.3f}+({x1 - x0:.3f})*(t-{t0:.3f})/{dt:.3f})/2)"
        tail = f"if(lt(t\\,{t1:.3f})\\,{linear}\\,{tail})"
    t_first, x_first = points[0]
    return f"if(lt(t\\,{t_first:.3f})\\,2*round({x_first:.3f}/2)\\,{tail})"


def detect_face_crop_x(cfg, raw):
    """Backward-compatible static crop helper used by focused unit tests."""
    track = detect_face_crop_points(cfg, raw)
    if not track:
        return None
    return int(round(float(np.median([x for _, x in track["points"]]))))



def compose_clip(cfg, raw, clip, s, e, stem):
    """1080x1920 composition with tracked portrait crop or safe blurred-background fallback."""
    # A single consistently detected face is followed over time. We deliberately avoid
    # face-identity claims and fall back when group/dialogue shots are ambiguous.
    fg_scale = float(cfg.get("render", {}).get("foreground_scale", 1.16))
    fg_scale = min(max(fg_scale, 1.0), 1.35)
    fg_width = int(1080 * fg_scale)
    edit_plan = validate_edit_plan(clip.get("edit_plan") or {})
    face_track = (detect_face_crop_points(cfg, raw, clip.get("speaker_analysis"))
                  if edit_plan["crop_strategy"] == "face_track" else None)
    crop_expr = face_crop_expression(face_track)
    if crop_expr:
        fc = (f"[0:v]scale=-2:1920,crop=1080:1920:'{crop_expr}':0,"
              "setsar=1,eq=contrast=1.02:saturation=1.04[v0]")
    else:
        fc = ("[0:v]split=2[a][b];"
              "[a]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
              "boxblur=8:3,scale=1080:1920,eq=brightness=-0.08[bg];"
              f"[b]scale={fg_width}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]")
    video_label = "v0"
    if edit_plan["motion"] == "subtle":
        # A restrained 4% punch-in is applied only to story/conversation plans.
        fc += ";[v0]scale=1124:1998:flags=lanczos,crop=1080:1920:(in_w-out_w)/2:(in_h-out_h)/2[vzoom]"
        video_label = "vzoom"
    if cfg["clip"]["captions"] and (clip.get("words") or clip.get("title")):
        build_ass(clip.get("words") or [], s, e, clip.get("title"),
                  WORK / f"{stem}.ass", edit_plan=edit_plan)
        fc += f";[{video_label}]subtitles={stem}.ass[v]"
    else:
        fc += f";[{video_label}]null[v]"
    out = WORK / f"{stem}.mp4"
    cmd = ["ffmpeg", "-y", "-i", Path(raw).name, "-filter_complex", fc, "-map", "[v]", "-map", "0:a:0",
           "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "20", "-maxrate", "20M", "-bufsize", "40M", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "128k",
           "-ar", "48000", "-ac", "2", "-movflags", "+faststart", "-t", f"{e - s:.2f}", out.name]
    r = subprocess.run(cmd, cwd=WORK, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode:
        raise RuntimeError("ffmpeg failed: " + r.stderr.decode(errors="ignore")[-400:])
    m = probe_media(out)
    if (m["w"], m["h"]) != (1080, 1920) or not m["has_audio"]:
        raise RuntimeError(f"bad output {m}")
    if not (3 <= m["duration"] <= 90) or abs(m["duration"] - (e - s)) > 1.0:
        raise RuntimeError(f"output duration {m['duration']:.1f}s, expected ~{e - s:.1f}s")
    compatibility_issues = instagram_media_issues(m)
    if compatibility_issues:
        raise RuntimeError("Instagram media compatibility failed: " + "; ".join(compatibility_issues))
    if out.stat().st_size > 250 * 1024 * 1024:
        raise RuntimeError("output larger than 250 MB")
    return out


def render_clip(cfg, clip):
    """Download only [start,end], then compose the vertical clip. Raises on any bad result."""
    vid, s, e = clip["video_id"], clip["start"], clip["end"] + 0.15
    stem = f"{vid}_{int(s)}"
    WORK.mkdir(exist_ok=True)
    for old in WORK.glob(f"{stem}*"):
        old.unlink()
    raw = download_section(cfg, vid, s, e, stem)
    diarization_cfg = cfg.get("diarization", {})
    if diarization_cfg.get("enabled"):
        try:
            from speaker_diarization import diarize_media
            turns = diarize_media(raw, env_value("HF_TOKEN") or None)
            clip["speaker_analysis"] = speaker_summary(turns, 0.0, e - s)
            clip.setdefault("features", {}).update(clip["speaker_analysis"])
            speaker_count = clip["speaker_analysis"].get("speaker_count")
            if isinstance(speaker_count, int):
                clip["features"]["speaker_count_bucket"] = (
                    "1" if speaker_count == 1 else "2" if speaker_count == 2 else "3+"
                )
        except Exception as ex:
            log.warning("speaker analysis skipped: %s", str(ex)[:200])
    out = compose_clip(cfg, raw, clip, s, e, stem)
    Path(raw).unlink(missing_ok=True)
    (WORK / f"{stem}.ass").unlink(missing_ok=True)
    return out


# =============================================================================
# 4b. QA AGENT: reviews the finished clip before anything is posted
# =============================================================================
def qa_checks(cfg, path):
    """Fail-closed deterministic media checks: black frames, silence and FFmpeg execution."""
    q = cfg.get("qa", {})
    media = probe_media(path)
    dur = media["duration"] or 1.0
    if not media.get("has_video") or not media.get("has_audio"):
        raise RuntimeError(f"QA input is missing a media stream: {media}")
    r = subprocess.run(
        ["ffmpeg", "-v", "info", "-i", str(path),
         "-vf", "blackdetect=d=0.4:pix_th=0.08",
         "-af", "silencedetect=noise=-42dB:d=0.8", "-f", "null", "-"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg technical QA failed: " + (r.stderr or "")[-500:])
    stderr = r.stderr or ""
    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", stderr))
    silence_durations = [float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", stderr)]
    silence = sum(silence_durations)
    max_silence = max(silence_durations, default=0.0)
    black_pct = black / dur * 100
    silence_pct = silence / dur * 100
    issues = []
    if black_pct > q.get("max_black_pct", 12):
        issues.append(f"{black_pct:.0f}% of the clip is black frames")
    if silence_pct > q.get("max_silence_pct", 25):
        issues.append(f"{silence_pct:.0f}% of the clip is silent")
    if max_silence > q.get("max_silence_segment_s", 2.5):
        issues.append(f"continuous silence lasts {max_silence:.1f}s")
    return {"black_pct": round(black_pct, 1), "silence_pct": round(silence_pct, 1),
            "max_silence_s": round(max_silence, 2), "issues": issues}



QA_PROMPT = (
    "You are the final QA editor for an Instagram Reel cut from a podcast. Watch it WITH SOUND. Check: "
    "(1) it starts and ends cleanly, not in the middle of a word or sentence; (2) a viewer with no prior "
    "context can understand the setup and payoff; (3) the first 1-3 seconds earn attention without "
    "misleading clickbait; (4) captions match the speech, are readable and synchronised; (5) the title "
    "matches the actual spoken content; (6) framing, cuts and visual emphasis suit the type of conversation; "
    "(7) audio is clear and balanced; (8) nothing unsafe: hate, harassment, explicit content, dangerous "
    "misinformation or defamation. Note an obviously promotional segment in issues when the editorial "
    "context suggests it, but do not infer an advertisement from a passing product mention alone. "
    'Reply with ONLY JSON: {"approve":true,"score":0,"unsafe":false,"issues":[""]}. '
    "approve=false only for serious quality or safety problems. score is 0-10 overall quality.")


def gemini_qa(cfg, path, clip=None):
    """Gemini watches the final render with its editorial intent and edit plan as context."""
    if not (cfg["llm"].get("final_qa") and gemini_key()):
        return None
    prev = Path(path).with_name(Path(path).stem + "_preview.mp4")
    try:
        subprocess.run(["ffmpeg", "-y", "-i", str(path), "-vf", "scale=360:640", "-r", "12", "-c:v", "libx264",
                        "-preset", "veryfast", "-crf", "33", "-c:a", "aac", "-b:a", "48k", "-ac", "1",
                        "-t", "45", str(prev)], check=True, capture_output=True)
        if prev.stat().st_size > 14 * 1024 * 1024:
            return None
        data = base64.b64encode(prev.read_bytes()).decode()
        clip = clip or {}
        context = {
            "content_type": clip.get("content_type"),
            "promotion_type": clip.get("promotion_type"),
            "promotion_confidence": clip.get("promotion_confidence"),
            "edit_plan": clip.get("edit_plan"),
        }
        d = parse_json(gemini_text(cfg, [
            {"inlineData": {"mimeType": "video/mp4", "data": data}},
            {"text": QA_PROMPT + "\\nEditorial context (verify against the video): "
                    + json.dumps(context, ensure_ascii=False)}
        ]))
        if isinstance(d, list) and d:
            d = d[0]
        if not isinstance(d, dict):
            log.warning("Gemini final QA returned an invalid JSON object")
            return None
        approve, unsafe = d.get("approve"), d.get("unsafe")
        score = _score(d.get("score"))
        issues = d.get("issues")
        if type(approve) is not bool or type(unsafe) is not bool or score is None or not isinstance(issues, list):
            log.warning("Gemini final QA response is missing required typed fields")
            return None
        return {"approve": approve, "score": score, "unsafe": unsafe,
                "issues": [str(x) for x in issues if str(x).strip()][:5]}
    except Exception as ex:
        log.warning("Gemini QA unavailable, continuing without it: %s", str(ex)[:200])
        return None
    finally:
        prev.unlink(missing_ok=True)


def qa_review(cfg, path, clip=None):
    """-> {'verdict': pass | soft_fail | hard_fail, 'score', 'issues'}.
    hard_fail = never post (technical problem or unsafe). soft_fail = review is required."""
    try:
        chk = qa_checks(cfg, path)
    except Exception as ex:
        # Fail closed: if the technical gate itself failed, do not silently treat the
        # render as approved. This prevents broken output from reaching the publisher.
        log.warning("technical QA could not run: %s", ex)
        return {"verdict": "hard_fail", "score": 0,
                "issues": [f"technical QA could not complete: {str(ex)[:180]}"]}
    if chk["issues"]:
        return {"verdict": "hard_fail", "score": 0, "issues": chk["issues"]}
    g = gemini_qa(cfg, path, clip)
    if g is None:
        policy = cfg.get("qa", {}).get("ai_unavailable", "manual_review")
        if policy == "technical_only":
            return {"verdict": "pass", "score": None,
                    "issues": ["AI review unavailable; technical checks only"]}
        return {"verdict": "soft_fail", "score": None,
                "issues": ["AI final review unavailable; manual review required"]}
    if g["unsafe"]:
        return {"verdict": "hard_fail", "score": g["score"], "issues": ["unsafe content"] + g["issues"]}
    min_score = float(cfg.get("qa", {}).get("min_ai_score", 6.5))
    fallback_score = float(cfg.get("qa", {}).get("fallback_min_ai_score", 5.5))
    # Best-available fallback is ONLY for Gemini-approved, non-unsafe clips.
    # Gemini approve=false, technical failures, unsafe content, and missing QA remain blocked.
    if not g["approve"]:
        return {"verdict": "soft_fail", "score": g["score"],
                "issues": g["issues"] or ["Gemini did not approve this clip"]}
    if g["score"] < fallback_score:
        return {"verdict": "soft_fail", "score": g["score"],
                "issues": g["issues"] or [f"AI quality score below fallback floor {fallback_score:.1f}/10"]}
    if g["score"] < min_score:
        return {"verdict": "pass", "score": g["score"],
                "issues": g["issues"] or [f"Best-available fallback: score {g['score']:.1f}/10 "
                                          f"is below preferred threshold {min_score:.1f}/10"],
                "fallback": True}
    return {"verdict": "pass", "score": g["score"], "issues": g["issues"], "fallback": False}


# =============================================================================
# 5. PUBLISH (Instagram Graph API, resumable upload) + insights
# =============================================================================
class PublishUncertainError(RuntimeError):
    """The remote publish may have succeeded but its response was lost."""


class InstagramProcessingFailedError(RuntimeError):
    """Instagram's media processor rejected the uploaded video bytes."""


def _instagram_error_payload(response):
    try:
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except (ValueError, AttributeError):
        return {}


def _instagram_error_type(response):
    payload = _instagram_error_payload(response)
    return str((payload.get("debug_info") or {}).get("type")
               or (payload.get("error") or {}).get("type") or "")


def media_summary(media):
    """Safe diagnostic summary of a media file; never includes credentials or upload URLs."""
    return (
        f"{media.get('w')}x{media.get('h')} {media.get('video_codec')}/"
        f"{media.get('video_pix_fmt')} {media.get('video_fps')}fps "
        f"{media.get('audio_codec')} {media.get('audio_sample_rate')}Hz/"
        f"{media.get('audio_channels')}ch duration={media.get('duration')}s "
        f"size={media.get('size_bytes')}B"
    )


def normalize_reel_for_instagram(video_path):
    """Create one conservative H.264/AAC compatibility encode after a processing rejection."""
    source = Path(video_path)
    output = source.with_name(source.stem + "_instagram_compat.mp4")
    output.unlink(missing_ok=True)
    command = [
        "ffmpeg", "-y", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0",
        "-vf", (
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p"
        ),
        "-c:v", "libx264", "-profile:v", "high", "-level:v", "4.1",
        "-preset", "medium", "-crf", "22", "-maxrate", "8M", "-bufsize", "16M",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "128k",
        "-ar", "48000", "-ac", "2",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
        str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        detail = (result.stderr or "")[-500:]
        raise RuntimeError("Instagram compatibility re-encode failed: " + detail)
    media = probe_media(output)
    issues = instagram_media_issues(media)
    if (media.get("w"), media.get("h")) != (1080, 1920):
        issues.append("normalised output is not 1080x1920")
    if issues:
        output.unlink(missing_ok=True)
        raise RuntimeError(
            "normalised Instagram media did not pass validation: "
            + "; ".join(issues) + " (" + media_summary(media) + ")"
        )
    log.warning("created Instagram compatibility encode: %s", media_summary(media))
    return output


def upload_reel_container(cfg, video_path, caption):
    """Upload and process a Reel container, but do NOT publish it.

    Shared by the production publisher and the check-live smoke test. A FINISHED
    container is still unpublished until media_publish is explicitly called.
    """
    ig_id, token = env_value("IG_USER_ID"), env_value("IG_ACCESS_TOKEN")
    if not ig_id or not token:
        raise RuntimeError("Instagram upload requires non-empty IG_USER_ID and IG_ACCESS_TOKEN secrets")
    path = Path(video_path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"Instagram upload input is missing or empty: {path}")
    g = graph(cfg)

    def upload_and_wait(candidate_path):
        response = requests.post(f"{g}/{ig_id}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": "true", "access_token": token}, timeout=60)
        if not response.ok:
            raise RuntimeError(
                f"container create failed: HTTP {response.status_code} {response.text[:300]}")
        container = _instagram_error_payload(response)
        if not container:
            try:
                container = response.json()
            except ValueError as ex:
                raise RuntimeError("Instagram container create returned invalid JSON") from ex
        container_id, upload_uri = container.get("id"), container.get("uri")
        if not container_id or not upload_uri:
            raise RuntimeError(f"Instagram container response missing id or upload URI: {container}")

        data = candidate_path.read_bytes()
        upload = requests.post(upload_uri, data=data, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0",
            "file_size": str(len(data)), "Content-Type": "application/octet-stream"})
        if not upload.ok:
            error = f"HTTP {upload.status_code} {upload.text[:400]}"
            if _instagram_error_type(upload) == "ProcessingFailedError":
                raise InstagramProcessingFailedError(error)
            raise RuntimeError("Instagram resumable upload failed: " + error)

        for _ in range(72):
            status_response = requests.get(f"{g}/{container_id}", timeout=30, params={
                "fields": "status_code,status", "access_token": token})
            if not status_response.ok:
                raise RuntimeError(
                    f"Instagram processing status failed: HTTP {status_response.status_code} "
                    f"{status_response.text[:200]}")
            try:
                status = status_response.json()
            except ValueError as ex:
                raise RuntimeError("Instagram processing status returned invalid JSON") from ex
            status_code = status.get("status_code")
            if status_code == "FINISHED":
                return str(container_id)
            if status_code in ("ERROR", "EXPIRED"):
                error = json.dumps(status, ensure_ascii=False)[:400]
                if "ProcessingFailedError" in error:
                    raise InstagramProcessingFailedError(error)
                raise RuntimeError(f"Instagram processing failed: {error}")
            if status_code not in ("IN_PROGRESS", "PENDING"):
                raise RuntimeError(f"Instagram returned unexpected container status: {status}")
            time.sleep(5)
        raise TimeoutError("Instagram processing timed out")

    try:
        return upload_and_wait(path)
    except InstagramProcessingFailedError as original_error:
        # The original container cannot be repaired in-place. Re-encode the file and
        # create a fresh container once; never retry the same rejected bytes.
        try:
            original_media = probe_media(path)
            log.warning("Instagram rejected original upload (%s); media=%s",
                        str(original_error)[:300], media_summary(original_media))
        except Exception:
            log.warning("Instagram rejected original upload (%s)", str(original_error)[:300])
        compatible = None
        try:
            compatible = normalize_reel_for_instagram(path)
            return upload_and_wait(compatible)
        except Exception as fallback_error:
            raise RuntimeError(
                "Instagram rejected the original file and the single compatibility retry failed. "
                f"Original rejection: {str(original_error)[:250]}; "
                f"fallback: {str(fallback_error)[:400]}"
            ) from fallback_error
        finally:
            if compatible is not None:
                compatible.unlink(missing_ok=True)


def post_reel(cfg, video_path, caption):
    container_id = upload_reel_container(cfg, video_path, caption)
    ig_id, token = env_value("IG_USER_ID"), env_value("IG_ACCESS_TOKEN")
    g = graph(cfg)
    try:
        p = requests.post(f"{g}/{ig_id}/media_publish", timeout=60, data={
            "creation_id": container_id, "access_token": token})
    except requests.RequestException as ex:
        raise PublishUncertainError(f"media_publish request outcome unknown: {ex}") from ex
    if p.status_code >= 500:
        raise PublishUncertainError(f"media_publish server error may have an unknown outcome: HTTP {p.status_code} {p.text[:200]}")
    if not p.ok:
        raise RuntimeError(f"publish rejected by Instagram: HTTP {p.status_code} {p.text[:300]}")
    try:
        media_id = p.json().get("id")
    except (ValueError, AttributeError):
        media_id = None
    if not media_id:
        raise PublishUncertainError("Instagram accepted publish request but returned no media id")
    return media_id


def due_slot(cfg, state, now=None):
    """The posting slot that still needs a post -> (slot_datetime, seconds_until_slot), or None.
    Looks `max_wait_min` ahead (the runner starts early and waits) and `catchup_hours` back, so a late,
    skipped or failed GitHub cron run is healed by the next one. A post near a slot counts as that slot's post."""
    tz = ZoneInfo(cfg["timezone"])
    now = now or datetime.now(tz)
    sch = cfg["schedule"]
    wait_s, catch_s = sch["max_wait_min"] * 60, sch["catchup_hours"] * 3600
    posted = []
    for c in state["clips"]:
        stamp = c.get("posted_at") or c.get("publish_started_at")
        if c.get("status") in ("posted", "publishing", "publish_uncertain") and stamp:
            posted.append(datetime.fromisoformat(stamp))
    due = []
    weekly = cfg.get("weekly_post_times") or {}
    for day in (-1, 0, 1):
        date = (now + timedelta(days=day)).date()
        weekday_name = date.strftime("%A")
        slots = weekly.get(weekday_name, cfg.get("post_times", []))
        for hhmm in slots:
            h, m = map(int, hhmm.split(":"))
            slot = datetime.combine(date, datetime.min.time(), tzinfo=tz).replace(
                hour=h, minute=m, second=0, microsecond=0)
            ahead = (slot - now).total_seconds()
            if not -catch_s <= ahead <= wait_s:
                continue
            lo, hi = slot - timedelta(seconds=wait_s + 300), slot + timedelta(seconds=catch_s)
            if any(lo <= p <= hi for p in posted):
                continue
            due.append((slot, ahead))
    return min(due, key=lambda x: x[0]) if due else None


def _fail(state, clip, ex, stage):
    clip["attempts"] += 1
    clip["error"] = f"{stage}: {str(ex)[:350]}"
    if clip["attempts"] >= 3:
        clip["status"] = "failed"
    jr(state, "clip_failed", clip=clip["id"], stage=stage, error=clip["error"][:200])
    save_state(state)
    gha("error", f"Clip {clip['id']} failed at {stage} (attempt {clip['attempts']}): {clip['error']}")
    log.error("clip %s failed at %s: %s", clip["id"], stage, ex)


def _post_clip(cfg, state, clip, path):
    # Persist intent before calling Instagram. A timeout during media_publish is ambiguous
    # and must never be retried blindly; definite pre-publish/API rejections can be retried.
    clip.update(status="publishing", publish_started_at=now_iso(), error=None)
    jr(state, "publish_started", clip=clip["id"], channel=clip["channel"])
    save_state(state)
    try:
        media_id = post_reel(cfg, path, clip["caption"])
    except PublishUncertainError as ex:
        clip.update(status="publish_uncertain", error=f"Instagram outcome uncertain: {str(ex)[:300]}")
        jr(state, "publish_uncertain", clip=clip["id"], error=clip["error"][:200])
        save_state(state)
        gha("warning", f"Clip {clip['id']} has an uncertain Instagram publish outcome; not retrying automatically.")
        log.error("clip %s publish outcome uncertain: %s", clip["id"], ex)
        return False
    except Exception as ex:
        clip.pop("publish_started_at", None)
        clip["status"] = "queued"
        _fail(state, clip, ex, "instagram")
        return None
    instagram_posted_at = now_iso()
    clip.update(status="posted", media_id=media_id, posted_at=instagram_posted_at, error=None)
    clip.pop("publish_started_at", None)
    clip.setdefault("platforms", {})["instagram"] = {
        "status": "posted", "media_id": media_id, "url": f"https://www.instagram.com/reel/{media_id}/",
        "posted_at": instagram_posted_at,
    }
    clip.pop("words", None)
    jr(state, "posted", clip=clip["id"], media=media_id, channel=clip["channel"],
       qa=(clip.get("qa") or {}).get("verdict"))
    save_state(state)

    # YouTube is a secondary destination. A failure must never roll back a successful
    # Instagram post or cause Instagram to be published again on the next run.
    yt_cfg = cfg.get("youtube", {}) or {}
    yt_platform = clip.setdefault("platforms", {}).setdefault("youtube", {"status": "pending"})
    if yt_cfg.get("enabled", True):
        if not os.getenv("YT_TOKEN_JSON", "").strip():
            yt_platform.update(status="not_configured", error="YT_TOKEN_JSON secret is missing")
            log.warning("Instagram posted successfully, but YouTube upload skipped: YT_TOKEN_JSON is not configured.")
            jr(state, "youtube_skipped", clip=clip["id"], reason="missing YT_TOKEN_JSON")
        else:
            try:
                from youtube_uploader import upload_to_youtube
                original_url = f"https://www.youtube.com/watch?v={clip.get('video_id')}" if clip.get("video_id") else ""
                source_line = f"Original podcast: {original_url}" if original_url else f"Source: {clip.get('channel', 'podcast')}"
                description = "\n\n".join(part for part in [
                    str(clip.get("caption") or "").strip(),
                    "#Shorts",
                    source_line,
                ] if part)
                yt_result = upload_to_youtube(
                    video=path,
                    title=clip.get("title") or "Podcast Short",
                    description=description,
                    category=str(yt_cfg.get("category", "22")),
                    keywords=yt_cfg.get("tags", ["podcast", "shorts", "clips", "india"]),
                    privacy_status=str(yt_cfg.get("privacy_status", "public")),
                    notify_subscribers=bool(yt_cfg.get("notify_subscribers", False)),
                )
                yt_platform.update(
                    status="uploaded", video_id=yt_result["video_id"], url=yt_result["url"],
                    uploaded_at=now_iso(), error=None,
                )
                jr(state, "youtube_uploaded", clip=clip["id"], video=yt_result["video_id"])
                log.info("YouTube Shorts uploaded for clip %s -> %s", clip["id"], yt_result["url"])
            except Exception as ex:
                yt_platform.update(status="failed", error=str(ex)[:500], failed_at=now_iso())
                jr(state, "youtube_upload_failed", clip=clip["id"], error=str(ex)[:180])
                # Make the secondary-platform failure visible in the Actions UI.
                # Instagram is already live, so do not roll back or retry it.
                gha("warning", f"Clip {clip['id']} posted to Instagram, but YouTube upload failed: {str(ex)[:220]}")
                log.exception("YouTube upload failed for clip %s; Instagram remains posted.", clip["id"])
    else:
        yt_platform.update(status="disabled")
        log.info("YouTube publishing disabled in config.yaml.")

    save_state(state)
    shutil.rmtree(WORK, ignore_errors=True)
    log.info("Instagram posted clip %s -> media id %s", clip["id"], media_id)
    return True



PROMOTION_SCREEN_VERSION = "publish-promotion-screen-v2"
PROMOTION_TYPES = {"none", "sponsor_read", "affiliate", "product_pitch",
                   "self_promotion", "unclear"}
PROMOTION_SCREEN_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "content_type": {
            "type": "STRING",
            "enum": ["story", "insight", "explanation", "opinion", "debate",
                     "question_answer", "reaction", "promotion", "other"],
        },
        "promotion_type": {
            "type": "STRING",
            "enum": ["none", "sponsor_read", "affiliate", "product_pitch",
                     "self_promotion", "unclear"],
        },
        "promotion_confidence": {"type": "NUMBER"},
        "classification_confidence": {"type": "NUMBER"},
        "evidence": {"type": "STRING"},
    },
    "required": ["content_type", "promotion_type", "promotion_confidence",
                 "classification_confidence", "evidence"],
}


def screen_queued_clip_for_promotion(cfg, clip):
    """Final fail-closed transcript screen for queued clips, including legacy state entries.

    It is run on the rendered clip's stored transcript immediately before QA/publishing.
    A failed or low-confidence screen is sent to human review, never silently approved.
    """
    if clip.get("promotion_screen_version") == PROMOTION_SCREEN_VERSION:
        policy = cfg.get("intelligence", {}).get("promotion_policy", "manual_review")
        threshold = float(cfg.get("intelligence", {}).get("promotion_threshold", 0.72))
        promotion_type = str(clip.get("promotion_type") or "none").lower()
        promotion_confidence = float(clip.get("promotion_confidence") or 0.0)
        classification_confidence = float(clip.get("classification_confidence") or 0.0)
        flagged = promotion_type not in ("none",) and (
            promotion_confidence >= threshold or classification_confidence >= 0.60
        )
        uncertain = (promotion_type == "unclear" or classification_confidence < 0.60)
        excluded = flagged and policy == "exclude"
        review_required = (
            bool(clip.get("editorial_review_required"))
            or uncertain
            or (flagged and policy == "manual_review")
        )
        if excluded:
            return {"review_required": False, "excluded": True,
                    "issues": ["Clip classified as promotional and excluded by editorial policy."],
                    "already_screened": True}
        return {
            "review_required": review_required, "excluded": False,
            "issues": ([str(clip.get("editorial_gate_reason") or "promotion classification requires review")]
                       if review_required else []),
            "already_screened": True,
        }

    words = clip.get("words") or []
    transcript = " ".join(str(item[1]) for item in words
                          if isinstance(item, (list, tuple)) and len(item) >= 2).strip()
    if not transcript:
        clip["promotion_screen_error"] = "no stored transcript is available for publication screening"
        return {
            "review_required": True, "excluded": False,
            "issues": ["Promotion screen cannot run because the clip transcript is missing."],
        }
    if not cfg.get("llm", {}).get("enabled") or not gemini_key():
        clip["promotion_screen_error"] = "Gemini unavailable"
        return {
            "review_required": True, "excluded": False,
            "issues": ["Promotion screen unavailable; manual review required."],
        }

    prompt = (
        "You are a strict final editorial safety reviewer for a podcast clip that is about to be "
        "published as an Instagram Reel. Classify ONLY the supplied clip transcript. Detect a "
        "sponsor read, paid brand integration, affiliate pitch, creator self-promotion, or direct "
        "product/service sales pitch. A casual product/brand mention, a factual example, or an "
        "ordinary recommendation in a broader discussion is NOT automatically an advertisement. "
        "Look for actual selling intent: sponsor disclosure, repeated product benefits, pricing, "
        "discounts, calls to action, app/service instructions, or a direction to a link/description. "
        "Do not infer sponsorship merely from a brand name. If evidence is ambiguous, return "
        "promotion_type='unclear' and request human review.\n"
        "Return ONLY one valid JSON object with: content_type (story, insight, explanation, opinion, "
        "debate, question_answer, reaction, promotion, other), promotion_type "
        "(none, sponsor_read, affiliate, product_pitch, self_promotion, unclear), "
        "promotion_confidence (0 to 1), classification_confidence (0 to 1), evidence (short exact "
        "phrase from the supplied transcript). Never invent words or context.\n"
        "Clip title: " + str(clip.get("title") or "")[:160] +
        "\nTranscript:\n" + transcript[:7000]
    )
    response = None
    last_screen_error = None
    for attempt in range(2):
        retry_prompt = prompt
        if attempt:
            retry_prompt += (
                "\nRETRY: The previous response could not be parsed or validated. "
                "Return exactly one JSON object matching the required schema. "
                "Do not add Markdown, commentary, or code fences."
            )
        try:
            response = parse_json(gemini_text(
                cfg, [{"text": retry_prompt}],
                temperature=0.0 if attempt else 0.1,
                max_tokens=1200,
                response_schema=PROMOTION_SCREEN_SCHEMA,
            ))
            if isinstance(response, list) and response:
                response = response[0]
            if not isinstance(response, dict):
                raise ValueError("screen returned no JSON object")

            promotion_type = str(response.get("promotion_type") or "").strip().lower()
            content_type = str(response.get("content_type") or "other").strip().lower()
            if promotion_type not in PROMOTION_TYPES:
                raise ValueError("screen returned an unsupported promotion_type")
            if content_type not in {
                "story", "insight", "explanation", "opinion", "debate",
                "question_answer", "reaction", "promotion", "other",
            }:
                raise ValueError("screen returned an unsupported content_type")
            try:
                promotion_confidence = float(response.get("promotion_confidence"))
                classification_confidence = float(response.get("classification_confidence"))
            except (TypeError, ValueError):
                raise ValueError("screen omitted numeric confidence values") from None
            if (not np.isfinite(promotion_confidence)
                    or not np.isfinite(classification_confidence)):
                raise ValueError("screen returned non-finite confidence values")
            if not (0.0 <= promotion_confidence <= 1.0
                    and 0.0 <= classification_confidence <= 1.0):
                raise ValueError("screen returned confidence outside the 0..1 range")
            promotion_confidence = min(max(promotion_confidence, 0.0), 1.0)
            classification_confidence = min(max(classification_confidence, 0.0), 1.0)
            evidence = str(response.get("evidence") or "")[:240]
            break
        except Exception as ex:
            last_screen_error = ex
            retryable_response_error = (
                isinstance(ex, ValueError)
                or (isinstance(ex, GeminiError)
                    and "response was not valid JSON" in str(ex))
            )
            if attempt == 0 and retryable_response_error:
                log.warning(
                    "promotion screen response was invalid; retrying once with strict JSON schema (%s)",
                    str(ex)[:180],
                )
                continue
            clip["promotion_screen_error"] = (
                f"{type(ex).__name__}: {str(ex)}"
            )[:240]
            clip["promotion_screen_attempts"] = attempt + 1
            log.error(
                "promotion screen failed for clip %s after %d attempt(s): %s",
                clip.get("id", "?"), attempt + 1, clip["promotion_screen_error"],
            )
            return {
                "review_required": True, "excluded": False,
                "issues": [
                    "Promotion screen failed or returned an uncertain result; manual review required."
                ],
            }
    if response is None:
        # Defensive guard: a screen that somehow exits the retry loop without a
        # validated response must never be interpreted as a clean classification.
        clip["promotion_screen_error"] = str(last_screen_error or "no validated response")[:240]
        clip["promotion_screen_attempts"] = 2
        return {
            "review_required": True, "excluded": False,
            "issues": ["Promotion screen did not produce a validated result; manual review required."],
        }

    policy = cfg.get("intelligence", {}).get("promotion_policy", "manual_review")
    threshold = float(cfg.get("intelligence", {}).get("promotion_threshold", 0.72))
    explicit_promotion = (
        promotion_type not in ("none",) or content_type == "promotion"
    )
    uncertain = (promotion_type == "unclear"
                 or classification_confidence < 0.60)
    flagged = explicit_promotion and (
        promotion_confidence >= threshold or classification_confidence >= 0.60
    )
    excluded = flagged and policy == "exclude"
    review_required = uncertain or (flagged and policy == "manual_review")
    if policy == "allow" and flagged:
        review_required = False

    clip.update({
        "promotion_screen_version": PROMOTION_SCREEN_VERSION,
        "promotion_screened_at": now_iso(),
        "content_type": content_type,
        "promotion_type": promotion_type,
        "promotion_confidence": round(promotion_confidence, 3),
        "classification_confidence": round(classification_confidence, 3),
        "promotion_evidence": evidence,
        "promotion_screen_attempts": attempt + 1,
        "promotion_screen_error": None,
    })
    if flagged:
        clip["editorial_review_required"] = review_required
        clip["editorial_gate_reason"] = (
            "promotional_content" if policy == "manual_review"
            else "promotional_content_excluded" if policy == "exclude" else ""
        )
    elif uncertain:
        clip["editorial_review_required"] = True
        clip["editorial_gate_reason"] = "promotion_classification_uncertain"
    issues = []
    if excluded:
        issues.append("Clip classified as promotional and excluded by editorial policy.")
    elif review_required:
        issues.append(
            "Clip appears promotional or its promotion classification is uncertain. "
            + (f"Evidence: {evidence}" if evidence else "Human decision required.")
        )
    return {"review_required": review_required, "excluded": excluded,
            "issues": issues, "already_screened": False}


def posting_slot_diagnostic(cfg, state, now=None):
    """Explain why no posting slot is currently eligible, using the same slot windows as due_slot."""
    tz = ZoneInfo(cfg["timezone"])
    now = now or datetime.now(tz)
    sch = cfg["schedule"]
    wait_s = sch["max_wait_min"] * 60
    catch_s = sch["catchup_hours"] * 3600

    posted = []
    for clip in state.get("clips", []):
        stamp = clip.get("posted_at") or clip.get("publish_started_at")
        if clip.get("status") in ("posted", "publishing", "publish_uncertain") and stamp:
            try:
                posted.append((clip, datetime.fromisoformat(stamp)))
            except (TypeError, ValueError):
                continue

    candidates = []
    accounted = []
    weekly = cfg.get("weekly_post_times") or {}
    for day_offset in (-1, 0, 1):
        date = (now + timedelta(days=day_offset)).date()
        slots = weekly.get(date.strftime("%A"), cfg.get("post_times", []))
        for hhmm in slots:
            hour, minute = map(int, hhmm.split(":"))
            slot = datetime.combine(date, datetime.min.time(), tzinfo=tz).replace(
                hour=hour, minute=minute, second=0, microsecond=0)
            delta = (slot - now).total_seconds()
            candidates.append((abs(delta), slot, delta))
            lo = slot - timedelta(seconds=wait_s + 300)
            hi = slot + timedelta(seconds=catch_s)
            matches = [
                f"clip {clip.get('id')} ({clip.get('status')})"
                for clip, stamp in posted if lo <= stamp <= hi
            ]
            if matches:
                accounted.append(f"{slot.strftime('%Y-%m-%d %H:%M')} {date.strftime('%A')} IST: {', '.join(matches)}")

    nearest = min(candidates, key=lambda item: item[0]) if candidates else None
    nearest_text = "none"
    if nearest:
        _, slot, delta = nearest
        direction = "in" if delta >= 0 else "ago"
        minutes = abs(delta) / 60
        nearest_text = f"{slot.strftime('%Y-%m-%d %H:%M')} IST ({minutes:.0f} min {direction})"

    accounted_text = "; ".join(accounted) if accounted else "none in the eligible slot windows"
    if weekly:
        slot_summary = f"weekly_slots={weekly}"
    else:
        slot_summary = f"configured_slots={','.join(cfg.get('post_times', []))} IST"
    return (
        f"posting-slot diagnostic: local_now={now.strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"timezone={cfg['timezone']} {slot_summary}; "
        f"eligible_window=-{sch['catchup_hours']}h/+{sch['max_wait_min']}m; "
        f"nearest_slot={nearest_text}; already_accounted={accounted_text}"
    )


def publish_next(cfg, wait=True, force=False):
    """Render, inspect, and publish one eligible clip; ambiguous outcomes are never auto-retried."""
    state = load_state()
    allowed_names = {c["name"] for c in authorized_channels(cfg)}
    if not allowed_names:
        log.warning("Publishing skipped: no authorized channels are enabled in config.yaml.")
        jr(state, "publishing_paused", reason="no authorized channels enabled")
        save_state(state)
        return True
    slot = None
    if not force:
        slot = due_slot(cfg, state)
        if slot is None:
            state = load_state()
            log.info("no posting slot is due right now")
            log.info("%s", posting_slot_diagnostic(cfg, state))
            return True
    all_queued = queued(state)
    q = [clip for clip in all_queued if clip.get("channel") in allowed_names]
    # Prefer clips with stronger editorial signals over FIFO order. Stable sorting
    # preserves queue order for ties and older state entries with missing scores.
    def queue_quality(clip):
        for key in ("editorial_score", "pre", "learned_score", "heat"):
            try:
                value = float(clip.get(key))
                if np.isfinite(value):
                    return value
            except (TypeError, ValueError):
                pass
        return 0.0
    q.sort(key=queue_quality, reverse=True)
    held = len(all_queued) - len(q)
    if held:
        log.warning("Holding %d queued clip(s) from channels without current permission: true; they will not be published.", held)
        jr(state, "clips_held_permission", count=held)
    if not q:
        if held:
            save_state(state)
            return True
        gha("warning", "No Reel published this slot: queue is empty after discovery/refill. This is an expected no-post outcome.")
        jr(state, "slot_deferred", reason="queue empty; no eligible clip available")
        save_state(state)
        return True
    if slot and wait and slot[1] > 0:
        log.info("waiting %.0f s for the %s slot", slot[1], slot[0].strftime("%H:%M"))
        time.sleep(slot[1])
    if slot:
        jr(state, "slot_due", slot=slot[0].isoformat(timespec="minutes"))

    needs_review = False
    quality_blocked = False
    candidate_limit = int(cfg.get("queue", {}).get("publish_candidate_limit", 10))
    candidates_to_check = q[:candidate_limit]
    if len(q) > len(candidates_to_check):
        log.warning(
            "Checking the first %d of %d queued clips this run; remaining clips stay queued.",
            len(candidates_to_check), len(q),
        )
    for clip in candidates_to_check:
        log.info("candidate clip %d (%s %s-%s)", clip["id"], clip["channel"], hms(clip["start"]), hms(clip["end"]))
        try:
            path = render_clip(cfg, clip)
        except Exception as ex:
            _fail(state, clip, ex, "render")
            continue
        if clip.get("approved_manually_at"):
            # Human approval applies to this exact clip; deterministic media QA still runs.
            try:
                technical = qa_checks(cfg, path)
                review = {"verdict": "hard_fail" if technical["issues"] else "pass",
                          "score": None,
                          "issues": technical["issues"] or ["Human-reviewed clip; technical QA passed"]}
            except Exception as ex:
                review = {"verdict": "hard_fail", "score": 0,
                          "issues": [f"technical QA failed after manual approval: {str(ex)[:180]}"]}
        else:
            # Mandatory final transcript screen also covers clips queued by older code
            # before editorial classification existed in state.json.
            promotion_screen = screen_queued_clip_for_promotion(cfg, clip)
            if promotion_screen["excluded"]:
                clip.update(status="rejected", error="Editorial policy: promotional content")
                quality_blocked = True
                clip["qa"] = {"verdict": "hard_fail", "score": None,
                              "issues": promotion_screen["issues"]}
                jr(state, "clip_rejected", clip=clip["id"], issues=promotion_screen["issues"])
                save_state(state)
                gha("warning", f"Clip {clip['id']} excluded by promotional-content policy.")
                continue
            if (promotion_screen["review_required"]
                    or clip.get("editorial_review_required")):
                issues = promotion_screen["issues"] or [
                    "Existing editorial classification requires a human decision."
                ]
                REVIEW_DIR.mkdir(parents=True, exist_ok=True)
                review_path = REVIEW_DIR / f"clip_{clip['id']}.mp4"
                shutil.copy2(path, review_path)
                # Preserve human-review holds for uncertain content, but allow bounded retries
                # when the screen itself failed transiently (API/response error).
                clip["editorial_review_required"] = not bool(clip.get("promotion_screen_error"))
                clip.update(status="needs_review", review_artifact_file=review_path.name,
                            error="Manual editorial review required: " + "; ".join(issues)[:300])
                # API/response failures can be transient. Schedule a bounded retry; low-confidence
                # classifications and suspected promotions remain human-review-only.
                if clip.get("promotion_screen_error"):
                    retry_count = int(clip.get("promotion_retry_count", 0))
                    if retry_count < 2:
                        clip["promotion_retry_after"] = (
                            datetime.now(timezone.utc) + timedelta(hours=24)
                        ).isoformat()

                clip["qa"] = {"verdict": "soft_fail", "score": None, "issues": issues}
                jr(state, "clip_needs_review", clip=clip["id"], artifact=review_path.name,
                   issues=issues, review_stage="promotion_screen")
                save_state(state)
                gha("warning", f"Clip {clip['id']} needs editorial review; it will not be auto-published.")
                needs_review = True
                quality_blocked = True
                continue
            # Persist the clean publication screen before proceeding to the normal quality gate.
            save_state(state)
            review = qa_review(cfg, path, clip)
        if (clip.get("editorial_review_required")
                and not clip.get("approved_manually_at")
                and review["verdict"] == "pass"):
            review = {
                "verdict": "soft_fail", "score": review.get("score"),
                "issues": ["Editorial classification needs a human decision: "
                           + str(clip.get("editorial_gate_reason") or "editorial review"),
                           "Candidate has a high-confidence promotional or uncertain content classification"],
            }
        clip["qa"] = {"verdict": review["verdict"], "score": review["score"], "issues": review["issues"],
                      "fallback": bool(review.get("fallback", False))}
        if review.get("fallback"):
            log.warning("Clip %s accepted as best-available fallback (QA %.1f/10; preferred %.1f/10).",
                        clip["id"], review["score"],
                        float(cfg.get("qa", {}).get("min_ai_score", 6.5)))
            jr(state, "clip_best_available_fallback", clip=clip["id"],
               score=review["score"], issues=review["issues"])
        if review["verdict"] == "hard_fail":
            clip.update(status="rejected", error="QA: " + "; ".join(review["issues"])[:300])
            quality_blocked = True
            jr(state, "clip_rejected", clip=clip["id"], issues=review["issues"])
            save_state(state)
            gha("warning", f"Clip {clip['id']} rejected by QA: {review['issues']}")
            continue
        if review["verdict"] == "soft_fail":
            REVIEW_DIR.mkdir(parents=True, exist_ok=True)
            review_path = REVIEW_DIR / f"clip_{clip['id']}.mp4"
            shutil.copy2(path, review_path)
            clip.update(status="needs_review", review_artifact_file=review_path.name,
                        error="Manual review required: " + "; ".join(review["issues"])[:300])
            jr(state, "clip_needs_review", clip=clip["id"], score=review.get("score"),
               artifact=review_path.name, issues=review["issues"])
            save_state(state)
            gha("warning", f"Clip {clip['id']} needs manual review; it will not be auto-published.")
            needs_review = True
            quality_blocked = True
            continue
        post_result = _post_clip(cfg, state, clip, path)
        if post_result is True:
            return True
        if post_result is False:
            # Ambiguous media_publish outcome: do not publish another candidate in this slot.
            return False
        # Definite create/upload/API failure: continue to the next queued clip.
        continue

    if needs_review or quality_blocked:
        remaining = max(0, len(q) - len(candidates_to_check))
        reason = (
            "no Reel published; best available candidates were held/rejected by editorial or QA gates"
            + (f"; {remaining} queued candidate(s) not scanned due to publish_candidate_limit"
               if remaining else "")
        )
        jr(state, "slot_deferred", reason=reason,
           checked_candidates=len(candidates_to_check), queued_remaining=remaining)
        save_state(state)
        gha("warning", "No Reel published this slot: all checked candidates failed or were held by quality gates. This is an expected no-post outcome, not a runner failure.")
        return True
    jr(state, "slot_missed", reason="no clip could be posted")
    save_state(state)
    return False

def fetch_metrics(cfg, media_id):
    token, g, out = env_value("IG_ACCESS_TOKEN"), graph(cfg), {}
    if not token:
        log.warning("Instagram metrics skipped: IG_ACCESS_TOKEN is not configured")
        return out
    # Query one metric at a time: reels_skip_rate and watch-time fields are Reel-specific,
    # and one unsupported metric must not discard the other useful insights.
    for metric in (
        "views", "reach", "likes", "comments", "shares", "saved",
        "ig_reels_avg_watch_time", "ig_reels_video_view_total_time",
        "reels_skip_rate",
    ):
        try:
            response = requests.get(
                f"{g}/{media_id}/insights", timeout=30,
                params={"metric": metric, "access_token": token},
            )
            if not response.ok:
                log.debug("Instagram insight %s unavailable: %s", metric, response.text[:160])
                continue
            payload = response.json().get("data") or []
            if not payload:
                continue
            item = payload[0]
            if item.get("name") != metric:
                item = next((entry for entry in payload if entry.get("name") == metric), item)
            values = item.get("values") or []
            value = values[0].get("value") if values else item.get("value")
            if isinstance(value, (int, float)):
                out[metric] = value
            elif isinstance(value, str):
                try:
                    out[metric] = float(value)
                except ValueError:
                    pass
        except Exception as ex:
            log.debug("Instagram insight %s failed: %s", metric, str(ex)[:160])
    return out

def update_metrics(cfg):
    state, changed, n = load_state(), False, 0
    for c in state["clips"]:
        if c["status"] != "posted" or not c.get("media_id") or n >= 10:
            continue
        age_h = (now_utc() - datetime.fromisoformat(c["posted_at"])).total_seconds() / 3600
        if not 24 <= age_h <= 24 * 10:
            continue
        if c.get("metrics_at") and (now_utc() - datetime.fromisoformat(c["metrics_at"])
                                    ).total_seconds() < 20 * 3600:
            continue
        m = fetch_metrics(cfg, c["media_id"])
        n += 1
        if m:
            sampled_at = now_iso()
            c["metrics"], c["metrics_at"], changed = m, sampled_at, True
            add_metric_snapshot(c, m, c.get("posted_at"), sampled_at=sampled_at)
            quality = performance_quality(m, c.get("end", 0) - c.get("start", 0))
            c["performance_detail"] = quality
            if quality.get("score") is not None:
                c["performance_score"] = quality["score"]
    if changed:
        save_state(state)
    return changed


# =============================================================================
# 6. ORCHESTRATION, STATUS, CHECK
# =============================================================================
def tick(cfg, force=False):
    # With no explicitly authorized sources, this is an intentional idle state, not a failed run.
    # Do not publish any clips left in the queue after a permission has been disabled.
    if not authorized_channels(cfg):
        state = load_state()
        log.warning("Clipper is idle: no channel has permission: true in config.yaml. No content will be discovered or published.")
        jr(state, "automation_idle", reason="no authorized channels enabled; publishing disabled")
        save_state(state)
        try:
            update_metrics(cfg)
        except Exception:
            log.exception("metrics update failed")
        write_summary()
        return True

    ok = True
    state = load_state()
    if retry_transient_promotion_reviews(state):
        save_state(state)
    state = load_state()
    active_queue = queued_for_authorized_channels(state, cfg)
    allowed_names = {c["name"] for c in authorized_channels(cfg)}
    review_backlog = [c for c in state.get("clips", [])
                      if c.get("status") == "needs_review" and c.get("channel") in allowed_names]
    pending_count = len(active_queue) + len(review_backlog)
    if pending_count == 0:
        try:
            produce(cfg)                          # nothing queued or held: analyse first
        except Exception:
            log.exception("initial produce failed")
    if not publish_next(cfg, wait=True, force=force):
        ok = False
    if len(queued_for_authorized_channels(load_state(), cfg)) < cfg["queue"]["refill_when_below"]:
        log.info("queue is low -> finding the next episode")
        try:
            produce(cfg)
        except Exception:
            log.exception("refill failed")
    try:
        update_metrics(cfg)
    except Exception:
        log.exception("metrics update failed")
    write_summary()
    return ok


def status_table(limit=15):
    state = load_state()
    rows = ["| id | status | channel | window | views | reach | saves | shares | quality | retention | skip |",
            "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for c in state["clips"][-limit:][::-1]:
        m = c.get("metrics") or {}
        detail = c.get("performance_detail") or {}
        quality = c.get("performance_score")
        quality_display = f"{quality:.2f}" if isinstance(quality, (int, float)) else ""
        rows.append(f"| {c['id']} | {c['status']} | {c['channel']} | "
                    f"{hms(c['start'])}-{hms(c['end'])} | {views_of(c) or ''} | "
                    f"{m.get('reach', '')} | {m.get('saved', '')} | {m.get('shares', '')} | "
                    f"{quality_display} | {detail.get('retention_pct', '')} | "
                    f"{detail.get('skip_rate', '')} |")
    q = len(queued(state))
    review_count = sum(c.get("status") == "needs_review" for c in state["clips"])
    uncertain_count = sum(c.get("status") in ("publishing", "publish_uncertain") for c in state["clips"])
    total = sum(views_of(c) or 0 for c in state["clips"])
    quality_samples = sum(c.get("performance_score") is not None for c in state["clips"])
    jl = [f"- `{j['t'][5:16]}` **{j['event']}** " + ", ".join(f"{k}={v}" for k, v in j.items()
                                                           if k not in ("t", "event"))[:160]
          for j in state.get("journal", [])[-8:]]
    learning = learning_counts(state)
    return (f"**Queue:** {q} waiting · **Manual review:** {review_count} · "
            f"**Publish outcome check:** {uncertain_count} · "
            f"**Performance samples:** {quality_samples} · **Total views tracked:** {total}\n"
            f"**Learning data:** {learning['candidate_decisions']} candidate decisions · "
            f"{learning['human_feedback']} human feedback records · "
            f"{learning['published']} published clips\n\n"
            + "\n".join(rows)
            + ("\n\n**Agent journal (latest)**\n" + "\n".join(jl) if jl else ""))

def write_summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("## Podcast clipper\n" + status_table() + "\n")


def check(cfg, live=False):
    """Validate integrations; live=True exercises media paths without queuing or publishing."""
    if live:
        notice = (
            "DIAGNOSTIC ONLY: check-live does not add clips to the persistent queue or publish a Reel. "
            "Its Instagram smoke test uploads a temporary container but deliberately does not call media_publish."
        )
        print(notice)
        gha("notice", notice)
    ok = True

    def line(good, text, required=True):
        nonlocal ok
        passed = bool(good)
        print(("OK   " if passed else ("FAIL " if required else "info ")) + text)
        if required:
            ok &= passed

    line(shutil.which("ffmpeg"), "ffmpeg")
    line(shutil.which("ffprobe"), "ffprobe")

    ig_id, ig_token = env_value("IG_USER_ID"), env_value("IG_ACCESS_TOKEN")
    line(bool(ig_id), "env IG_USER_ID")
    line(bool(ig_token), "env IG_ACCESS_TOKEN")
    n = len(authorized_channels(cfg))
    line(n > 0, f"{n} channel(s) with permission: true")

    youtube_cfg = cfg.get("youtube", {}) or {}
    if youtube_cfg.get("enabled", True):
        if not os.getenv("YT_TOKEN_JSON", "").strip():
            line(False, "YT_TOKEN_JSON (YouTube upload will be skipped until configured)", required=False)
        else:
            try:
                from youtube_uploader import check_youtube_account
                yt_account = check_youtube_account()
                line(True, f"YouTube OAuth channel: {yt_account['channel_title']} ({yt_account['channel_id']})",
                     required=False)
            except Exception as ex:
                line(False, f"YouTube OAuth/API check: {str(ex)[:220]}", required=False)

    if ig_id and ig_token:
        try:
            response = requests.get(
                f"{graph(cfg)}/{ig_id}", timeout=30,
                params={"fields": "username", "access_token": ig_token},
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            line(response.ok, ("Instagram account: " + str(payload.get("username", "?")))
                 if response.ok else "Instagram token/account check: " + response.text[:200])
            if response.ok:
                try:
                    media_response = requests.get(
                        f"{graph(cfg)}/{ig_id}/media", timeout=30,
                        params={"fields": "id,media_type,media_product_type,timestamp",
                                "limit": 10, "access_token": ig_token},
                    )
                    if media_response.ok:
                        media_items = (media_response.json() or {}).get("data") or []
                        latest_reel = next((item for item in media_items
                                            if item.get("media_product_type") == "REELS"), None)
                        if latest_reel:
                            available_metrics = fetch_metrics(cfg, latest_reel["id"])
                            line(bool(available_metrics),
                                 f"Instagram insights endpoint returned {len(available_metrics)} metric(s) "
                                 "for a recent Reel", required=False)
                        else:
                            line(False, "no recent Reel found to test insights; metrics check skipped",
                                 required=False)
                    else:
                        line(False, "Instagram media listing for insights failed: "
                             + media_response.text[:180], required=False)
                except Exception as ex:
                    line(False, f"Instagram insights smoke test failed: {str(ex)[:180]}",
                         required=False)
        except Exception as ex:
            line(False, f"Instagram account check failed: {str(ex)[:200]}")

    llm = cfg.get("llm", {})
    gemini_required = bool(llm.get("enabled") or llm.get("video_verify")
                           or llm.get("final_qa") or llm.get("polish_captions"))
    key = gemini_key()
    line(bool(key), "env GEMINI_API_KEY", required=gemini_required)
    if key:
        try:
            txt = gemini_text(cfg, [{"text": 'Reply with exactly this JSON: {"ok": true}'}])
            valid = isinstance(parse_json(txt), dict) and parse_json(txt).get("ok") is True
            line(valid, f"Gemini text API works (model {_MODEL_CACHE.get('model')})",
                 required=gemini_required)
        except Exception as ex:
            line(False, f"Gemini text API failed: {str(ex)[:240]}", required=gemini_required)

    diarization_enabled = bool((cfg.get("diarization") or {}).get("enabled"))
    hf_token = env_value("HF_TOKEN")
    if diarization_enabled:
        line(bool(hf_token), "env HF_TOKEN for enabled speaker diarization")
        if live and hf_token:
            try:
                from speaker_diarization import _load_pipeline
                _load_pipeline(hf_token)
                line(True, "pyannote Community-1 model loads with this token")
            except Exception as ex:
                # Diarization is an optional enhancement. A gated-model 403 should
                # be visible, but must not block clipping, rendering, or publishing.
                line(False, f"optional pyannote model unavailable: {str(ex)[:240]} "
                     "(accept model conditions on the HF account linked to HF_TOKEN)",
                     required=False)

    if live:                                        # real network probes (what unit tests cannot cover)
        deno_path = shutil.which("deno")
        line(bool(deno_path), "Deno JavaScript runtime for YouTube extraction")
        try:
            ejs_version = package_version("yt-dlp-ejs")
        except PackageNotFoundError:
            ejs_version = None
        line(bool(ejs_version), "yt-dlp-ejs challenge scripts" +
             (f" ({ejs_version})" if ejs_version else ""))
        try:
            bgutil_version = package_version("bgutil-ytdlp-pot-provider")
        except PackageNotFoundError:
            bgutil_version = None
        line(bgutil_version == BGUTIL_PLUGIN_VERSION,
             f"bgutil PO-token plugin version {bgutil_version or 'missing'} "
             f"(expected {BGUTIL_PLUGIN_VERSION})")
        try:
            pot_response = requests.get(f"{BGUTIL_PROVIDER_URL}/ping", timeout=5)
            pot_ok = pot_response.ok
            pot_detail = f"HTTP {pot_response.status_code}"
        except Exception as ex:
            pot_ok = False
            pot_detail = str(ex)[:120]
        line(pot_ok, f"bgutil PO-token HTTP server ({pot_detail})")
        line(bool(env_value("YT_COOKIES_FILE")), "YouTube cookie file configured", required=False)

        vid = cfg.get("check_video_id", "aqz-KE-bpKQ")     # open-licensed test video
        test_output = None
        try:
            info = full_info(cfg, vid)
            line(True, f"YouTube video info works: {info['title'][:50]!r} ({info['duration']}s)")
            line(bool(info.get("heatmap")), "Most-Replayed heatmap present", required=False)
            s0 = float(min(30, info["duration"] * 0.3))
            clip = {"video_id": vid, "start": s0, "end": s0 + 8, "title": "Clipper preflight",
                    "words": [[s0 + i * 0.5, f"test{i}"] for i in range(14)]}
            t0 = time.time()
            test_output = render_clip(cfg, clip)
            media = probe_media(test_output)
            line(media["w"] == 1080 and media["h"] == 1920 and media["has_audio"],
                 f"REAL YouTube download + 9:16 render works ({media['w']}x{media['h']}, "
                 f"{media['duration']:.1f}s, took {time.time() - t0:.0f}s)")
            technical = qa_checks(cfg, test_output)
            line(not technical["issues"],
                 f"technical QA: black {technical['black_pct']}% silent {technical['silence_pct']}% "
                 f"{technical['issues'] or ''}")
            final_review = gemini_qa(cfg, test_output)
            if llm.get("final_qa") and key:
                line(final_review is not None, "Gemini final-video QA returned a valid typed response")
                if final_review is not None:
                    line(final_review["approve"] and not final_review["unsafe"],
                         f"Gemini test-video QA score {final_review['score']}: "
                         f"{final_review['issues'] or 'no issues'}", required=False)

            # This performs a real resumable upload and waits for FINISHED, but intentionally
            # does not call media_publish. It leaves an unpublished container that expires.
            if ig_id and ig_token:
                try:
                    container_id = upload_reel_container(
                        cfg, test_output, "Clipper preflight upload — unpublished test container"
                    )
                    line(bool(container_id), "Instagram resumable upload + processing works "
                         "(test container NOT published)")
                except Exception as ex:
                    line(False, f"Instagram resumable upload smoke test failed: {str(ex)[:240]}")
        except Exception as ex:
            line(False, "YouTube download/render/QA smoke test FAILED "
                 f"(check YT_COOKIES / YT_PROXY): {str(ex)[:300]}")
        finally:
            if test_output:
                Path(test_output).unlink(missing_ok=True)
            shutil.rmtree(WORK, ignore_errors=True)

        permitted = authorized_channels(cfg)
        if permitted:
            try:
                entries = list_recent(cfg, permitted[0]["handle"], 3)
                line(bool(entries), f"channel listing works ({permitted[0]['name']}: {len(entries)} videos)")
            except Exception as ex:
                line(False, f"channel listing failed: {str(ex)[:200]}")

            # Exercise the same real discovery -> transcript -> candidate -> Gemini pipeline
            # used by produce(), but never add clips to state and never publish this candidate.
            try:
                smoke_state = {"episodes": {}, "clips": []}
                source_channel, episode = pick_episode(cfg, smoke_state)
                line(bool(episode), "eligible authorised podcast episode found")
                if episode:
                    picks = analyse_episode(cfg, source_channel, episode, state=smoke_state)
                    line(bool(picks), f"real episode transcript/editorial analysis produced {len(picks)} clip(s)")
                    if picks and llm.get("enabled") and key:
                        line(any("llm" in pick for pick in picks),
                             "Gemini candidate ranking returned usable scores")
                    if picks and llm.get("video_verify") and key:
                        line(any("vid" in pick for pick in picks),
                             "Gemini YouTube video verification returned usable scores")
                    if picks:
                        selected = picks[0]
                        real_clip = {
                            "video_id": episode["id"], "start": selected["start"],
                            "end": selected["end"], "title": selected.get("title") or "Production smoke test",
                            "words": [[round(t, 2), word] for t, word in selected.get("words", [])],
                            "features": {},
                        }
                        rendered = None
                        try:
                            rendered = render_clip(cfg, real_clip)
                            rendered_media = probe_media(rendered)
                            line(rendered_media["w"] == 1080 and rendered_media["h"] == 1920
                                 and rendered_media["has_audio"],
                                 "selected real episode clip rendered to 1080x1920 with audio")
                            rendered_qa = qa_checks(cfg, rendered)
                            line(not rendered_qa["issues"],
                                 f"selected clip technical QA passed: {rendered_qa['issues'] or 'clean'}")
                            if llm.get("final_qa") and key:
                                rendered_ai = gemini_qa(cfg, rendered)
                                line(rendered_ai is not None,
                                     "Gemini final QA works on a real selected podcast clip")
                        finally:
                            if rendered:
                                Path(rendered).unlink(missing_ok=True)
                            shutil.rmtree(WORK, ignore_errors=True)
            except Exception as ex:
                line(False, f"real episode production smoke test failed: {str(ex)[:300]}")
    return ok


def review_action(clip_id, approve):
    """Approve a held clip for the next publish run, or reject it permanently."""
    if clip_id is None:
        raise ValueError("--clip-id is required for approve/reject")
    state = load_state()
    clip = next((c for c in state["clips"] if c.get("id") == clip_id), None)
    if clip is None:
        raise ValueError(f"clip {clip_id} was not found in state")
    if clip.get("status") != "needs_review":
        raise ValueError(f"clip {clip_id} is {clip.get('status')!r}, not needs_review")
    if approve:
        clip.update(status="queued", approved_manually_at=now_iso(), error=None)
        jr(state, "clip_manually_approved", clip=clip_id)
        log.info("clip %s approved; run publish --force to publish it now", clip_id)
    else:
        clip.update(status="rejected", rejected_manually_at=now_iso(),
                    error="Rejected during manual review")
        jr(state, "clip_manually_rejected", clip=clip_id)
        log.info("clip %s rejected", clip_id)
    save_state(state)
    return True

def feedback_action(clip_id, label, reasons=None, note=""):
    """Persist structured human editorial feedback for future evaluation and learning."""
    if clip_id is None:
        raise ValueError("--clip-id is required for feedback")
    if not label:
        raise ValueError("--label is required for feedback")
    state = load_state()
    clip = add_feedback(state, clip_id, label, reasons=reasons, note=note)
    jr(state, "human_feedback_recorded", clip=clip_id, label=label,
       reasons=",".join(reasons or [])[:160])
    save_state(state)
    log.info("feedback recorded for clip %s: %s", clip_id, label)
    return True


def resolve_uncertain_publish(clip_id, media_id=None, retry=False):
    """Resolve an ambiguous Instagram publish only after checking the account manually."""
    if clip_id is None:
        raise ValueError("--clip-id is required")
    if bool(media_id) == bool(retry):
        raise ValueError("provide exactly one of --media-id (confirm posted) or --retry")
    state = load_state()
    clip = next((c for c in state["clips"] if c.get("id") == clip_id), None)
    if clip is None:
        raise ValueError(f"clip {clip_id} was not found in state")
    if clip.get("status") not in ("publishing", "publish_uncertain"):
        raise ValueError(f"clip {clip_id} is {clip.get('status')!r}, not an uncertain publish")
    if media_id:
        clip.update(status="posted", media_id=media_id,
                    posted_at=clip.get("publish_started_at") or now_iso(), error=None)
        clip.pop("publish_started_at", None)
        jr(state, "uncertain_publish_confirmed", clip=clip_id, media=media_id)
        log.info("clip %s marked as posted (media id %s)", clip_id, media_id)
    else:
        clip.update(status="queued", attempts=0, error=None)
        clip.pop("publish_started_at", None)
        jr(state, "uncertain_publish_cleared_for_retry", clip=clip_id)
        log.warning("clip %s returned to queue; verify it is NOT already on Instagram before retrying", clip_id)
    save_state(state)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["tick", "publish-now", "produce", "publish", "metrics", "status", "check",
                                        "approve", "reject", "feedback", "confirm-post", "retry-uncertain"])
    ap.add_argument("--clip-id", type=int, help="clip id for manual review or publish reconciliation")
    ap.add_argument("--media-id", help="Instagram media id when confirming an uncertain post")
    ap.add_argument("--label", choices=["good", "weak", "needs_context", "bad_cut",
                                         "promotional", "visual_issue", "caption_issue", "other"],
                    help="human editorial feedback label")
    ap.add_argument("--reason", action="append", default=[],
                    help="optional feedback reason; may be repeated")
    ap.add_argument("--note", default="", help="optional short reviewer note")
    ap.add_argument("--retry", action="store_true", help="retry an uncertain publish only after verifying it is absent from Instagram")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="post now, skip slot wait + duplicate guard")
    ap.add_argument("--live", action="store_true", help="check: also probe YouTube for real")
    ap.add_argument("--config")
    args = ap.parse_args()
    if args.force and args.command not in {"tick", "publish-now", "publish", "approve"}:
        ap.error("--force cannot make a diagnostic or maintenance command publish; use publish-now or tick --force")
    if args.dry_run and args.command != "produce":
        ap.error("--dry-run only applies to produce")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout)])
    cfg = load_config(args.config)
    ok = True
    if args.command in {"tick", "publish-now"}:
        # publish-now is the unambiguous manual path: fill the queue if needed, then
        # run the normal QA and publishing pipeline immediately, bypassing slot wait.
        ok = tick(cfg, force=(args.force or args.command == "publish-now"))
    elif args.command == "produce":
        produce(cfg, dry_run=args.dry_run)
    elif args.command == "publish":
        ok = publish_next(cfg, wait=not args.force, force=args.force)
    elif args.command == "metrics":
        update_metrics(cfg)
    elif args.command == "status":
        print(status_table(30))
    elif args.command == "approve":
        ok = review_action(args.clip_id, approve=True)
        if ok and args.force:
            ok = publish_next(cfg, wait=False, force=True)
    elif args.command == "reject":
        ok = review_action(args.clip_id, approve=False)
    elif args.command == "feedback":
        if args.clip_id is None or not args.label:
            ap.error("feedback requires --clip-id and --label")
        ok = feedback_action(args.clip_id, args.label, reasons=args.reason, note=args.note)
    elif args.command == "confirm-post":
        ok = resolve_uncertain_publish(args.clip_id, media_id=args.media_id)
    elif args.command == "retry-uncertain":
        ok = resolve_uncertain_publish(args.clip_id, retry=True)
    else:
        ok = check(cfg, live=args.live)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

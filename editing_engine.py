"""Validated, deterministic per-clip editing plans.

The planner selects from supported renderer behaviours. It never emits executable FFmpeg
commands or untrusted filter strings.
"""
VERSION = "edit-plan-v2"

ALLOWED_FORMATS = {"story", "educational", "debate", "humour", "conversation", "general"}
ALLOWED_CAPTION_STYLES = {"minimal", "emphasis", "dialogue", "kinetic"}
ALLOWED_CROP = {"face_track", "safe_center"}


def build_edit_plan(clip, cfg=None):
    clip = clip or {}
    kind = str(clip.get("content_type") or clip.get("semantic_kind") or "general").lower()
    if kind in ("personal_story", "story"):
        fmt, captions, words, crop = "story", "minimal", 4, "face_track"
    elif kind in ("insight", "explanation", "educational"):
        fmt, captions, words, crop = "educational", "kinetic", 3, "face_track"
    elif kind in ("debate", "question_answer"):
        fmt, captions, words, crop = "debate", "dialogue", 3, "face_track"
    elif kind in ("reaction", "humour"):
        fmt, captions, words, crop = "humour", "emphasis", 3, "face_track"
    else:
        fmt, captions, words, crop = "general", "minimal", 4, "face_track"

    plan = {
        "version": VERSION,
        "format": fmt,
        "caption_style": captions,
        "caption_group_words": words,
        "caption_font_size": 68 if fmt != "educational" else 70,
        "caption_margin_v": 420,
        "caption_emphasis": "meaningful",
        "title_duration_s": 2.8,
        "caption_animation": "pop",
        "crop_strategy": crop,
        "motion": "subtle" if fmt in ("story", "conversation") else "off",
        "broll": False,
        "camera_cutting": False,
        "rationale": (
            "Natural pacing for a story." if fmt == "story" else
            "Emphasise key phrases in an explanatory segment." if fmt == "educational" else
            "Compact dialogue captions; camera cutting requires validated active-speaker tracks."
            if fmt == "debate" else
            "Keep reaction timing readable without unnecessary effects." if fmt == "humour" else
            "Use the safe default conversational treatment."
        ),
    }
    return validate_edit_plan(plan)


def _bounded_float(value, default, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    if not __import__("math").isfinite(number):
        number = default
    return min(max(number, low), high)


def validate_edit_plan(plan):
    if not isinstance(plan, dict):
        plan = {}
    raw_format = plan.get("format")
    raw_caption = plan.get("caption_style")
    raw_crop = plan.get("crop_strategy")
    fmt = raw_format if isinstance(raw_format, str) and raw_format in ALLOWED_FORMATS else "general"
    caption = (raw_caption if isinstance(raw_caption, str)
               and raw_caption in ALLOWED_CAPTION_STYLES else "minimal")
    crop = raw_crop if isinstance(raw_crop, str) and raw_crop in ALLOWED_CROP else "face_track"
    try:
        words = min(max(int(plan.get("caption_group_words", 4)), 2), 6)
    except (TypeError, ValueError):
        words = 4
    try:
        font = min(max(int(plan.get("caption_font_size", 68)), 48), 84)
    except (TypeError, ValueError):
        font = 68
    try:
        margin = min(max(int(plan.get("caption_margin_v", 420)), 180), 650)
    except (TypeError, ValueError):
        margin = 420
    motion = plan.get("motion") if plan.get("motion") in ("off", "subtle") else "off"
    return {
        "version": VERSION, "format": fmt, "caption_style": caption,
        "caption_group_words": words, "caption_font_size": font,
        "caption_margin_v": margin, "crop_strategy": crop,
        "motion": motion, "caption_emphasis": str(plan.get("caption_emphasis") or "meaningful")[:24],
        "title_duration_s": _bounded_float(plan.get("title_duration_s", 2.8), 2.8, 1.0, 5.0),
        "caption_animation": plan.get("caption_animation") if plan.get("caption_animation") in ("none", "pop", "fade") else "pop",\n        "broll": False, "camera_cutting": False,
        "rationale": str(plan.get("rationale") or "")[:240],
    }

"""Optional local speaker diarization using pyannote Community-1.

Install requirements-diarization.txt, accept the model terms on Hugging Face, and
set HF_TOKEN. The main clipper remains usable without this optional dependency.
"""
from functools import lru_cache
from pathlib import Path
import logging
import os
import subprocess

log = logging.getLogger("clipper.diarization")
MODEL_ID = "pyannote/speaker-diarization-community-1"


@lru_cache(maxsize=1)
def _load_pipeline(token):
    from pyannote.audio import Pipeline
    pipeline = Pipeline.from_pretrained(MODEL_ID, token=token)
    if pipeline is None:
        raise RuntimeError("pyannote returned no diarization pipeline")
    return pipeline


def diarize_media(media_path, token=None):
    """Return relative {start,end,speaker} turns for a media clip; [] means unavailable.

    Model inference is opt-in. The downloaded video segment is converted to mono 16kHz
    PCM first, so the model sees audio only and inference does not depend on video codec.
    """
    token = token or os.environ.get("HF_TOKEN")
    if not token:
        log.info("speaker diarization skipped: HF_TOKEN is not configured")
        return []
    source = Path(media_path)
    wav = source.with_name(source.stem + "_diarization.wav")
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vn",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
            capture_output=True, text=True, timeout=120)
        if result.returncode != 0 or not wav.exists() or wav.stat().st_size < 1024:
            raise RuntimeError("audio extraction failed: " + (result.stderr or "")[-300:])

        pipeline = _load_pipeline(token)
        output = pipeline(str(wav))
        annotation = getattr(output, "speaker_diarization", output)
        turns = []
        for turn, _, speaker in annotation.itertracks(yield_label=True):
            start, end = float(turn.start), float(turn.end)
            if end > start:
                turns.append({"start": start, "end": end, "speaker": str(speaker)})
        turns.sort(key=lambda item: (item["start"], item["end"]))
        log.info("speaker diarization found %d turns across %d speakers",
                 len(turns), len({t["speaker"] for t in turns}))
        return turns
    except Exception as ex:
        log.warning("speaker diarization unavailable for %s: %s", source.name, str(ex)[:300])
        return []
    finally:
        wav.unlink(missing_ok=True)

# Podcast clipper (GitHub Actions + Gemini)

Automates an authorized podcast-to-Reels workflow: candidate discovery, transcript/heatmap scoring,
Gemini ranking, vertical rendering, technical QA, Instagram publishing and performance tracking.
It can help find promising clips; it cannot guarantee virality or predict views reliably. Treat scores
as ranking signals and review initial outputs before enabling unattended publishing.

The renderer now tracks a single visible face across sampled frames and smoothly adjusts the
9:16 crop position. It is visual tracking, not face identity recognition. Multi-face shots and uncertain
detections use the full-frame blurred fallback. Optional pyannote diarization can describe speaker turns
and prevent aggressive face crops on dialogue-heavy clips; it does not yet match a diarized voice to a
specific face, generate reaction cuts, or select B-roll.

Every rendered MP4 is checked against the Instagram Reels media profile before QA/publishing:
H.264/HEVC video, 4:2:0 pixel format, 23–60 fps, AAC audio at up to 48 kHz, mono/stereo,
and conservative bitrate/file-size limits. The renderer exports H.264 at 30 fps and AAC stereo
at 48 kHz / 128 kbps, then rejects any file that fails the compatibility checks.

Candidate selection now reconstructs transcript context before and after every cut. External context is
used to detect dangling references, never included as if they were spoken in the clip. The learning loop
uses average watch time, reach-normalised shares/saves/likes and Reels skip rate when those insights are
available; raw views are no longer the sole source-ranking signal. It keeps exploring channels with fewer
than `learning.min_channel_samples` mature Reels and only learns hook/duration priors after
`learning.min_format_samples` examples (both thresholds are configurable in `config.yaml`).

## Put it on GitHub and prove it works (15 minutes)
1. Create a repo, commit every file in this folder (including the hidden `.github` folder), push.
2. Settings -> Secrets and variables -> Actions -> New repository secret:
   | Secret | Needed | What |
   |---|---|---|
   | `IG_USER_ID`, `IG_ACCESS_TOKEN` | yes | Instagram Business/Creator account; use a long-lived *Page* token |
   | `GEMINI_API_KEY` | recommended | Google AI Studio key |
   | `YT_COOKIES` | if YouTube blocks | contents of a Netscape cookies.txt from a throw-away Google account |
   | `YT_PROXY` | if YouTube blocks | e.g. `http://user:pass@host:port` (residential) |
3. Settings -> Actions -> General -> Workflow permissions -> **Read and write**.
4. In `config.yaml` set `permission: true` ONLY for creators whose content you are authorized to republish. Do not enable the example channels without permission/licensing. For best reliability, test using your own or explicitly licensed source videos first.
5. Actions -> **podcast-clipper** -> Run workflow:
   1. **check-live** - diagnostic only. It verifies YouTube download, a real 9:16 render, QA,
      Gemini and Instagram resumable upload. It deliberately does **not** enqueue clips or call
      Instagram `media_publish`; a successful green check-live run does not post anything.
   2. **produce** with *dry_run* ticked - analyses a real episode and prints candidates without saving them.
   3. **publish-now** - runs the normal production tick immediately: it fills the queue when needed,
      renders and quality-checks a candidate, then attempts to publish one Reel without waiting for a slot.
      Check Instagram and the run log for the returned media ID. A clip can still be held for manual review
      or rejected by QA; those are intentional safety gates, not successful posts.
6. After the first successful post, the cron schedules run by themselves (05:50 / 17:50 IST start and wait
   for the exact slot; 07:50 / 19:50 IST are catch-up runs). Manual `tick` without *force* only posts when
   a slot is due; `tick` with *force* is the alternative to `publish-now`.

## Speaker diarization

Speaker diarization is enabled in `config.yaml`. It remains safely inactive until the Hugging Face
model is authorised and the repository has a token:

1. Accept the model conditions for [pyannote Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1).
2. Create a Hugging Face read token and add it as the GitHub Actions repository secret `HF_TOKEN`.

The workflow reads `diarization.enabled` directly from `config.yaml` and installs
`requirements-diarization.txt` only when diarization is enabled and `HF_TOKEN` is present. No
`ENABLE_DIARIZATION` repository variable is needed. Without the secret, diarization is skipped and the
base clipper continues normally. Model access approval is a one-time Hugging Face account action; a
token alone does not bypass the model conditions. Set `diarization.enabled: false` to turn it off.

Diarization runs on each selected clip's downloaded audio segment, not the entire source episode. If
the package, token, model, or inference is unavailable, the clipper logs the failure and continues with
the non-diarized layout.

The diarization result describes speaker count, turns, switching, dominance and overlap. It is currently
used as a conservative layout signal. Matching speaker identities to visible faces and cutting between
camera angles requires audiovisual speaker-to-face association and is not claimed here.

## If check-live says the YouTube download FAILED
GitHub's servers use datacenter IPs and YouTube often answers yt-dlp with "Sign in to confirm you're not a
bot". The workflow already starts a PO-token provider. Next steps, easiest first: (1) add `YT_COOKIES`,
(2) add `YT_PROXY`, (3) use a self-hosted runner (your PC / a VPS) by changing `runs-on` to `self-hosted`.

## How it works (an agent loop, not a script)
**Plan -> verify -> adapt**, with a fallback at every step:
1. **Choose** - transcript phrase boundaries + opening-hook and speech-density signals + Most-Replayed heatmap when available. A missing heatmap is treated as a neutral signal instead of discarding the episode. A missing/weak transcript does not produce arbitrary time-grid clips: the episode is skipped for retry/review. Gemini (text) judges up to 20 moments on hook, standalone clarity, payoff, emotion and specificity; Gemini (video) can review the shortlist.
2. **Captions** - Gemini fixes mis-heard words, names and punctuation (a safety guard discards rewrites that change meaning); captions are burned in word-by-word with the spoken word highlighted, plus an on-screen title.
3. **Verify before queueing** - a 3-second test download proves the episode is really downloadable. If not, the agent records why, schedules a retry in 12 h and moves to the next episode.
4. **Download ladder** - if YouTube rejects one yt-dlp client it automatically tries the others (default, tv, ios, mweb, any-format) and remembers which one works.
5. **QA before posting** - FFmpeg checks black frames, total silence and long continuous silent gaps; a failed FFmpeg QA run is a hard failure. Gemini reviews the final rendered clip and must return a valid typed approval plus a minimum score. If Gemini final QA is unavailable or rejects the clip, it is held for manual review, not auto-published. The MP4 is uploaded as a run artifact for 14 days.
6. **Slot catch-up** - four cron runs a day (two primary, two backup). If GitHub drops or delays a run, the next one posts the missed slot (up to 6 h late) and never posts a slot twice.
7. **Buffer** - the queue always holds at least one episode ahead (`refill_when_below: 3`), so a YouTube outage does not miss a post.
8. **Learning** - Instagram insights steer which channels are scanned first. Every decision is written to a journal shown in each run's summary.

If a clip needs human review, open the completed Actions run and download its `clip-review-<run id>` artifact. Then run the workflow with command `approve` or `reject` and the clip ID. Choose `force=true` with `approve` to publish immediately after the manual approval; otherwise it returns to the queue for the next slot. Technical QA still runs after manual approval.

If a publish request times out or has an ambiguous server response, the clip is marked `publish_uncertain` and is not retried automatically. First inspect the Instagram account. Use `confirm-post` with the clip ID and Instagram media ID when it did publish; use `retry-uncertain` only after verifying that it did not publish.

Posting is still not *guaranteed*: if YouTube blocks every download strategy for days, or Instagram revokes your token, the run fails loudly (red X + email from GitHub) instead of silently.

## Files
`podcast_clipper.py` the pipeline - `config.yaml` all settings - `.github/workflows/clipper.yml` schedule and manual runs -
`state/state.json` queue and results (created and committed by the workflow).

## Honest money maths
Instagram does not pay accounts for reposted/unoriginal content. Income must come from elsewhere (clipping
campaigns that pay per 1,000 views, affiliate links, your own product, brand deals). At about $1 per 1,000
views, $1,000/month needs about 1,000,000 views/month (roughly 17,000 per post at 2 posts/day).

## Creator quality checklist

Before switching from test mode to unattended posting, review at least 10 rendered clips and check:
- The first 1-3 seconds communicate a clear idea and do not depend on prior context.
- The segment includes its setup and payoff; the cut does not truncate a word or key answer.
- The speaker's face is large enough and not cut off; face position can move during a single-face shot, but no face identity or active-speaker-to-face matching is inferred. Review multi-person and fast-cut footage manually.
- Captions are accurate, synchronized, readable on a phone, and do not cover the speaker's mouth.
- The title is specific and truthful, not a promise the clip fails to deliver.
- Audio is clear and balanced; there are no black frames, long silences, or abrupt endings.
- Rights/permission and platform eligibility have been confirmed.

## How to improve using analytics

Compare posts at similar ages (for example, 24 hours and 7 days). Review reach, average watch time, completion/retention where available, shares, saves and follows. Do not compare raw views of posts with very different ages or source audiences. Test one major variable at a time (hook style, crop, caption density, or duration), and keep a record of the result.

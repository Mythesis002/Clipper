# AgentClipper upgrade notes

## Creator-quality and reliability pass

- Increased candidate window support to 15–45 seconds so clips can retain setup and payoff.
- Rebalanced pre-ranking so the opening-hook heuristic has meaningful influence without treating replay heat as a short-form success guarantee.
- Made the hook heuristic more conservative: reduced keyword/clickbait bonuses and penalized filler/context-dependent openings and tiny fragments.
- Expanded Gemini candidate review from 15 to 20 candidates and visual review from 6 to 8 (API usage may increase).
- Updated Gemini prompts to evaluate standalone clarity, setup/payoff, and first-seconds attention.
- Added a configurable, bounded foreground zoom for landscape sources (`render.foreground_scale`, default 1.16). This is not face tracking; centered crops can still cut off off-centre speakers.
- Added startup configuration validation.
- Made technical QA fail closed if the technical QA pass itself errors, rather than silently passing the clip.
- Expanded README with rights/permission guidance and a manual creator review checklist.

## Important limitations

This upgrade improves ranking and safeguards, but cannot guarantee views. It does not add true face tracking, automatic reaction cuts, B-roll generation, or a trained retention predictor. The first 10 rendered clips should be reviewed manually before unattended publishing. All example channels remain disabled (`permission: false`) and must stay disabled unless you have appropriate permission/licensing.

## Validation performed

- Python AST/syntax parse: passed.
- YAML parse and clip duration/zoom configuration sanity check: passed.
- Runtime integration tests and live YouTube/Instagram/Gemini tests were not run in this environment because the `yt_dlp` dependency and authenticated platform credentials were unavailable.

## Quality and reliability hardening

- Missing YouTube replay heatmaps no longer disqualify an otherwise eligible episode; the replay score becomes neutral.
- Missing transcripts no longer create arbitrary fixed-grid clips without editorial context or reliable captions.
- Caption correction preserves the original word timestamps and is rejected when token count changes or the rewrite diverges too much.
- Gemini final QA now validates response types and score; unavailable, malformed, or low-scoring review enters manual review by default.
- FFmpeg QA checks command exit status, media streams, black-frame share, total silence, and long continuous silent gaps.
- Soft-rejected clips are never auto-published. Rendered review MP4s are exposed as GitHub Actions artifacts with approve/reject commands.
- Instagram publish timeouts or ambiguous responses are quarantined to avoid duplicate posts; confirmed outcomes can be reconciled manually.
- Added conservative single-face framing for landscape sources, with the blurred full-frame composition retained as the fallback for group/uncertain shots.
- Added startup configuration validation, regression tests, and a separate credential-free CI workflow.

### Remaining creator-editing limitations

This is not yet a full professional NLE/AI editor: it has no continuous active-speaker tracking, diarization-driven camera switching, reaction inserts, B-roll search/generation, semantic sound design, or learned retention predictor. The new face crop is static per clip and should be evaluated on real source footage before unattended use. These changes improve selection and operational safety; they do not guarantee viral performance.

## Editorial intelligence and performance learning

- Added a transcript-context engine that inspects words before and after each candidate without pretending those words are in the clip.
- Added opening-type, context-dependency, standalone-clarity and payoff signals to the ranking and Gemini editor prompt.
- Added empirical hook-type and duration priors with a four-post minimum sample and conservative shrinkage.
- Added optional pyannote Community-1 diarization for each rendered clip segment; speaker-turn count, switches, dominance and overlap inform safe reframing.
- Replaced the fixed face crop with a dynamic, interpolated face-position track; ambiguous group/dialogue footage keeps the full-frame fallback.
- Added retention-weighted performance scores using average watch time, reach-normalised shares/saves/likes and Reels skip rate where those insights are available.
- Source ordering now explores channels with insufficient performance data first, then prioritises mature retention/engagement quality rather than raw views.
- Channel and format learning sample requirements are configurable in `config.yaml`.
- Added editor-intelligence, analytics and real FFmpeg render integration tests.

### Optional diarization setup

- Accept the Hugging Face conditions for `pyannote/speaker-diarization-community-1`.
- Add the `HF_TOKEN` Actions secret and `ENABLE_DIARIZATION=true` repository variable.
- Set `diarization.enabled: true` in `config.yaml`.
- PyTorch/pyannote are installed only when explicitly opted in. Without the token/model, the pipeline continues without diarization.

### Important boundaries

The current face track follows a visible face position; it does not identify people or conclusively match a diarized voice to a face. The diarization output is used as conservative layout evidence, not as a multi-camera edit decision. No universal view or retention benchmark is hard-coded as a prediction of virality.



## Gemini API model pinning and timeout reliability

- Pinned Gemini requests to `gemini-2.5-flash`; the runtime no longer falls through to other model IDs or auto-discovers a different Flash model.
- Added startup validation so a different `llm.model` is caught before the pipeline runs.
- Added bounded retries for connection/read timeouts and transient HTTP 408/429/5xx responses.
- Uses shorter read timeouts for text requests and a longer allowance for video inputs; retries stay on the same model.
- Added regression coverage for model pinning, disabled model discovery, and timeout retry behavior.

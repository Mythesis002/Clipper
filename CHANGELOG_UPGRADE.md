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

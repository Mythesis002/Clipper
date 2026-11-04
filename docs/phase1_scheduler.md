# Clipper Phase 1 scheduler runbook

Phase 1 replaces best-effort GitHub cron publishing with PostgreSQL slot scheduling. **Do not enable it on a production account until the manual verification checklist is complete.** The dispatcher publishes only slots whose clip is rendered, QA-passed, media-verified, and container-ready before T-0.

## Architecture

- PostgreSQL is the scheduling clock (now()), slot/job source of truth, lease store, and audit log.
- planner materializes 7 days ahead, assigns clips at T-6h, enqueues T-30m media verification, and enqueues container upload at T-10m.
- worker handles source production, download/render/Gemini QA, object-storage upload, ffprobe verification, and Instagram container creation/polling.
- dispatcher polls PostgreSQL, claims due slots using FOR UPDATE SKIP LOCKED, waits against monotonic time, and at T-0 only calls media_publish.
- YouTube upload is queued only after Instagram succeeds and runs as a separate job.
- Object storage stores rendered MP4s so worker replicas do not depend on local files.
- GitHub Actions is CI/manual diagnostics only; production scheduling is not cron-driven.

## Local setup

Requirements: Docker Compose, Git, Python 3.12 for bootstrap scripts.

1. Copy env.example to .env and set secure secrets. Do not commit .env. Create the key file with the command in env.example; keep it chmod 600 and outside version control. For production, use a secret manager or mounted secret file.
2. Create a UUID for the Phase 1 user and set CLIPPER_USER_ID.
3. Set CLIPPER_USER_EMAIL, IG_USER_ID, IG_ACCESS_TOKEN, GEMINI_API_KEY, and optional YT_TOKEN_JSON/YT_CHANNEL_ID. These platform tokens are bootstrap-only inputs; they are encrypted before database storage and are not passed to long-running containers.
4. The generated token key file is required for bootstrap and mounted as a Docker secret for worker/dispatcher. Losing it means stored tokens cannot be decrypted and accounts must reconnect.
5. Set SOURCE_RIGHTS_ATTESTATION to a truthful statement that you own, licensed, or have explicit permission to repost each enabled source. Bootstrap intentionally refuses to attest on your behalf.
6. Install Python dependencies: python -m pip install -r requirements.txt -r requirements-scheduler.txt
7. Load bootstrap variables in the shell: set -a && source .env && set +a
8. Start PostgreSQL and MinIO only: docker compose up -d postgres minio
9. Apply migrations (fresh Postgres volume applies them via container entrypoint; for an existing DB, apply both):
   - psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_scheduler.sql
   - psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_scheduler_seed.sql
10. Bootstrap tenant, source attestations, Instagram account row, and local config schedule: python scripts/bootstrap_user.py
11. Back up JSON state and import into Postgres:
   - cp state/state.json state/state.json.pre-scheduler.bak
   - python scripts/import_legacy_state.py
12. Start services: docker compose up -d --build --scale worker=4 --scale dispatcher=2
   Four worker replicas are a starting point for 1,000 scheduled posts/day; benchmark actual render/QA duration and scale workers before enabling that volume. Compose defaults to 32 publishing threads per dispatcher (64 across two replicas). This is intentionally conservative until connection-pool and API-rate load tests justify raising it. Some Compose versions ignore deploy.replicas; use explicit --scale flags.

## Phase 1 environment

- CLIPPER_STATE_BACKEND=postgres: opt-in legacy pipeline state adapter. JSON remains default for existing unit tests and rollback.
- CLIPPER_USER_ID: UUID created during bootstrap.
- IG_ACCESS_TOKEN and YT_TOKEN_JSON are bootstrap-only. Bootstrap encrypts them with AES-256-GCM into connected_accounts.token_ciphertext. Runtime services decrypt only in memory. The master key is mounted as a secret file; keep it in a secret manager, rotate with a planned re-encryption procedure, and never log or commit it.
- S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET: MinIO locally or S3-compatible storage (Cloudflare R2/AWS) in deployment.
- PUBLISH_CONCURRENCY: per dispatcher process. 150 x 2 replicas = 300 maximum in-flight slots.
- Provider buckets/circuit breakers are global PostgreSQL state. Defaults (Meta 100 req/s with burst 300; Gemini 5 req/s with burst 10; YouTube 1 req/s with burst 10) are Clipper-side throttles, not provider quota claims. Tune them against live response headers and error rates.
- MAX_LATE_MINUTES: default 20 in bootstrap; per schedule rule in PostgreSQL.

## State transitions

scheduled → assigned → preparing → verified → container_uploading → container_ready → publishing → published

Terminal/hold states: missed, failed, publish_uncertain, cancelled.

- Render/Gemini/QA is a worker job and never executes inside the dispatcher.
- A reclaimed `prepare_clip` job resumes only when its slot is still `preparing` for the same clip; per-slot advisory locking serializes duplicate/reclaimed prepare attempts.
- A QA or permanent Instagram processing rejection quarantines the clip and attempts replacement from the user's permission-attested ready queue.
- At publish, slot CASes to publishing and appends publish_started before the API call.
- Timeout/5xx/unknown response becomes publish_uncertain; no automatic retry is allowed.
- A slot past run_at_utc + max_late_minutes becomes missed and will not be published late.
- Schedule/timezone edits cancel only future unclaimed scheduled slots and rematerialize them. Assigned or publishing slots are not moved.

## Run tests

Offline regression suite: python -m unittest discover -s tests -v

PostgreSQL scheduler tests (migration must be applied first):
TEST_DATABASE_URL="$DATABASE_URL" python -m unittest tests.test_scheduler_postgres -v

CI runs existing tests and PostgreSQL integration tests against PostgreSQL 16.

## Manual verification checklist (before production cutover)

- [ ] Back up state/state.json and current GitHub Actions secrets.
- [ ] Verify each enabled creator's permission and submit the source-rights attestation.
- [ ] Bootstrap one test tenant and inspect sources, schedule_rules, connected_accounts.
- [ ] Run materializer twice; second run should create zero duplicate slots.
- [ ] Test a slot in the next few minutes using a test Instagram account only.
- [ ] Confirm T-6h render/QA completes by worker; inspect S3 object and QA report.
- [ ] Confirm T-30m verification is a separate worker job.
- [ ] Confirm T-10m container creation is completed and quota snapshot is saved.
- [ ] Confirm dispatcher logs show only media_publish at T-0 (no rendering, Gemini, upload, or quota GET).
- [ ] Kill dispatcher after it claims a slot but before publish; verify another dispatcher can recover only if state never reached publishing.
- [ ] Simulate timeout after publish_started; verify publish_uncertain and no retry.
- [ ] Confirm Instagram success enqueues independent YouTube job; fail YouTube and verify Instagram slot remains published.
- [ ] Verify concurrent slots for one account serialize with advisory locks.
- [ ] Run 500-slot PostgreSQL burst test and inspect DB/dispatcher CPU, connections, and p95 lag.
- [ ] Do not merge/cut over until CI is green and a test account completes the lifecycle.

## Known limitations (Phase 1)

- Single tenant/account credentials via environment secrets. OAuth, per-account encrypted token storage/KMS, refresh, notifications, admin CLI, dead-man monitoring, and multi-tenant API are Phase 2/3.
- Strict exactly-once external publishing is impossible to prove after an accepted request whose response is lost unless Meta provides a supported idempotency key. Clipper chooses safety: no automatic retry in publish_uncertain; reconciliation/manual confirmation is required.
- Quota endpoint is checked at T-10 before container upload. T-0 intentionally performs only media_publish; quota may change in the gap if an external publisher uses the same account.
- A live 300-account publish burst and p95 10-second/99.9% within-30-second SLA have not been measured against Meta API or production infrastructure. DB contention tests are not proof of external publishing SLA.
- YouTube upload recovery after process crash is conservative: an in-progress upload is marked uncertain to avoid duplicate Shorts. Manual reconciliation is required.

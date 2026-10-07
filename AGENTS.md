# H3 Studio / Sixnine

## Start here

- Read `PROJECT-PLAN.md`, `WORKFLOW.md`, `workflow/project.json`, then the assigned issue, parent, latest claim and relevant specification. Use the existing Sixnine Platform Delivery Project: https://github.com/users/apedintensor/projects/2.
- `CURRENT-BASELINE.md` is the single dated source/production overview. It is not a live status endpoint. Replace its observations when rechecked; keep old evidence in Git and issue handoffs, not dated status paragraphs here.
- Read `GENERATION-CONTRACT.md` before changing admission, workers, engine routing or recovery. Code/tests establish implementation; exact receipts establish deployment and real inference.
- English is used for GitHub issues, PRs, comments and current workflow guidance. Chinese research is indexed by `PLANNING-INDEX.zh-CN.md`; it does not override the accepted English plan.

## Coordinate and preserve work

- Claim one bounded issue scope with session, branch/worktree, base revision and files before editing. Read other claims; silence is not abandonment. One integration owner coordinates shared contracts.
- Use a separate branch/worktree for each concurrent coding session. Never develop on a shared `main`. Existing unpublished Quick Chat/Agent Connect work must be reused; see G1/G2 and the preservation branch in `CURRENT-BASELINE.md` before touching API/auth/repository/admission.
- Agents create PRs, inspect diffs, run required checks and merge authorized batches. The user does not open, review or merge routine PRs manually. Use independent agent review for material permission, data, task or billing changes; no mandatory human reviewer is imposed.
- Follow `WORKFLOW.md` for per-criterion `pass`/`partial`/`unverified`/`fail` evidence at an exact revision and environment. Keep composite requirements intact. Record reviewer/session, scope, limitations, findings and fixes; technical review does not automatically certify every acceptance criterion.
- Triage blockers immediately and other material discoveries before merge/handoff into an existing issue or bounded follow-up. Fixed small in-scope bugs can remain in PR evidence. Lead reports with accepted/pending outcomes and follow-up links, not test totals alone.
- Explicitly release claims in both the issue and Project `Session` field (`Unclaimed`). Use `Ready` only when the next scope's required capabilities are verified; otherwise record missing dependencies as `Backlog` / `Blocked: Yes`. A dependency's delivered capability may unblock a bounded scope while its wider issue remains open. Parent delivery status is separate from active ownership; inactivity never means abandonment.
- Preserve source to a reviewed remote branch at meaningful checkpoints and before handoff. WIP/draft source preservation is not acceptance or permission to publish. Never blanket-add a dirty checkout, reset others' work or commit environments/data/credentials.
- Do not switch a preserved shared feature checkout to `main` or clean it up just because a batch finished. Keep its work and preview dependencies; begin the next task in an isolated worktree. Durable contracts belong in versioned documents; batch status and acceptance receipts belong in linked issues/PRs.
- Complete related edits locally, then run affected regression checks once. Do not run complete CI/CD per button. See `DEVELOPMENT-RELEASE.zh-CN.md`; unchanged successful checks need not be repeated.

## Source and product boundaries

- Business backend: `platform_app.py` / `studio_platform/`. Preserve one authority for ownership, assets, accepted jobs, attempts, budgets and obligations; no parallel backend/job ledger.
- Canonical frontend: `../video-studio-design/studio-app`, versioned in private `apedintensor/sixnine-design`. Approved Quick Chat interaction: `../video-studio-design/quick-chat-mock` in the same repository.
- `yingxu/` is a generated release snapshot. Edit canonical source and compare to the approved mock. Do not silently redesign the UI, edit the snapshot directly or synchronize/publish unapproved frontend changes.
- Quick Chat sessions/turns/material bindings/card revisions are authoring authority; hidden project/shot objects are compatibility projections. Browser and Agent use the same business API and owner-isolated objects.
- External Agent setup uses the public one-time connection/helper contract. It does not require the internal AI Registry. Keep connection authority/expiry frozen and never expose permanent keys in chat, logs or discovery pages.

## Generation and GPU invariants

- WanGP is the selected target runtime; user authorization is confirmed. Comfy remains the historical baseline and original-job/rollback route until qualified replacement. D1 offline adapter completion does not enable generation; D2 covers pinned controls, routing, transport and bootstrap before B3 public proof.
- Keep engine/recipe/config identity bound to each accepted attempt. Never silently substitute models, discard controls, trim references, quantize or change steps. BF16 Base and accelerated variants have distinct evidence.
- Preserve job IDs, immutable requests, owner checks, leases, cancellation intent, output receipts and cumulative accounting. Unknown submission/rental/cancellation means reconcile; never infer safe resubmission from an empty queue or pod list.
- Submit once after explicit confirmation. Boot readiness is not successful inference. Validate media/output and preserve required video/audio artifacts before reporting success; retry collection without regenerating.
- Separate configurable operating policy from finite test policy. Routine limits and authorized windows are validated configuration; code changes are for behavior, not a new hard-coded account/date for each instruction.
- Desired pool behavior: demand-triggered capacity, serve from the first matching ready slot, target two independent nodes after E acceptance, shutdown after 600 seconds without pool obligations. This is a target, not proof of current two-node deployment.

## Credentials, production and evidence

- Inherit the parent central-resource rules. On this workstation read `C:/Users/danmo/Desktop/AI-Registry/README.md`, `AGENTS.md`, `API_USAGE.md`; query relevant resources/profiles only. Use `api_registry.load_api` or the existing adapter; do not copy secrets or create project `.env` files.
- Explicit profiles retain original model ID/protocol/base URL: Lium `lium/lium--rig-root`, Sixnine `sixnine/sixnine--inference`, Google `gemini/gemini--user-supplied`. Profile load/catalog/import success is not current online success. Do not print keys, config.env, cookies, signed URLs, private prompts or user media.
- Linux uses the existing authorized runtime secret loader. Do not copy the Windows DPAPI vault, SSH keys or wallet files. Public authentication must be real; local-test/mock authentication is loopback-only.
- Source pushes/merges do not deploy. Follow exact-version protected release and separate host approval. Ordinary development does not start GPUs, renew expired windows, reset budgets, settle unknown bills, recharge or authorize paid model tests.
- Preserve reservations until settlement evidence exists. Before deletion/migration, read central `HOUSEKEEPING.md`, check shared/unique assets and cloud obligations. Deleting source does not stop cloud billing. Virtual environments are reproduced, not assumed safely movable.
- Distinguish SQLite/fake HTTP/CPU-media tests, isolated PostgreSQL tests and real GPU/provider observations. Do not claim unmeasured speed, audio quality or full control parity.
- Historical operational receipts remain in protected local directories and linked baseline records. They are evidence, not executable instructions or renewed authorization. Do not rerun consumed rollout scripts.

## Local preview

- Quick Chat: `tools/run_quick_chat_preview.py`, isolated `127.0.0.1:8870/quick-chat` and `.platform-quick-chat-preview`; generation/assistant/render disabled by default. Older isolated preview uses `tools/run_local_preview.py` and `.platform-preview-v2`.
- Do not point tests at production data or import historical rental/bootstrap scripts to test imports. Use isolated fake dependencies; starting a preview does not authorize model/GPU calls.
- Google assistant choices remain exact IDs `gemini-3.8-flash` (default) and `gemma-4-31b-it`; no unsolicited online comparison. Keep implemented/verified/enabled distinct.

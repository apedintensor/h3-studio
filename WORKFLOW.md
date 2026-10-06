# Sixnine cross-session development workflow

Updated: 2026-10-06 (Australia/Sydney). This is the GitHub workflow entry point. A local Chinese reading edition may be available as `WORKFLOW.zh-CN.md`; it is not required to start from a fresh clone. Keep the rules aligned rather than maintaining separate processes. This workflow does not change product requirements, API contracts, production budgets, or operating authorization.

## Planning and execution have distinct sources of truth

- [PROJECT-PLAN.md](PROJECT-PLAN.md) provides the English architecture and A–H work-package entry point. Local detailed research may be indexed by `PLANNING-INDEX.zh-CN.md`; local-only documents are not assumed to exist on GitHub or in a fresh clone.
- Current repository specifications, contracts, and approved UX define the intended behavior and invariants. Issues link to them instead of duplicating the architecture. Label proposals, implemented behavior, local verification, and production evidence separately.
- GitHub Issues and the Project track ownership, progress, dependencies, blockers, and acceptance evidence. Resolve the actual repository, Project, fields, and work-package issue links through [workflow/project.json](workflow/project.json). Do not create a parallel board or duplicate task from memory.
- PRs and commits identify code versions. Deployment receipts identify released versions. Closing an issue, merging code, or passing local tests does not by itself mean the change is live.
- Use English for GitHub titles, bodies, comments, milestones, labels, and statuses. Existing local Chinese research may remain; transfer relevant requirements into the English issue-linked specification. Do not link unpublished local files as if they were accessible on GitHub.
- The user's latest explicit instructions take precedence over an older plan. Record resulting scope and dependency changes in the existing issue and relevant specification. Do not ask again for an action already clearly authorized.

## Start a new session

Read **applicable AGENTS → PROJECT-PLAN → this workflow → the assigned issue, its parent work package, and relevant specification**. Expand only the contracts, code, and evidence needed for that task. Local sessions may also consult `PLANNING-INDEX.zh-CN.md` for detailed historical research.

Use `workflow/project.json` to locate the actual remote work items. Read the latest claim, comments, PRs, dependencies, and status before proceeding. A screenshot or a past chat saying “done” does not replace these records. Reconcile discrepancies rather than assuming the current production state.

This entry point depends on project context. **A completely new session outside this project does not automatically know this repository or board.** The user must provide a repository, issue, or Project link, or open the project. A shared board also does not authorize a session to message other sessions automatically.

For a session without project context, paste:

```text
Continue work in https://github.com/apedintensor/h3-studio.
Read AGENTS.md, PROJECT-PLAN.md, WORKFLOW.md, and workflow/project.json.
Check the Project, current claims, and dependencies; pick the highest-priority Ready task
unless I assign a specific issue. Claim a bounded scope before editing and leave a handoff.
Preserve other sessions' changes and existing operational limits. Use English on GitHub.
```

## Work packages and near-term order

| Work package | Outcome |
|---|---|
| A — Baseline and contracts | Verify existing entry points, versions, capabilities, states, and contracts |
| B — Minimum generation loop | Complete submission, on-demand capacity, generation, persistence, and download for the same job |
| C — Failure recovery | Handle duplicate submissions, unknown outcomes, cancellation, restart, and artifact collection |
| D — WanGP runtime integration | Integrate upstream WanGP through an adapter and qualify it against the fixed baseline |
| E — Dual-GPU redundancy | Isolate members and budgets, serve from the first ready member, and release idle capacity |
| F — Object storage and recovery | Establish independent backups, object-storage migration, and recovery |
| G — Scenario integration | Connect approved Quick Chat, Yingxu, and other scenarios to the shared backend |
| H — Collaboration and scale | Add collaboration permissions and scale only when supported by demonstrated needs |

The near-term sequence is **A1 → A2 → B plus D integration → C recovery acceptance → public B/D proof**, then E/G. C semantics are designed alongside B/D. Use [CURRENT-BASELINE.md](CURRENT-BASELINE.md), [GENERATION-CONTRACT.md](GENERATION-CONTRACT.md), the plan and board for evidence and dependencies. Authorized local UX work can proceed independently, but a completed page cannot substitute for generation-loop acceptance. Split parent work packages into bounded, verifiable vertical slices rather than assigning “the entire frontend” or “the entire backend.”

## Claim work before editing

1. Find the existing issue for the outcome. Confirm its parent work package, current versus expected behavior, relevant specification, and acceptance criteria. Related low-risk changes can share one batch issue; every button does not require a separate process.
2. Inspect dependencies and concurrent work, then claim the task. Record the session, branch/worktree, base commit, change boundary, dependencies, and next checkpoint. If the checkout is shared, state that explicitly.
3. Inspect current workspace changes and shared API, schema, and execution contracts. Different files can still change the same contract; coordinate with its owner before implementation.
4. Set the corresponding Project status to `In progress`. Re-read the latest claim before starting: GitHub comments are not an atomic distributed lock. If claims or changes overlap, preserve existing work and coordinate. Do not overwrite, take over silently, or infer abandonment from inactivity.
5. If implementation changes the scope or contract, update the same issue/specification and its dependency impact. Do not generate another competing “final design.”

Use a short claim or resume comment:

```text
Claim / resume
- Session: <session title and ID>
- Checkout: <branch and worktree, or shared checkout>
- Base revision: <commit>
- Scope: <files/modules and shared contracts>
- Depends on: <issue links, or none known>
- Next checkpoint: <concrete deliverable>
- Verification: <offline/local/production; allowed boundary>
```

Every work item has one explicit integration owner. Parallel sessions use bounded child tasks with coordinated scopes. Separate worktrees do not eliminate contract conflicts. Commit only your own changes; do not reorganize another session's unfinished files.

## Implement and verify a batch

- Edit canonical sources. The frontend source is `../video-studio-design/studio-app`; `yingxu/` is a generated release snapshot. Follow the current AGENTS rules for snapshot synchronization and local UX acceptance.
- Follow [DEVELOPMENT-RELEASE.zh-CN.md](DEVELOPMENT-RELEASE.zh-CN.md): complete a related batch, then run affected regression checks. During development, run necessary checks without repeating unchanged full CI/CD suites for every small edit.
- Preserve account isolation, job identity, budget ledgers, immutable inputs, and existing assets. An unverified `unknown` outcome does not justify duplicate submission or rental.
- Creating an issue, moving a card, or accepting a PR does not authorize paid API calls, GPU startup, increased budgets, production deployment, data migration, or deletion. Check existing user authorization and release rules before such actions; report missing authorization clearly.
- Never put API keys, connection codes, tokens, cookies, passwords, signed URLs, private prompts, or user media in issues, PRs, comments, or attachments. Use redacted error codes, versions, aggregate evidence, or approved synthetic media. Keep sensitive receipts in protected locations and reference them without secrets.
- If GitHub is unavailable, independent authorized work without ownership conflicts may continue. State that remote status is not synchronized. Do not claim that an issue was assigned or a board was updated unless that operation succeeded.

## Pause or hand off

Update the original issue with completed work, the exact commit or uncommitted files, verification results, blockers, next steps, and any running operations. State what remains unverified, particularly paid calls, GPU execution, deployment, and migration.

Before ownership changes, identify the change boundary and unfinished operations. The receiving session checks the latest code and evidence before proceeding. A handoff does not cancel accepted jobs, restart instances, reset ledgers, or extend authorization windows. A brief absence does not release ownership; a transfer or relinquishment must be explicit.

## Finish and accept

Record the following in the original issue/PR:

1. Which acceptance criteria passed, with checks and environment; keep failures and unverified items explicit.
2. The commit/PR, specification and contract updates, and compatibility impact. Code or mock tests are not proof of production success.
3. Remaining work with linked follow-up issues and dependencies.
4. Release state and applicable rollback evidence. Use `Not released` if deployment has not occurred.

Project fields are:

- `Status`: `Backlog`, `Ready`, `In progress`, `In review`, `Done`.
- `Blocked`: `No`, `Yes`; include the reason, dependency, and next action when blocked.
- `Release`: `Not required`, `Not released`, `Released`.

Read actual field IDs from `workflow/project.json`. `Done` means the acceptance criteria for that issue are satisfied. If those criteria include deployment, the issue cannot be completed without deployment and the required verification. A local-only deliverable can be completed while release remains a separate work item.

Evidence must distinguish `Local verified`, `Released`, and `Production verified`. Use the existing release process when deployment is required. Deployment receipts establish release facts; test counts, merged PRs, and board status do not establish online availability.

At batch acceptance, the integration owner checks A–H dependencies, duplicate work, and contract differences, then updates the existing plan with material discoveries. Sessions do not need to rewrite the entire architecture, and document count is not a progress metric.

# Quick Chat API

Use the user's exact origin and authenticated `GET /v1/quick-chat/schema`, `/v1/capabilities`, `/openapi.json` for current fields/limits. Model IDs, profile IDs and resource IDs are different. Do not guess model availability from this document.

## Create, upload, edit

1. `POST /v1/quick-chat/sessions` with title/model_id and stable `Idempotency-Key`; persist the returned id/version/web_url. Creating a session does not rent GPU.
2. `POST /v1/quick-chat/sessions/{id}/assets` multipart `file` and stable `client_asset_id`. Upload receipts are not browser entity IDs. Preserve an uncertain upload ID and recover that receipt; wait for `ready`. Bind intended ready receipts through `PUT /.../{id}/materials` with `expected_version` and the full binding catalog. Disabled bindings remain in the catalog. Get current session/materials before replacing them.
3. Save user turns through `POST /.../{id}/turns` with expected_version, text, exact model_id and assistant_mode. Use `assistant_mode: "none", create_card: true` to atomically save the original input and one editable draft card; the response includes its `card_id`. This uses the turn's frozen materials and next_settings, makes no assistant call, and never starts generation. Keep the original Idempotency-Key/body when recovering a lost response. Legacy `none` without the optional boolean flag only saves text. `create_card: true` is rejected with assist/discuss. `discuss` only discusses; default `assist` may propose a card and needs assistant:run plus runtime enablement. A model suggestion is not execution authorization. Inspect media_input_manifest before claiming the assistant saw a file.
4. Alternatively `POST /.../{id}/cards` with a complete prompt, recipe_id, controls, inputs, copies. Inputs must use authorized session bindings. First/last frames use `{asset_id}`; image/video/audio entries use asset_id and the schema's purpose/range; guides use media_id/time_seconds/use_audio. All uint64 seeds are decimal strings, never JS numbers. `copies` is multiple independent jobs, not native model batching.
5. Edit via `POST /.../{id}/cards/{card_id}/revisions` with expected_card_version and the complete snapshot. Revisions are immutable; next-round settings do not change old cards. Preserve prior versions and explicit lineage.

## Preflight, confirmation and results

6. `POST /.../{id}/revisions/{revision_id}/preflights` with capabilities_version/revision_hash. Inspect exact effective settings, blockers, outputs, expiry and the per-item/total reservation estimate. This may perform CPU media selection but never rents GPU.
7. Only within user authorization, `POST /.../{id}/revisions/{revision_id}/submissions` with preflight_id/revision_hash/confirmed=true and a persisted logical key. One revision has one submission across web/Agent credentials. On a lost response, replay original key/body; never create another paid request to test success.
8. Poll `GET /.../{id}/submissions/{submission_id}` with bounded backoff, independently of timeline changes. Inspect each item's actual job/status/artifacts/safe error. Timeline cursors recover creative history, not a substitute for active job polling.
9. A stopped failed execution may use `POST /.../{id}/submissions/{sid}/items/{item_id}/retry` with retry_of_execution_id/fresh_preflight_id/confirmed=true. New preflight covers only that item. Unadmitted items use `/resume-admission` and retain the original item/job. Successful or unresolved items must not be repeated. Cancellation is a request until the upstream stop is proven; unknown billing is not free.
10. Results become references only via explicit `POST /.../{id}/result-imports` with source_artifact_id/purpose/optional source_range. Wait for ready/asset_id, then bind it. An output URL or adopted story entity is not automatically an H3 input receipt.

The existing streaming file helper also accepts an OS connection: `python scripts/sixnine.py --base-url ORIGIN --connection CONNECTION_ID upload --session SESSION_ID --asset-id STABLE_CLIENT_ID --file LOCAL_FILE`. `resume-upload --session SESSION_ID --asset-id STABLE_CLIENT_ID` reconciles that original receipt. Use its existing `download PATH --output NEW_FILE --sha256 RETURNED_SHA256` command for artifacts; signed storage redirects use the separate credential-free client. Do not combine --connection with an environment/Registry key. All arguments above are nonsecret identifiers/paths; the helper loads the PAT only in memory.

Return the session's actual `web_url` on the same origin, IDs and actual outcome. The user can review the same cards, results and edits on the website. Content URLs remain authenticated; verify downloads against returned SHA-256 rather than inventing successful media.

Preflight items may return a stable `error_code` and an audited `error_message`; show the safe message without echoing provider exceptions. `reference_video_exceeds_output` requires a longer output or an explicitly shorter source selection. When `estimate_available` is false, the cost is unknown, not zero; do not present the summed placeholder as a quote. Original assets remain unchanged. Unbound disabled derivatives with `asset.parent_id`, `asset.selection` and no `asset.client_asset_id` are internal selections, not newly uploaded user references.

## Conflicts and authorization

Every write command uses a stable logical Idempotency-Key; same key/different body conflicts. Replay recovery precedes version checking. On version_conflict preserve the draft, read current state and deliberately reconcile. On stale preflight, check again; do not silently confirm. Unknown model/video/storage operations reconcile original records, never automatically re-POST.

The connection helper stores the Key in OS protection and sends it only in Authorization on the exact origin, without redirects. Revoke through the website to block future access; accepted jobs remain. A new scope/profile version does not expand older keys or pending codes. Creating a code requires the owner browser session; an Agent cannot grant itself another credential.

Current local release validation uses isolated test adapters and CPU media. It does not establish live Gemini/Gemma understanding or H3/GPU speed; runtime schema reports implemented/verified/enabled separately.

# Consumer workflow: decisions through acceptance

Use this for external Orca workers. Managed native execution retains its separate
[managed lifecycle](MANAGED.md). Neither a model flag nor these caller-reported
records proves native account binding.

## Keep task packets bounded

Name the target, change, invariants, exclusive file ownership and observable
acceptance checks. Supply relevant source pointers, rather than asking every
worker to rediscover the repository. For three or more workers, plan a wave,
dispatch that wave only and account for actual launches.

## Preserve the exact decision and launch

`route --json` and planner decisions include `decision_id`. The durable record
contains the exact UTF-8 task-text hash, the coordinator project, a hash of
routing source bytes (including local uncommitted changes), the effective
config hash, task tier/floor, review requirement and selected
provider/model/effort/family. A new route receives a new decision ID. These are
policy evidence, not credentials or authenticated caller identity. Raw task
text is not stored in this ledger.

The coordinator project is captured from an explicit `--project` argument on
`route`, `rerun`, `plan` and `report` when given; otherwise it is the resolved
cwd at decision time. Explicit `--project` is canonicalized once via
`outcome_cli.canonicalize_project` (resolve + abspath); a blank value is
rejected, never silently defaulted to cwd. A launch inherits the decision's
recorded project unless an explicit `--project` is passed, and an explicit
project that disagrees with the recorded decision fails before write
(`outcome_cli.resolve_project`). The launch inherits the decision's project
across cwd changes; it does NOT inherit actual provider/model/effort —
the operator passes those explicitly, and the launch validator rejects
any silent default from the decision selection without an
`--override-reason`. The audit filter matches the recorded project
exactly; a child worktree's path is not authoritative.

After a confirmed external launch:

```sh
rightsize report minimax --started --model MiniMax-M3 \
  --task "EXACT full text that was routed" --dispatch ctx_example \
  --worktree /absolute/worker/path --decision-id DECISION_ID \
  --project /absolute/coordinator/project \
  --override-reason "User explicitly requested this model"
```

Pass `--effort` when applicable. Pass `--native-session` only when directly
observed. Preserve the actual provider prefix: direct MiniMax and OpenCode Go
MiniMax are different quota providers but the same model family. An override
reason is required when the actual choice differs, including when no model was
recommended. Overrides are recorded external actions, not managed admission.
The CLI records account status as unverified.

Older reports without a decision ID are retained as unlinked. No short-brief,
worktree-name or timestamp guess upgrades them to exact attribution. Unlinked
launches cannot be marked accepted. If a hook recorded a launch first, explicitly
repeat its exact receipt with the known decision ID: a one-time binding event
preserves the original record. Task hash, project, actual choice and launch
metadata must match. Historical records are not automatically backfilled. Exact repeats are idempotent; conflicting IDs fail.

## Record evidence before acceptance

The closeout manifest is the primary path for new dispatches. It records a
linked launch's completion, optional review, optional rework, optional usage
and acceptance in one atomic transaction. The existing per-event `outcome
record` path remains for incremental additions before closeout; the manifest
re-runs the same per-kind validators atomically and adds closeout-only checks
(decision/project consistency, evidence-file digest, post-accept immutability).

```sh
rightsize outcome closeout --manifest /path/to/closeout.json
```

The CLI takes the manifest file only; there is no `--root` flag. The
evidence root is fixed to the directory holding the manifest, and
`evidence_path` is resolved relative to it. Absolute paths and `..`
traversal are still rejected. The closeout CLI does not take `--project`:
the project, when asserted, is an optional `project` field inside the
manifest. `--project` is accepted on `route`, `rerun`, `plan` and `report`
only.

The manifest is a JSON document with `schema_version: 1`, a `dispatch`, an
optional `decision_id` and `project` for explicit binding, and an `events`
array. Each event carries `event_id`, `kind`, `data` and an optional
`evidence_path`. The manifest is capped at 64 KiB and 64 events; each
referenced evidence file is capped at 1 MiB. `evidence_path` is required on
hash-bearing events (`completed`, `failed`, `cancelled`, `review`, `rework`,
`accepted`) and is resolved relative to the directory holding the manifest;
absolute paths and `..` traversal are rejected. Usage events carry the
token categories in `data` directly and do not require `evidence_path`.
The store reads each referenced file with `O_NOFOLLOW`, verifies every
path component is not a symlink, confirms it is a regular file, computes
its SHA256 and requires it to match `data.evidence_sha256`. The store
walks the path components with `lstat` so a symlink at any depth — leaf or
parent directory — is rejected before any byte is read.

File hash checks prove the bytes on disk matched the recorded
`evidence_sha256`; they do not prove execution correctness, reviewer
identity or account binding. The store never re-runs the test, re-reads the
file beyond the single SHA256 pass, or authenticates the model that
produced the evidence. A reviewer name and a `native_session` are caller
attestation, not authenticated identity.

Example:

```json
{
  "schema_version": 1,
  "dispatch": "ctx_example",
  "decision_id": "DECISION_ID",
  "project": "/absolute/coordinator/project",
  "events": [
    {"event_id": "evt_done", "kind": "completed",
     "data": {"evidence_sha256": "<sha of evidence/completed.txt>"},
     "evidence_path": "evidence/completed.txt"},
    {"event_id": "evt_review", "kind": "review",
     "data": {"actor": "reviewer", "provider": "codex",
              "model": "gpt-5.6-luna", "family": "gpt-5",
              "verdict": "accepted",
              "evidence_sha256": "<sha of evidence/review.md>"},
     "evidence_path": "evidence/review.md"},
    {"event_id": "evt_usage", "kind": "usage",
     "data": {"source": "native-session-counter",
              "input_tokens": 12000, "output_tokens": 900,
              "cache_read_tokens": 50000}},
    {"event_id": "evt_accept", "kind": "accepted",
     "data": {"actor": "coordinator",
              "evidence_sha256": "<sha of evidence/acceptance.md>"},
     "evidence_path": "evidence/acceptance.md"}
  ]
}
```

Validation is all-or-nothing: any envelope, kind, evidence, decision,
project or review-floor check fails before any event is written. An event_id
already present with identical content is left alone (idempotent retry);
identical id with differing content fails the batch. Per-kind payload rules:

| Kind | Data fields |
| --- | --- |
| `completed`, `failed`, `cancelled` | `evidence_sha256` |
| `review` | `actor`, `provider`, `model`, `family`, `verdict` (`accepted` or `rejected`), `evidence_sha256` |
| `rework` | `actor`, `seconds` (nonnegative finite number), `evidence_sha256` |
| `usage` | `source`; optional `input_tokens`, `output_tokens`, `cache_read_tokens` (nonnegative integers, not booleans) |
| `accepted` | `actor`, `evidence_sha256` |

`evidence_sha256` is the lowercase SHA256 of retained review/test/artifact
evidence. The store records a caller attestation, not authenticated review or
automatic verification of the referenced file beyond the file-level SHA256
match. JSON rejects unknown fields and duplicate keys; do not include raw
transcripts, tasks or secrets. Unknown fields are a hard rejection, not a
silent drop.

`completed` records lifecycle success, never acceptance. Before reporting
completion, a worker must read queued coordinator messages and resolve or
explicitly report outstanding findings. The coordinator independently checks the
diff and acceptance evidence.

For incremental additions before closeout, or for adding a single event to a
launch that was not closed out, the per-event command remains:

```sh
rightsize outcome record --dispatch ctx_example --event-id UNIQUE_STABLE_ID \
  --kind completed --data evidence.json
```

### Required review and the cross-provider floor

High-stakes work always requires review; other tasks require it when the
second-opinion score reaches 0.6. The reviewer must use a configured model at
the recorded task floor with required capabilities. Acceptance requires the
review's `provider` to differ from the actual author provider AND the
review's `family` to differ from the actual author family: matching EITHER
blocks acceptance (`outcome_store.OutcomeStore._review_valid`). A missing
reviewer remains outstanding. Go MiniMax and direct MiniMax cannot review
one another independently. The cross-provider/family requirement is
enforced at acceptance, not at review-record time: a same-family `review`
event is stored if it passes per-event validation (configured profile,
family match, task floor, capability gate), but the `accepted` event
refuses when `review_required` is true and the latest accepted review
matches the actual author on either provider or family.

Caller claims of a different family are checked against the configured profile;
an invented family fails at parse time. The reviewer family must match the
profile's family, not a caller-supplied label.

When the user restricts a run to a MiniMax-only worker pool, that
restriction applies to that run. The cross-provider/family requirement is
unsatisfiable inside a MiniMax-only pool, so any review-required work in
the restricted run cannot record acceptance until a separate, out-of-pool
reviewer is dispatched. The coordinator does not widen the pool, substitute
a same-family reviewer, or reclassify the task to bypass review; the
blocker is stated explicitly instead. Future dispatches are not bound by
the current run's restriction.

### Acceptance and explicit evidence

Acceptance requires linked completion and explicit evidence. A rejected review
blocks acceptance. Required review must be accepted after the latest recorded
repairs. Record repairs and usage before acceptance; new events cannot silently
change an already accepted record. This gates Rightsize evidence, not external
Orca task settlement or deployment. Managed review retains its existing contract.

### Orca inbox draining before worker_done

Workers MUST process every row, ACK the delivery ID with
`orca orchestration check --ack <delivery_id>`, then drain the next batch
before sending `worker_done`. The same delivery can hold more than one
follow-up, and an unacknowledged row is replayed at the next check,
hiding later corrections while they pile up unread. The Orca CLI does not
mechanically refuse `worker_done` while rows remain unacknowledged; the
coordinator verifies that the worker actually consumed the queued guidance
and a run can appear settled without that having happened. Treating inbox
draining as a normative obligation before `worker_done` is how a worker
keeps its own record straight, not how the tool blocks bad endings.

### Native usage with exact session attribution

For repairs, record measured coordinator effort seconds and evidence. Do not use
elapsed waiting time between worker completion and the next launch as repair time.
The absence of a repair record is unknown, not proof of zero rework.

For example, a usage payload contains separate cumulative categories:

```json
{"source":"native-session-counter","input_tokens":12000,"output_tokens":900,"cache_read_tokens":50000}
```

Report the latest cumulative snapshot, not incremental deltas. The audit uses the
latest snapshot per dispatch, never sums repeated snapshots. Missing categories
remain unknown; an explicit zero is known zero. Cache reads are repeated context
processing, not unique context size or attributable subscription charges.

### UI and browser verification evidence

Claims that a UI change renders or behaves correctly must name the actual
component, the actual browser execution path, and a retained artifact. Examples
that satisfy the rule: a Playwright/Chromium run on the deployed preview with a
named selector and a screenshot, a recorded CDP `Page.captureScreenshot` plus
the network log, or a Vitest/Storybook component test whose story renders in
a real browser and whose snapshot is retained.

Examples that do not satisfy the rule: a unit test against a helper function
that mocks the DOM, a snapshot diff against a Storybook story without a real
browser launch, a build-success log alone, an HTTP-only `curl` against the
preview URL, or prose asserting "looks right" without a retained artifact. A
passing mocked helper does not count as browser proof even when the helper is
exercised by a UI test, because the rendered output was not produced; an HTTP
fetch proves the server returned the HTML, not that a browser rendered it.

Name the file path of the retained artifact, the test or script that produced
it, and the run command. If the artifact is a screenshot, reference its path
and the page URL plus selector. Missing artifacts stay unknown; do not mark the
claim accepted on assertion alone.

## Inspect performance and coverage

```sh
rightsize outcome audit --project /absolute/coordinator/project
```

Output is always JSON; no `--json` flag is needed. It reads the local
`outcomes.sqlite3` without creating it, probing providers or changing quota.
It returns decisions, launches and events filtered to the recorded project
path; `metrics` keys report denominators and known/unknown splits
(`total_launches`, `linked_launches`, `unlinked_launches`, `completed_launches`,
`accepted_launches`, `rejected_review_launches`,
`missing_required_review_launches`, `known_usage_launches`,
`unknown_usage_launches`, `known_rework_launches`, `unknown_rework_launches`,
plus `coordinator_repair` and per-category `usage_categories`), and `attempts`
returns per-launch status with `latest_review`, `latest_usage` and
`rework_events`. Task latency starts at the earliest recorded launch for the
same project/task hash and includes recorded retries up to acceptance. An
incomplete history is not a complete lifetime measurement.

Use independently accepted results, repair burden and comparable task families
for evaluation. Do not infer savings or model superiority from worker exit
status, token totals or unmatched samples. Runtime records are private; project
knowledge still belongs in the canonical Brain.

## What this cannot prove

- Account binding. A model name is not authentication, and a `native_session`
  recorded by the same process that runs the worker is attribution, not identity.
- Correctness. Recorded `accepted` events are caller attestations against
  evidence hashes; the store never re-runs the test or re-reads the file.
- Savings or comparative model quality. Accepted counts and token totals are
  inputs to evaluation, not conclusions; missing fields stay missing.
- Coverage. A smaller audit window or absent launches leave denominators
  incomplete; the audit reports known/unknown splits but does not impute zeros.
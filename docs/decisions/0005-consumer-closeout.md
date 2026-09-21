# Consumer closeout under MiniMax-only workers

Date: 2026-09-21. Parent #20; follow-up #29 to ADR0004 and #25/#27/#28.

The user approved four evidence improvements and explicitly required supervised
MiniMax Orca workers for consumer dispatches. This ADR closes the gap that
ADR0004 surfaced under that restriction and documents what the public docs
now record.

## User decisions

- Approved durable decision/launch identity and exact outcome events for
  external Orca workers (ADR0004).
- Approved independent acceptance evidence that retains known/unknown
  denominators; missing metrics stay missing, not implicit zero.
- Restricted this workstream's dispatches to supervised MiniMax Orca
  workers through the shared Rightsize installation. The restriction is
  scoped to the run the user restricted; it is not a permanent
  repository-wide rule.

## Implementation choices

The implementation owner delivered the closeout manifest contract and the
project-resolution helpers; details land in `rightsize.py`, `outcome_cli.py`
and `outcome_store.py`. The following non-negotiables constrained the design.

- Stable explicit project identity. The project is recorded at decision time
  and repeated on every linked event. `route`, `rerun`, `plan` and `report`
  accept an explicit `--project`; closeout accepts a `project` only as an
  optional manifest field, not as a CLI flag. Explicit `--project` is
  canonicalized once (`outcome_cli.canonicalize_project`: expanduser +
  resolve); a blank value is rejected, never silently defaulted to cwd.
  The launch inherits the decision's recorded project unless an explicit
  `--project` is passed, and an explicit project that disagrees with the
  recorded decision fails before write (`outcome_cli.resolve_project`).
  The launch inherits the decision's project across cwd changes; it does
  NOT inherit actual provider/model/effort, which the operator passes
  explicitly and the launch validator preserves or rejects with
  `--override-reason`. A child worktree's path is not authoritative; the
  audit filter matches the recorded coordinator path or nothing, and the
  audit never guesses.
- Decision-derived launch identity. The launch record carries the
  `decision_id`, exact task hash, project and account status (`unverified`
  until proven by a separate attestation). It is added by append-only binding
  to an existing unlinked launch when the task hash, project and launch
  metadata agree exactly; otherwise it is rejected before write.
- Atomic idempotent closeout. `rightsize outcome closeout --manifest FILE`
  records a linked launch's completion, optional review, optional rework,
  optional usage and acceptance in one transaction. The CLI takes the
  manifest file only: there is no `--root` flag and no `--project` flag.
  The evidence root is fixed to the directory holding the manifest. The
  manifest is JSON with `schema_version: 1`, a `dispatch`, an optional
  `decision_id` and `project`, and an `events` array; each event carries
  `event_id`, `kind`, `data` and an optional `evidence_path`. The manifest
  is capped at 64 KiB and 64 events; each referenced evidence file is
  capped at 1 MiB and must be a regular file whose SHA256 matches
  `data.evidence_sha256`. `evidence_path` is required on hash-bearing
  events (`completed`/`failed`/`cancelled`/`review`/`rework`/`accepted`)
  and is resolved relative to the manifest directory; absolute paths and
  `..` traversal are rejected. Every path component is lstat-checked for
  symlinks, so a symlink at any depth (leaf or parent directory) is
  rejected before any byte is read. File hash checks prove the bytes on
  disk matched the recorded SHA256; they do not prove execution
  correctness, reviewer identity or account binding. Validation is
  all-or-nothing: any envelope, kind, evidence, decision, project or
  review-floor check fails before any event is written. An event_id
  already present with identical content is left alone (idempotent
  retry); identical id with differing content fails the batch. Existing
  `outcome record` per-event calls remain valid for incremental additions
  before closeout.
- Required review floor preserved. `high_stakes` and any second-opinion score
  at or above the recorded threshold require that the `accepted` event be
  accompanied by a `review` event whose `provider` differs from the actual
  author model AND whose `family` differs from the actual author family:
  matching EITHER blocks acceptance (`outcome_store.OutcomeStore._review_valid`).
  The reviewer must use a configured profile whose family matches the
  review's `family`, pass the recorded `quality_floor` and capability
  gates via `qualified_candidates`, and clear per-event validation;
  an under-floor reviewer or invented family is rejected before write.
  Same-family `review` events are stored when they pass per-event
  validation; the cross-provider/family requirement is enforced at
  acceptance, not at review-record time. A MiniMax author cannot be
  reviewed by a MiniMax Go variant of the same family, and direct MiniMax
  cannot review OpenCode Go MiniMax.
- MiniMax-only pool blocker. With MiniMax-only workers restricted to a
  particular run, the cross-provider/family reviewer requirement is
  unsatisfiable for any work whose recorded review requirement is `true`
  in that run. The coordinator does not widen the pool and does not
  reclassify the existing task to bypass review. Recorded advisory
  acceptance is blocked until a separate, out-of-pool reviewer is
  dispatched. The coordinator states the blocker explicitly rather than
  recording acceptance. The MiniMax-only restriction is scoped to the
  run the user restricted; it does not bind later dispatches.
- UI and browser evidence. UI verification claims require the actual
  component and browser execution path plus a retained artifact (Playwright
  run, CDP capture, real-browser Storybook/Vitest render with snapshot).
  HTTP-only `curl` against a preview URL proves the server returned the
  HTML but does not prove a browser rendered it, and is not browser proof.
  A mocked helper test, a build-success log, or prose without an artifact
  is not browser proof.
- Unknown stays unknown. The audit reports known/unknown splits for usage,
  rework, and review; missing categories are not converted to zero. Coverage
  denominators are explicit.

## Non-goals

- This ADR does not claim managed account binding, savings, model-quality
  rankings, automatic historical backfill, or universal automation.
- It does not change provider reserves, account bindings, catalog ladder
  rankings, or active worker state.
- It does not move private project telemetry into the public repository.
  Project-specific knowledge still lives in the canonical Brain.

## Acceptance

- One newly authorised real consumer run retains selected/effective/override
  evidence, exact project/task identity and settlement-to-acceptance events;
  the audit reconciles with repository verification and reports unknown
  coverage explicitly.
- A MiniMax-only author whose work is review-required records a same-family
  `review` event successfully but the `accepted` event is refused at parse
  time; acceptance is recorded only after an out-of-pool reviewer lands an
  accepted `review` event.
- UI claims cite a named component, a named browser execution path, and a
  retained artifact; a missing artifact keeps the claim outstanding.
- Implementation owner delivers the closeout manifest contract and focused
  regressions; the public docs reflect the implemented parser only.
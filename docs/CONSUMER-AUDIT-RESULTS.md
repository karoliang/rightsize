# Consumer audit implementation verification

2026-09-21. Parent #20; findings #25; implementation #26, #27, #28.
Decision: [ADR0004](decisions/0004-consumer-acceptance-evidence.md).

Implemented locally on the existing task-fit/MiniMax working tree. Pre-existing
changes were preserved. No provider settings, reserves, credentials or managed
admission schema changed. No commit, push, deployment or historical migration is
claimed by this verification.

Three supervised Orca workers used direct MiniMax-M3 in OpenCode at the user's
explicit request. The planner classified bounded implementation tasks at band1
but left them unplaced; the explicit band2 override preserved their floor.
The native TUI showed MiniMax-M3. Orca's effective-model fields were null, so no
managed account-binding claim follows. Coordinator review corrected implementation
and documentation defects before acceptance, including races, timestamp replay,
terminal conflicts, stale review after repair, false usage coverage and retry
latency. Worker success alone was not accepted as proof.

## Validation

- All28 existing and new offline test scripts passed, including hooks and the
  policy assertion script. Four Docker-dependent pilot acceptance checks were
  skipped by their existing gate; no Docker isolation change is included here.
- Focused final checks:38 classifier tests,75 evidence-store tests,10 CLI
  integration tests,16 reliability tests and10 task-fit tests passed.
- New tests cover repository-path false positives, preservation of meaningful
  prose, irreversible-action separation, concurrent duplicate writes, history
  beyond200 entries, exact override receipts, read-only audit, conflicting
  outcomes, failed-first-attempt latency, review freshness/floor/family, explicit
  hook-first binding, corrupt-state refusal and unknown-versus-zero metrics.
- The installed read-only `outcome audit --project PATH` command returned valid
  JSON without creating history or contacting providers. No project savings or
  comparative model quality is inferred from that smoke test.
- Changed Python modules compile and `git diff --check` passes. Worker terminals
  created for this run were closed after settlement; their exact holds released.

New external dispatches must use [CONSUMER-WORKFLOW.md](CONSUMER-WORKFLOW.md).
The two reviewed consumer instruction files point to that workflow. Existing
workers and incomplete historical records were not altered. Independent review
is enforced for recorded advisory acceptance; external Orca completion and native
managed acceptance retain their separate lifecycle contracts. Evidence hashes
and actor/model claims remain caller attestations, not cryptographic identity.

# Consumer closeout final results

Date: 2026-09-21. Parent #20; follow-up #29. Final results from the
MiniMax peer reviewer for the closeout manifest contract under the
MiniMax-only worker restriction.

## What this review pinned

The reviewer-owned fixture
(`test_consumer_closeout_review.py`) runs end-to-end against the
implemented parser and store. The implementation-owned fixture
(`test_consumer_workflow.py`) exercises the same surfaces from the
shipped path. Both fixtures were run from the shared working tree at the
end of the review; source hashes match the validated full run recorded
in `/tmp/rightsize-29/verification/summary.json`.

## Final focused counts

| Suite | Tests | Skip | Exit | Log |
| --- | --- | --- | --- | --- |
| `test_consumer_workflow.py` (shipped path) | 31 | 0 | 0 | `/tmp/rightsize-29/final-focused.log` |
| `test_consumer_closeout_review.py` (MiniMax peer reviewer) | 29 | 0 | 0 | `/tmp/rightsize-29/final-focused.log` |
| combined | 60 | 0 | 0 | `/tmp/rightsize-29/final-focused.log` |

The combined log shows `Ran 60 tests in 0.234s OK` with exit 0.

## Source hashes

| File | SHA-256 |
| --- | --- |
| `rightsize.py` | `fa5553efb43c1a3fccc242625cc792d0c931cc6cc54b70aebdfd0fd6136f503c` |
| `outcome_cli.py` | `e1f9473826b415a129575aab0076892ede050830f039ff2a9b6254715a549343` |
| `outcome_store.py` | `b0242279f957f8160f0138cc20dd0e3bcee7ec94c5c422a2b0ac6ed99f265e48` |
| `test_consumer_workflow.py` | `394aa19dda19aa20fb7adfb2276e3d9984f086d29d8cead77e00b6d993825dfc` |
| `test_consumer_closeout_review.py` | `f7afa90d21d855b10be935c9bbc4cfb4171b4259009e811af41168e05a1a3191` |

The first four match `/tmp/rightsize-29/verification/summary.json` byte
for byte; the reviewer-owned test file changed during this review and
its hash is the post-fix value.

## Full root-suite exit codes

Every root-level `test_*.py` plus `hooks/test_hook.py` was run with
captured exit code and full output under
`/tmp/rightsize-29/final-alignment-checks/`. All exit 0.

| File | Exit | Notes |
| --- | --- | --- |
| `test_accounts.py` | 0 | 14 tests |
| `test_claude_execution.py` | 0 | 12 tests |
| `test_consumer_closeout_review.py` | 0 | 29 tests |
| `test_consumer_outcomes.py` | 0 | 10 tests |
| `test_consumer_workflow.py` | 0 | 31 tests |
| `test_context.py` | 0 | 15 tests |
| `test_credentials.py` | 0 | 6 tests |
| `test_judgment.py` | 0 | 7 tests |
| `test_managed_ledger.py` | 0 | 16 tests |
| `test_managed_router.py` | 0 | 13 tests |
| `test_minimax.py` | 0 | 9 tests |
| `test_native_claude.py` | 0 | 8 tests |
| `test_native_codex.py` | 0 | 17 tests |
| `test_native_opencode.py` | 0 | 11 tests |
| `test_native_rpc.py` | 0 | 7 tests |
| `test_native_transport.py` | 0 | 5 tests |
| `test_opencode_execution.py` | 0 | 13 tests |
| `test_outcome_store.py` | 0 | 75 tests |
| `test_pilot_acceptance.py` | 0 | 9 tests, **4 Docker skipped** |
| `test_pilot_execution.py` | 0 | 4 tests |
| `test_pilot_native.py` | 0 | 1 test |
| `test_pilot_run.py` | 0 | 11 tests |
| `test_pilot_tasks.py` | 0 | 4 tests |
| `test_reliability.py` | 0 | 16 tests |
| `test_replay.py` | 0 | 9 tests |
| `test_rightsize.py` | 0 | own runner: `all checks passed` |
| `test_state_migration.py` | 0 | 15 tests |
| `test_task_fit.py` | 0 | 10 tests |
| `test_task_judgment.py` | 0 | 38 tests |
| `hooks/test_hook.py` | 0 | own runner: `hook checks passed` |

The Docker-skipped tests in `test_pilot_acceptance.py` are
`ContainerAcceptanceTests` cases gated behind `explicit Docker
integration gate`. They are skips in this environment, not execution
proof; the 5 non-Docker tests in the same file ran and passed.

`test_rightsize.py` and `hooks/test_hook.py` are assertion scripts with
their own runners; their successful exit is the contract. They are not
unittest suites and have no per-test count to report.

## Reviewer-fixture corrections applied

The original `ReviewQualification` tests were false positives: they put
the manifest at `tmp/manifest.json` while the evidence blobs lived at
`tmp/evidence/<id>.bin`, so `evidence_path: "c.bin"` resolved to
`tmp/c.bin` (missing) and the CLI rejected the batch on file lookup
rather than on the intended profile/family/floor rule. Each test now:

1. Co-locates the manifest under the evidence directory
   (`tmp/evidence/manifest.json`) so `evidence_path` resolves to the
   bytes whose SHA is in `data.evidence_sha256`.
2. Captures stderr from `r.main` and asserts the dedicated rejection
   substring (`configured model profile` for profile/family mismatch,
   `quality floor` for under-floor reviewers).
3. Records no event row when the rejection fires.

The fix also adds:

- `test_actual_effort_never_defaults_from_selected_effort`: when a
  decision selected an effort but the operator passes no `--effort`,
  the launch validator rejects the diff and requires
  `--override-reason`; with `override_reason` the launch is accepted
  and `actual.effort` stays `None` (never silently rewritten).
- `test_same_family_review_stored_when_review_not_required`: a
  same-family `review` event is stored when `review_required=False`,
  because storage is permissive and the gate is at acceptance.
- `test_same_family_review_blocks_required_acceptance_only`:
  the inverse pin — a same-family review stored under
  `review_required=True` is refused at the accepted-gate.
- `test_symlink_in_parent_dir_rejected_with_real_bytes`: a parent
  symlink (`root/link/file`, `link` -> outside root) is rejected by the
  per-component `lstat` walk before any byte is read. The real target
  file exists and the SHA matches, so the rejection is unambiguously
  the symlink guard.

The initial 26 tests include the corrected fixtures (manifest
co-location, stderr capture, dedicated rejection substring). Three
additional cases bring the total to 29: the two same-family cases
(`test_same_family_review_stored_when_review_not_required`,
`test_same_family_review_blocks_required_acceptance_only`) and the
parent-directory symlink case
(`test_symlink_in_parent_dir_rejected_with_real_bytes`). The
`test_actual_effort_never_defaults_from_selected_effort` case extends
the prior no-effort contract to cover both the rejection path and
the override-reason path, asserting that the actual effort stays
`None` and is never silently rewritten from the selection.

## Cross-provider/family acceptance gate

Acceptance requires `review.provider != actual.provider` AND
`review.family != actual.family`; matching EITHER blocks acceptance
(`outcome_store.OutcomeStore._review_valid`). Same-family `review`
events pass per-event validation (configured profile, family match,
task floor, capability gate) and are stored; the gate fires at the
`accepted` event, not at review-record time. A MiniMax-only dispatch
pool cannot produce a qualified reviewer for MiniMax-authored work,
so the `accepted` event fails the transactional acceptance check (not
JSON/argument parsing) even when a same-family `review` event is
present, and the coordinator states the blocker rather than widening
the pool or reclassifying the task. A same-family `review` event
records only if per-event validation (configured profile, family
match, task floor, capability gate) passes; it never satisfies a
required independent acceptance check.

## Source-implemented versus peer-tested

- **Source-implemented:** the closeout manifest contract, the
  per-event `_event` helper shared by `event()` and `closeout()`,
  evidence-byte validation including per-component `lstat` symlink
  rejection, and the launch validator that refuses silent effort
  defaulting without `--override-reason`.
- **Peer-tested:** the MiniMax peer-reviewer-owned fixture exercises the
  contract through real CLI calls (`r.main(["outcome", "closeout",
  ...]`), captures stderr, and pins the dedicated rejection substring.
  The shipper fixture (`test_consumer_workflow.py`) covers the happy
  path through the same parser.

These are distinct assertions. The reviewer fixture would fail if the
parser silently accepted an invalid manifest; the shipper fixture
would fail if the parser silently rejected a valid one. Both ran and
passed against the same source hashes above.

## What this review does not establish

- **Independent cross-provider acceptance.** MiniMax peer review under
  the user's restriction is not a different-provider, different-family
  reviewer. Acceptance fails the transactional acceptance check (not
  JSON/argument parsing) for any MiniMax-only restricted run whose
  work is `review_required=True`, until an out-of-pool reviewer is
  dispatched. The same-family `review` event the peer reviewer may
  record passes per-event validation (configured profile, family
  match, task floor, capability gate) and is stored, but never
  satisfies a required independent acceptance check.
- **Account binding.** A model name and `native_session` recorded by
  the same process are caller attestation, not authenticated identity.
- **Execution correctness.** File hash checks prove the bytes on disk
  matched `data.evidence_sha256`; the store never re-runs the test or
  re-reads the file beyond the single SHA256 pass.
- **Comparative model quality or savings.** Accepted counts and token
  totals are inputs to evaluation, not conclusions.
- **Real-network execution.** No provider call, model call, or
  network probe ran in this review. The 60 focused tests are
  synthetic-store, temporary-filesystem, and CLI-in-process.

# Paired coding pilot preparation

Tracking: [#19](https://github.com/karoliang/rightsize/issues/19).
The promotion criteria remain in [EVALUATION.md](planning/EVALUATION.md).

```sh
python3 pilot_tasks.py
python3 pilot_tasks.py --prepare /private/new-pilot-directory
```

The first command verifies the reviewed, SHA-256-pinned corpus: 20 coding tasks,
five in each tier, with 74 deterministic input/output checks. Every reference
solution passes; every starting solution fails at least one check. Argument
mutation fails acceptance. Boolean and integer results are distinguished.
Only the checked-in, digest-verified fixture sources execute during this check.
There is no command to execute model-generated Python on the host.

Preparation creates separate baseline/candidate copies of `solution.py` and
`TASK.md`, plus a private manifest outside those task directories. Both arms
start with byte-identical files. Categories rotate; AB/BA order alternates.
The manifest records task/source/acceptance hashes, caller judgments, the fixed
`a168a85` baseline identity and its source hash, and the two-attempt maximum.
Neither reference solutions nor acceptance cases are copied into task snapshots.
Existing directories and symlinks are refused rather than overwritten. No Git
commit, provider discovery, reservation or model launch occurs.

## Task coverage

| Tier | Five tasks |
| --- | --- |
| Mechanical | Public-field extraction, stable deduplication, bucket-field rename, status mapping, stable receipt sorting |
| Implementation | Tightest quota bucket, conservative point rounding, commitment aggregation, bounded optional context, outcome denominators |
| Diagnosis | Null reset, zero versus unknown quota, cumulative usage deduplication, denial account scope, migrated hold expiration |
| High stakes | Denial/reserve/slot precedence, credential binding, terminal cancellation proof, request idempotency, permission intersection |

These are isolated coding exercises in router semantics. The high-stakes tier
tests security-sensitive logic without editing real credentials, permissions or
production data. Results will describe this small task mix, not establish broad
coding superiority or statistically reliable savings. Cases and references are
public in the source repository, so the future pilot worktrees must contain only
the generated task snapshot and must not give the model this repository as
additional context. The harness must enforce source permissions.

## Remaining execution gate

The generated manifest deliberately has `live_ready: false` and no usage ceilings.
It is a preparation artifact, not permission for an unbounded loop. Before any
pilot launch, the runner still needs to:

1. Freeze the candidate revision, corpus and per-provider usage ceilings before
   seeing results; record identical native permission limits for both arms.
2. Execute the hash-verified baseline policy and candidate policy on equivalent
   captured observations, retaining each verdict. Apply the same current account,
   denial, atomic-admission and native-execution checks to both arms. A baseline
   selection that fails these checks is a recorded refusal, not an override.
3. Initialize isolated Git snapshots for managed execution and restrict the model
   to its task sources. Independently check generated code in a bounded OS sandbox
   with no credentials or network. Never pass candidate code to `check_source`.
4. Join each result to decision, account, native receipt, usage and independent
   review evidence. Preserve refusals, missing results, timeouts and retries in
   the report; do not replenish the corpus with easier tasks after results arrive.
5. Stop on safety violations, uncertain active work or exhausted usage ceilings.
   Inspect every high-stakes failure. The complete paired report and adapter/
   rollback gates are required before promotion.

The common execution checks intentionally control away unsafe historical launch
behavior. The comparison measures routing choices and accepted results under the
same execution controls; it does not pretend to reproduce an obsolete launcher.
Preparation and reference checks contribute zero live attempts and cannot close
the pilot gate.

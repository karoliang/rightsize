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
There is no command to execute model-generated Python on the host. Generated
solutions use the separate container acceptance runner below.

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
   to its task sources. Join the container acceptance runner's source hash and
   result to the actual managed worktree. Never pass generated code to
   `pilot_tasks.check_source`.
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

## Container acceptance

```sh
docker pull python@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
python3 pilot_acceptance.py --task mechanical-public-fields \
  --candidate /private/trial/solution.py --record /private/trial/new-review.json
RIGHTSIZE_TEST_DOCKER=1 python3 test_pilot_acceptance.py
```

Docker is a pilot-validation dependency, not a router runtime dependency. The
runner requires an available local Unix Docker endpoint and the pinned image.
It never pulls implicitly and never falls back to host execution. The extra CI
job fetches that image, then runs all candidate checks without network access;
normal router tests still need only Python's standard library.

Candidate code is limited to 64 KiB of Python functions/docstrings, with no
imports, decorators, dunder introspection or file I/O. The bootstrap exposes
builtins needed by these exercises; the AST/builtin restrictions are additional
checks, not the OS isolation boundary. Python 3.13 runs in the pinned container.
Only source and argument values enter over stdin. Expected answers stay in the
host checker, which compares returned values and post-call arguments exactly.
Changing acceptance cases inside the task worktree cannot change the verdict.

Each container uses UID/GID65534, no host mounts, no networking, a read-only root,
all capabilities dropped, no-new-privileges, 256 MiB memory with no additional
swap, half a CPU, 16-process limit, two CPU seconds and a ten-second execution
deadline. Output is capped at64 KiB. The runner inspects effective Docker
configuration before starting code. Docker gets an empty client configuration,
so host proxy settings and credential helpers do not populate the container.
These controls follow Docker's [run contract](https://docs.docker.com/engine/containers/run/)
and [resource constraints](https://docs.docker.com/engine/containers/resource_constraints/).

Before creation, a private fsynced review record stores the random container name,
endpoint, source/corpus/image identity and pending state. Creation records the
exact container ID before execution. Errors and timeouts trigger forced removal;
an independent container listing must confirm removal. Unknown cleanup forces
`accepted:false` and `status:unresolved`. No create/start retry occurs after an
uncertain response. A host-process crash leaves the pending record for inspection
and cleanup; the future driver must stop on that record, not launch another
attempt or treat its missing result as accepted.

Exit0 means accepted with cleanup confirmed, exit1 means recorded non-acceptance,
and exit2 means input/infrastructure setup failed before a review could finish.
Review files are never overwritten by a fresh invocation. Raw candidate output,
Docker error text and solution bodies are not copied into review records.

Tests execute all20 references and all20 starting defects in real containers,
plus isolation probes for host files, environment, network and writes, and CPU,
memory, output and wall-time failures. Container absence is checked after each.
These are acceptance-harness proofs, still zero live model pilot attempts.

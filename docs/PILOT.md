# Paired coding pilot preparation

Tracking: [#19](https://github.com/karoliang/rightsize/issues/19).
The promotion criteria remain in [EVALUATION.md](planning/EVALUATION.md).
The first bounded live trial is documented in [PILOT-RESULTS.md](PILOT-RESULTS.md);
it stopped at its declared usage threshold and did not complete the promotion gate.

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
It is a preparation artifact, not permission for an unbounded loop. The driver
below implements the control flow for these gates; a live trial must still prove
the resulting evidence before promotion:

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

## Durable paired driver

`pilot_run.py` now connects frozen observations, the original baseline policy,
current managed admission, native execution and independent container review.
It is an opt-in experiment runner, not a change to ordinary routing.

Create an explicit limits file outside the checkout. For example, the following
are per-provider ceilings, not a suggested entitlement or a dollar estimate:

```json
{
  "opencode": {"attempts": 60, "dispatch_points": 50, "input_tokens": 2000000, "output_tokens": 100000},
  "codex": {"attempts": 40, "dispatch_points": 100, "input_tokens": 2000000, "output_tokens": 100000},
  "claude": {"attempts": 20, "dispatch_points": 100, "input_tokens": 2000000, "output_tokens": 100000}
}
```

```sh
python3 pilot_run.py init /private/new-trial --limits /private/limits.json --native-opencode
python3 pilot_run.py step /private/new-trial
python3 pilot_run.py report /private/new-trial
```

`init` requires a clean committed candidate, pins its revision, configuration,
manifest and limits, and prepares both arms. Unlike the standalone preparation
command, it marks that manifest ready for explicit driver steps with its frozen
usage ceilings. `--native-opencode` explicitly selects the existing native Go
home for this trial, bypassing the separate vault choice without modifying the
production configuration. No credentials are copied into the bundle.

Each `step` can launch at most one new native attempt. It holds a run lock and
persists admission/dispatch intent first. Repeating a step after a crash reuses
its admission request or observes the exact recorded native attempt. A step that
was already dispatching never invokes launch again. An unresolved attempt blocks
advancement; the native owner lock also applies to scoped OpenCode recovery.
The fixed baseline is executed from its verified Git object against the same
captured observations as the candidate, with no judge call or baseline-side
state writes. Actual admission uses fresh observations and current account,
denial, slot, point and capability-floor checks for either arm. It does not
replace a refused historical selection with the candidate's preferred model.

The first attempts follow the manifest's alternating order. A reviewed rejection
permits one retry after both first attempts for that pair; other outcomes do not
silently retry. Each retry starts from the same initial files and task contract,
without test answers or generated feedback. Its two arms share a newly captured
observation. High-stakes fixtures retain that tier, but `destructive=0` reflects
that these tasks only edit isolated test files. This is not an approval bypass
for production changes.

The conservative dispatch-point and attempt ceilings are enforced before an
admission is attempted and never refunded to create more pilot capacity. Native
input/output counters are separate per-provider **stop thresholds**: when an
observed running counter reaches its remaining threshold, the driver requests
native cancellation and still requires terminal proof. Counters arrive in native
updates and may overshoot a threshold before cancellation is observed; these are
not hard provider billing limits. Missing terminal usage stops the run. Included
quota/reserve checks remain authoritative, and there is no paid fallback or
automatic budget reset. Other native token categories remain in each attempt's
metrics; do not sum overlapping counters or compare unlike provider categories
as a common price.

Pilot execution adds these source controls:

- Codex uses a named profile with root reads denied, only the worktree writable,
  `.git`/`TASK.md` read-only, no temporary-directory or network grants, and minimal
  system/tool reads. On the inspected Homebrew installation, the tool prefix
  must also be readable for Codex's own helper to start. The profile is verified
  in the prepared response. The required experimental protocol capability is
  enabled explicitly. Inherited MCP/plugin entries and web/app tools are disabled
  for the pilot thread, and shell tools do not inherit the host environment.
- OpenCode retains native read/glob/grep rules and external-directory denial,
  but grants edits only to `solution.py`. Recovery reconstructs the same rules.
- Claude remains a recorded refusal for pilot execution until equivalent source
  restriction is verified. The policy may still select it; this limitation is
  visible rather than a hidden change to the routing ladder.

The local Codex no-inference check prepared this profile successfully. A command
under the same native profile could read the task and edit `solution.py`, but
could neither read a sibling file outside the worktree nor modify `TASK.md`.
OpenCode's restrictions remain native tool permissions, not an OS sandbox.
Generated Python acceptance always uses the separately verified Docker boundary.

Git worktrees are initialized from the immutable arm snapshots. Before launch,
the driver records hashes of the initial source, task and worktree Git pointer.
Review rejects extra files or changed task/Git metadata, and binds the container
result to the exact produced source hash. Only a matching independent review is
recorded as accepted in the managed ledger. If a crash occurs after that ledger
write, resumption requires the same review evidence rather than accepting a
standalone accepted label.

The report preserves refusals and unresolved attempts in attempted-task
coverage, separates unstarted tasks and reports per-provider budget consumption.
It summarizes recorded state; it is not independent re-verification or automatic
promotion. Native first-output/settlement times and review times are retained per
attempt, but first-useful-output classification, final latency analysis and manual
inspection of high-stakes failures remain evaluation work. No live paired trial
results are asserted by the driver's simulated tests.

## Equal per-arm ceilings

New trials may pass `--per-arm` to `init`. Each provider limit then applies
independently to baseline and candidate, with identical limits for both. This
means twice the aggregate attempt/dispatch/token allowance compared with shared
mode; it does not increase native quota or relax reserve/account checks. Report
includes aggregate usage and separate `arm_usage`. The scope is pinned in both
manifest and bundle. Older bundles lacking scope retain their original shared
semantics; the halted first trial is never converted or replenished.

All pre-admission checks and native cancellation thresholds use the selected
arm's spending. Unresolved work still blocks the entire run. Reaching either
arm's ceiling stops the entire experiment: unused allowance is never transferred
from its partner. This removes cross-arm budget consumption, not all possible
censoring. Any budget-truncated pair must remain visible and cannot establish
quality superiority.

The next trial is predeclared before new output: the same20 task contracts and
acceptance cases, with the newly documented cache-free environment instructions
applied equally to both arms; alternating order, one retry after rejection,
120-second native attempt timeout, unchanged native policy/reserves and explicit
existing native Go login. Per provider, **each arm** has40 admission attempts,
100 conservative dispatch points,2,000,000 input and100,000 output token stop
thresholds. Aggregate ceilings are thus80 attempts/200 points/4m input/200k output
per provider, subject to the stricter real included-quota admission checks.
No paid fallback, limit increase, selective replacement or default promotion.

The prior trial suggests these ceilings can accommodate the planned initial
attempts, but this is an estimate, not guaranteed completion. Preserve both runs
in reported evidence. This evaluates quality and lifecycle coverage under the
observed choices; a same-choice run does not establish routing savings. Any
future claim about differing-choice routing needs separately declared evidence.

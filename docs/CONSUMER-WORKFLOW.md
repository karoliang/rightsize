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
contains the exact UTF-8 task-text hash, caller cwd as project, a hash of routing
source bytes (including local uncommitted changes), the effective config hash,
task tier/floor, review requirement and selected provider/model/effort/family.
A new route receives a new decision ID. These are policy evidence, not credentials
or authenticated caller identity. Raw task text is not stored in this ledger.

After a confirmed external launch, from the same project cwd:

```sh
rightsize report minimax --started --model MiniMax-M3 \
  --task "EXACT full text that was routed" --dispatch ctx_example \
  --worktree /absolute/worker/path --decision-id DECISION_ID \
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

Write a small JSON payload, then use:

```sh
rightsize outcome record --dispatch ctx_example --event-id UNIQUE_STABLE_ID \
  --kind completed --data evidence.json
```

Kinds and payloads:

| Kind | Data fields |
| --- | --- |
| `completed`, `failed`, `cancelled` | `evidence_sha256` |
| `review` | `actor`, `provider`, `model`, `family`, `verdict` (`accepted` or `rejected`), `evidence_sha256` |
| `rework` | `actor`, `seconds`, `evidence_sha256` |
| `usage` | `source`, optional `input_tokens`, `output_tokens`, `cache_read_tokens` |
| `accepted` | `actor`, `evidence_sha256` |

`evidence_sha256` must be the lowercase SHA256 of retained review/test/artifact
evidence. The command records a caller attestation, not authenticated review or
automatic verification of the referenced file. JSON rejects unknown fields and
duplicate keys; do not include raw transcripts, tasks or secrets. Payloads are
limited to64 KiB. Each event ID is globally unique and stable across retries.

`completed` records lifecycle success, never acceptance. Before reporting
completion, a worker must read queued coordinator messages and resolve or
explicitly report outstanding findings. The coordinator independently checks the
diff and acceptance evidence.

High-stakes work always requires review; other tasks require it when the
second-opinion score reaches0.6. The reviewer must use a configured model at the
recorded task floor with required capabilities, from a different provider AND
family than the actual author model. A missing reviewer remains outstanding.
Go MiniMax and direct MiniMax cannot review one another independently. Caller
claims of a different family are checked against the configured profile.

Acceptance requires linked completion and explicit evidence. A rejected review
blocks acceptance. Required review must be accepted after the latest recorded
repairs. Record repairs and usage before acceptance; new events cannot silently
change an already accepted record. This gates Rightsize evidence, not external
Orca task settlement or deployment. Managed review retains its existing contract.

For example, a usage payload contains separate cumulative categories:

```json
{"source":"native-session-counter","input_tokens":12000,"output_tokens":900,"cache_read_tokens":50000}
```

Report the latest cumulative snapshot, not incremental deltas. The audit uses the
latest snapshot per dispatch, never sums repeated snapshots. Missing categories
remain unknown; an explicit zero is known zero. Cache reads are repeated context
processing, not unique context size or attributable subscription charges.

For repairs, record measured coordinator effort seconds and evidence. Do not use
elapsed waiting time between worker completion and the next launch as repair time.
The absence of a repair record is unknown, not proof of zero rework.

## Inspect performance and coverage

```sh
rightsize outcome audit --project /absolute/coordinator/project
```

Output is always JSON; no `--json` flag is needed. It reads the local
`outcomes.sqlite3` without creating it, probing providers or changing quota.
It reports decisions, launches, completion, acceptance, review, usage and rework
with explicit known/unknown denominators. Task latency starts at the earliest
recorded launch for the same project/task hash and includes recorded retries up
to acceptance. An incomplete history is not a complete lifetime measurement.

Use independently accepted results, repair burden and comparable task families
for evaluation. Do not infer savings or model superiority from worker exit
status, token totals or unmatched samples. Runtime records are private; project
knowledge still belongs in the canonical Brain.

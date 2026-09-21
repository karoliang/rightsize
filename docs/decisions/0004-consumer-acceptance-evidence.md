# Consumer decisions and acceptance evidence

Date: 2026-09-21. Parent #20; implementation #26, #27, #28; audit #25.

The user approved the consumer-audit recommendations and explicitly requested
supervised MiniMax workers through Orca. MiniMax-M3 is an explicit implementation
override, not a new capability ranking or automatic policy promotion. The
coordinator owns the following contract and independently reviews the work.

## Decisions

- Strip path/domain metadata from heuristic classification without suppressing
  meaningful risk words in task prose. Classify irreversible actions separately
  from financial/security correctness. Caller task-bound judgments remain the
  preferred way to communicate precise intent; the fallback stays labelled.
- Require an independent review before accepting high-stakes advisory work,
  regardless of the caller's optional second-opinion score. A missing reviewer
  is outstanding work, not permission to lower the task floor. Other tasks can
  request review through the existing score. Model families come from policy.
- Retain durable local advisory evidence in `outcomes.sqlite3` next to runtime
  state. Keep the compatibility JSON's rolling view and the managed admission
  database unchanged. This is operational telemetry, not a parallel knowledge
  base or a credential store. No automatic backfill may invent missing joins.
- Join an exact decision ID and task hash to a dispatch. Preserve project cwd,
  source and configuration hashes, recommendation, actual model/effort and any
  override reason. Evidence supplied by the coordinator does not authenticate
  account binding or model identity.
- If a hook already recorded an unlinked launch, permit explicit one-time
  decision binding only when its exact project, task hash and launch metadata
  agree. Append binding evidence; preserve the original launch unchanged.
- Completion, review and acceptance are distinct events. Independent acceptance
  requires explicit evidence; recommended review additionally needs a different
  provider and family. Store evidence digests, not source/task transcripts.
- Report coordinator repair seconds and cumulative native token categories with
  known/unknown coverage. Include observed retries when measuring task latency
  to acceptance. Never convert cache reads into unique context, dollars or
  claimed subscription savings.

## Scope and alternatives

An append-only SQLite evidence store permits transactional idempotency and
durable history without enlarging the quota-state JSON indefinitely. It does
not replace managed admission, solve the upstream Orca account handoff (#21),
or enforce acceptance in external orchestrators. External coordinators must
use the documented receipt/event commands. Native managed lifecycle review
continues to use its existing evidence contract.

Do not infer correctness from worker exit, provider branding or a sampled
success percentage. No provider reserve, profile ranking, account or active
worker configuration is changed by this work. Implementation is complete only
after focused regressions and integration gates pass; live deployment or
consumer migration is a separate claim.

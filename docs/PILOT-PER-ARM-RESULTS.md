# Complete paired pilot: equal arm budgets

All20 tasks passed independently on both baseline and candidate:40 native
attempts, no retries, no refusals, no unresolved outcomes or remaining active
managed attempts. The candidate showed no accepted-result regression on this
small corpus. **All20 policy selections were identical, so this does not
establish better routing, cost savings or broad coding superiority.**

Candidate `99a894e2003ec4ec94a0ee8e7a5a635cf47c8590`, historical policy
`a168a85055a3f1fa527da593fced87cf37c10892`. CI35563338985 passed before launch.
[Results and source/review audit](../fixtures/pilot/results/2026-09-21-per-arm.json)
and [redacted native trace counts](../fixtures/pilot/results/2026-09-21-per-arm-traces.json)
are committed. Account/session/worktree identities remain private.

The same20 contracts and cases were declared before output, with the cache-free
prompt on both arms and alternating order. Each arm had its own provider ceilings:
40 attempts,100 conservative dispatch points,2m input and100k output tokens.
Native included-quota, account, denial, reserve and lifecycle checks remained in
force. No limits were replenished or tasks replaced. The [earlier incomplete
shared-budget trial](PILOT-RESULTS.md) remains part of the evidence.

| Measure | Baseline | Candidate |
| --- | ---: | ---: |
| Accepted / attempted tasks |20 /20|20 /20|
| Native attempts |20|20|
| Native input tokens |1,203,279|1,224,180|
| Native output tokens |13,335|13,910|
| Estimated dispatch points |48|48|
| Median admission to independent acceptance, seconds |27.48|28.42|
| Nearest-rank p95, seconds |44.10|35.45|

Only Codex was selected. Aggregate native input2,427,459/output27,245 across147
model responses. Per-response input/output/cached-input/total sums matched every
ledger record exactly. No token or subscription-point category was converted into
dollars; dispatch points are estimates, not attributable billed consumption.
First-useful-output timing remains unavailable because first native output was
not classified independently. There were no retries to exclude from task timing.

All87 command executions were inspected: zero Git checks and zero Apple tool-cache
warnings. One candidate bucket-rename self-check raised an assertion; the model
corrected its implementation within that same native turn and passed independent
acceptance. It was not an optional-context failure, a setup failure or a separate
retry. The original22 failures remain in the older trial. Cross-trial task coverage
and native context differ, so no percentage savings claim follows from averages.

The final read-only audit recomputed all frozen policy verdicts, checked unique
attempt/request and native session/turn joins, matched account/fingerprint/model/
effort receipts, metrics and terminal ledger states, and verified every source,
task, corpus and review digest. Accepted worktrees contained no extra files or
changed TASK.md/Git pointers. All five high-stakes pairs were manually inspected:
denial precedence, credential binding, terminal cancellation proof, idempotent
start and permission intersection. All preserve the stated fixture contracts.

This completes the bounded paired execution and overhead follow-up in#22.
It does not promote the router or close the full architecture epic. Claude live
execution, scoped-vault delivery, the upstream Orca prepared-account boundary,
and rollout/rollback gates remain separately tracked in#14/#17/#19/#21.
No further benchmark is planned as part of this handoff.

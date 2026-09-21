# ADR0003: Preserve task quality before allocating subscription quota

Date: 2026-09-21. Status: user-authorized implementation under #24, parent #20.

The user approved improving task/model/effort selection after a read-only audit
found broad bands, provider-level effort and below-band fallback. Implement the
first bounded slice using existing task classes and quota/launcher seams.

Task and retry floors are hard limits. Candidate models need an explicit profile
covering the task's required capabilities and quality band before quota ordering.
Effort defaults vary by task and model and increases traverse only that model's
supported levels. Review candidates must meet the task floor and be a different
provider and model family. Same-model subscription aliases are not independent.

Agent implementation choice: initial capability assignments preserve existing
ladder placements and are labelled provisional. Do not claim a new ranking from
model size or marketing. The completed paired pilot selected only Codex and
identical policies; it cannot establish cross-model fit. No new benchmark is
needed to prove deterministic floor and effort invariants.

Keep caller judgment v1 unchanged for compatibility. Add policy fields to replay
and future pilot snapshot capture so frozen evaluation does not silently omit
them. Historical snapshots without profiles retain ladder-based fit semantics;
current policy still enforces the original task/retry floor. Historical baseline
source and completed results remain immutable.

Scope excludes new providers, model downloads, native managed adapters, learned
promotion, detailed context/modality requirements and an exhaustive benchmark.
See ../TASK-FIT.md for policy, evidence and remaining limitations.

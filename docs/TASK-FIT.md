# Task fit before quota

Rightsize first filters models by task requirements, then chooses among eligible
providers using the existing quota rules. A roomy plan cannot make an unsuitable
model eligible. This policy applies to advisory routes, planner waves and managed
admission. Caller judgment v1 remains unchanged.

## Current task classes

| Task class | Examples | Minimum starting band | Required profile capabilities |
| --- | --- | --- | --- |
| mechanical | Local rename, prescribed formatting or documentation edit | 1 | code |
| implementation | Bounded feature or UI change with a complete specification | 1, raised to 2 for wide scope | code |
| design | Architecture, unresolved product or interface design decisions | 3 | code, design |
| diagnosis | Unknown root cause, cross-system debugging | 3 | code, diagnosis |
| high_stakes | Security, auth, money, schema/data changes or published output | 3 | code, high_stakes |

These are the existing classifier classes, not newly benchmarked specialties.
A simple implementation of an already-decided UI is different from deciding its
design. Parameter count, brand name and subscription price do not establish fit.
The active caller can provide the task-bound judgment envelope; the existing
keyword fallback remains explicitly labelled and may misclassify a task.

`task_profiles` sets minimum bands and required capabilities. `model_profiles`
uses exact `provider:model` keys and specifies:

- `max_band`: highest task band provisionally approved for that model.
- `capabilities`: declared task capabilities; reviews additionally need `review`.
- `family`: same-model aliases across providers, including MiniMax Go/Ultra.
- `efforts`: ordered levels supported by the selected runtime/model; empty means
  no exposed effort control.
- `effort_by_task`: task-specific defaults; an explicitly pinned ladder effort
  overrides these only when supported.
- `evidence`: current basis for the assignment, included in decision notes.

The shipped capability assignments deliberately preserve the previous ladders:
band1/2 models support bounded coding/review; band3 models are provisionally
approved for design, diagnosis and high-stakes work. This is an operator policy,
**not proof that every model meets the same quality bar**. Codex supported effort
lists came from the local native catalog on 2026-09-21. Claude uses the conservative
low/medium/high subset already configured; OpenCode models have no effort flag.
Revalidate native support when updating model IDs. Unknown models are excluded
until profiled; do not assign capabilities solely to make a blocked task run.

## Floors, effort and reviews

The computed task band, task-profile minimum and retry floor combine using the
strictest value. Single-task routing can try a higher band, never a lower one.
No qualified capacity returns `pick: null`, with the preserved `quality_floor`
and reasons. Batch planning keeps these tasks unplaced for a later wave. Existing
quota reserves, measured burn, denial/account gates and launcher refusals remain.

Effort uses the selected model's ordered levels. A retry adds supported steps;
wide scope or an irreversible operation adds one more step. Stop at the model's
supported maximum. For example, a model exposing only low/high moves directly
from low to high, never to an invented medium or xhigh. Shipped ladders no longer
pin effort, so a powerful model selected for a mechanical task can use low effort.
Explicit user ladder pins remain supported. Effort labels are not equivalent
quantities of reasoning across providers.

Reviews use the task quality floor, not an unconditional cheap reviewer. They
must come from a different provider and model family. Go M3 and Ultra M3 therefore
do not count as an independent pair. A missing reviewer is reported as outstanding;
high-stakes tasks require review regardless of the optional second-opinion score.
The advisory outcome store gates recorded acceptance on completed execution and
a qualifying independent review after recorded repairs; external worker settlement
and the managed review contract remain separate. See [consumer workflow](CONSUMER-WORKFLOW.md).
Planner costs include the review's actual band.

## Verification and limits

Offline regressions cover task/retry floors under quota pressure, missing model
profiles and capabilities, unsupported efforts, model-specific effort ceilings,
independent review, planner allocation and frozen-policy replay. No new model
benchmark was run. Prior pilot results remain unchanged and do not establish
cross-model superiority.

Context length, vision requirements, latency targets and detailed task-family
success rates are not yet part of caller judgment v1. Do not infer these from
capability tags. A later evidence-driven update can extend that contract and run
small comparisons where model/task fit is uncertain. Outcome collection and
promotion are not automatic in this change.

Tracking: [#24](https://github.com/karoliang/rightsize/issues/24).
Decision: [ADR0003](decisions/0003-task-fit-routing.md).

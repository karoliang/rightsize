# Forbidden models

An operator sometimes needs a model to be unselectable. Not deprioritised, not
discouraged in the brief: unselectable, in every band, in every role, on every
entry point. This is how that works and what it costs.

## The defect this replaces

A spec file said, verbatim:

```
Constraints: Codex only on this host. MiniMax excluded. Astra must not be
selected for routine coding, building, retries or fallback.
```

and `rightsize route --spec <that file>` picked `codex:gpt-6-astra`.

Nothing malfunctioned. The spec is input to the **judgment**, which produces a
tier and four scores, and the selector never reads a word of it. A sentence in a
brief is a wish about selection, not a constraint on it, and no amount of
emphasis in prose changes which list `pick()` walks.

The second half was narrower and worse. Before Sol joined band 3,
`rerun --previous codex:gpt-6-astra` correctly kept Astra out of the
implementer ladder, re-decided to `claude:claude-opus-5` without lowering the
quality floor, and then selected `codex:gpt-6-astra` as the reviewer. An
exclusion that covers one role is not an exclusion; a forbidden model simply
arrives through the other door.

Both are the same fix: the exclusion has to live where candidates are filtered.

## Configuration

```json
"model_policy": {
  "forbidden": {
    "codex:gpt-6-astra": "owner policy 2026-09-27 (#32): not for routine coding, building, retries or fallback in any role"
  }
}
```

Keys are `provider:model`, exactly as they appear in `bands` and
`review_ladder`. The value is the reason, and it is printed wherever the
exclusion is applied, so an operator reading a decision sees the policy rather
than an unexplained absence. A list of keys is accepted too, and gets a generic
reason.

The shipped config is the owner policy. A repository `.rightsize.json` may add
forbidden models, using either a mapping or a list, but its policy is unioned
with the owner's forbidden entries. An empty mapping, empty list, or null policy
section cannot clear them. If both files name the same model, the owner's
reason wins. Other repository settings keep their usual merge behavior.

There is no separate filter. `pick()` already took an `exclude` set for "this
model already had a go at this task"; policy extends the same argument to a
mapping that carries a reason per entry. Every selection in `decide()` passes
through it: the implementer ladder, the relaxed-pacing retry, each fallback
band, and the review ladder.

## What it guarantees

- **Every entry point.** `route`, `plan` and `rerun` all select through
  `decide()`, and `decide()` builds the exclusion set itself rather than
  receiving it. `managed` admission goes through the same function.
- **Every role.** `review_ladder` and the band ladders used for review are
  filtered identically. This is the finding above, written down as a test.
- **Every band, including fallback.** A band whose only permitted entries are
  unavailable does not resolve upward into a forbidden model.
- **No substitution.** When policy empties the eligible set, the decision is
  **blocked** and the block names the forbidden models. It does not descend a
  band, it does not relax a quality floor, and it does not report a bare
  "nothing eligible" that reads like a quota problem which will pass on its own.
- **Capacity stays distinct.** If any task-qualified candidate in the tried
  bands is permitted but lacks capacity, the decision remains unplaced without
  a policy block. `plan` may try it in a later wave when a slot opens.
- **Blocked is not unplaced.** In `plan`, a policy block lands in `blocked`
  rather than `unplaced`, because waiting for another wave will not un-forbid a
  model. `cmd_plan` prints the reason per blocked task for the same reason:
  telling an operator to tighten the brief sends them to the wrong file.

## The exception contract

A task that genuinely requires a forbidden model can have it, and cannot have it
quietly.

```bash
# Recorded and escalated. Not dispatchable: no launch command is printed.
rightsize route --spec task.md \
  --require-model codex:gpt-6-astra \
  --necessity "the failing transport handshake only reproduces under Astra's ultra effort level, which no other profiled model exposes"

# Dispatchable, with the necessity and the approver on the decision.
rightsize route --spec task.md \
  --require-model codex:gpt-6-astra \
  --necessity "..." \
  --exception-approved-by karoliang
```

The same three flags exist on `rerun` and `plan`. On `plan` the exception
applies to every task in the batch, because a batch has no single necessity of
its own; a fan-out that needs a forbidden model for one task should route that
task on its own.

Four things are enforced:

1. The model must actually be under policy. Asking for an exception to something
   nobody forbade is a misreading of the config, and it is refused with exit 2.
2. `--necessity` must be specific. A justification that only restates the
   request ("required", "it is better here") is refused. A reason nobody can
   review later is not a reason.
3. Without `--exception-approved-by` the decision is **blocked** and marked
   `ESCALATE`, and no launch command is printed. An exception that dispatches on
   the strength of its own justification is a flag, not a contract.
4. An exception never lifts the **review** role. The necessity was recorded for
   the work, not for the second opinion on it.

The recorded exception is on the decision as `policy.exception`, with its model,
necessity, approver and status (`pending`, `approved` or `unused`), and travels
into the decision log with everything else.

## Preflight

`rightsize doctor` reports the forbidden list, then counts what each band has
left:

- **error** when a band has no permitted candidate at all, because every task at
  that floor will block rather than dispatch.
- **warn** when a band has exactly one, because a rerun at that floor has
  nothing left to choose and there is no independent reviewer.

That check began with a dependency nobody had connected. Before Sol joined
band 3, a missing `OPENCODE_API_KEY` made both OpenCode entries unusable,
leaving Opus as the only permitted band 3 candidate. The current band also has
Sol. With Codex and Claude usable, the router can choose Sol to implement and
Opus to review independently. `doctor` uses the last provider reading to warn
when only one permitted candidate is usable, or error when none is usable.

## Rollback

The owner can delete the `model_policy` section, or delete one entry from the
owner config, to return to the previous behaviour. A repository overlay cannot
perform that rollback. No ladder, profile, threshold or quality floor needs
restoring. Reverting the code commit is also safe, because a config carrying
`model_policy` is simply ignored by a build that does not know the key.

## Tests

`python3 -m pytest -q test_model_policy.py`. Offline, no tokens. Both repros
from #32 are pinned, each with its control: the same judgment and the same
headroom against a config with the policy removed still picks the forbidden
model, which is what makes the passing case mean something.

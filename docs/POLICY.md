# How a decision gets made

Every route is the same five steps, in this order. Nothing here is a
preference; each step is either arithmetic over a measured number or a typed
judgment with a threshold you can see and change.

```
probe -> eligibility -> judgment -> band -> pick
(what is left)  (who qualifies)  (what kind    (how much   (which one,
                                  of work)      model)      and why)
```

## 1. Probe: what is left, and how well we know it

Each provider returns buckets shaped `{id, percent, resets_at, source}`, where
`percent` is percent **used**.

`percent = None` means the number is genuinely unknown, and that is deliberately
not the same as zero. An unknown number flows through the whole policy as
unknown; it is never rounded into "probably fine".

| Provider | Current quota source |
| --- | --- |
| OpenCode Go | `GET /zen/go/v1/usage`, rolling/weekly/monthly windows |
| Codex | Native `codex app-server` rate-limit control under the selected account home; no rollout/transcript fallback |
| Claude Code | Native stream-json `get_usage` under verified login; explicitly labelled transcript/declared-budget fallback only when native control is unavailable |
| OpenRouter | Live key limits or account credits; published free-request counter when present, otherwise the local reported-request counter |
| MiniMax Token Plan | Native Token Plan remains endpoint, explicit rolling/weekly remaining percentages; ambiguous count fields are ignored |
| OpenCode Zen free | No measurable subscription quota; labelled free |

Native identity and freshness are separate from numeric headroom. Missing native
freshness or unreadable windows cannot become zero usage. Codex rollout files can
be shared across homes, so their location cannot establish quota-account identity.
Claude's computed fallback is an estimate, not eligibility for managed native
execution. Codex and Claude use different native protocols.

Probes run in parallel; the ordinary cache lifetime is60 seconds. `probe` and
`route --fresh` re-read. A cached reading is not a guarantee that quota or account
identity stayed unchanged; managed admission and execution perform their own
identity, freshness and denial checks.

### Advisory versus managed execution

Plain `route` and `plan` record recommendations but create no quota holds.
`--reserve` creates legacy advisory holds in the active JSON state. Explicit
launch reports account for actual external dispatches. Those records do not
prove that an Orca worker used the probed account, and advisory hooks cannot
prevent every external launch.

The separate `managed.sqlite3` ledger uses atomic admission transactions and
native launch/reconciliation evidence. It does not turn legacy external launches
into managed ones. Claude managed execution requires explicit disabled usage
credits; Codex requires native included-usage permission. Keep upstream Orca
account-handoff limits visible rather than inferring binding from a model name.
See [MANAGED.md](MANAGED.md) and [NATIVE-ADAPTERS.md](NATIVE-ADAPTERS.md).

The additional `outcomes.sqlite3` is private advisory evidence: durable decision,
launch, completion, review, acceptance, usage and rework records. It owns no quota
and does not change the managed database. See [consumer workflow](CONSUMER-WORKFLOW.md).

## 2. Eligibility: pure arithmetic, no opinions

For each provider:

```
binding bucket = the bucket with the least (100 - percent - reserve)
usable         = that number, in percentage points
```

The **binding bucket** is the one that will stop you first, which is not always
the one with the most spent. A provider is blocked when any of these holds:

- the probe failed, or there is no credential;
- it was reported exhausted and its reset has not passed yet (see rule 5);
- `usable <= 0`, meaning it is below its configured reserve.

The reserve is the point of the whole line. It keeps a slice of every plan
unspent, so that an escalation later in the day still has somewhere to go.

**Pace, across every window.** A percentage on its own says nothing about
whether it is too much. 77 per cent of a week with a day left is fine; 38 per
cent of a month with 27 days left is not. Dividing what has been spent by how
much of the window has passed says which, with no history needed:

```
elapsed   = 1 - (resets_at - now) / window
projected = percent / elapsed      # where the window lands at this rate
```

This runs on **every** bucket, not only the binding one, because the windows are
nested: every token spent against the weekly is also spent against the monthly,
so a weekly that looks cheap to empty can be the thing that exhausts the month.
Real numbers that prompted it:

| bucket | used | elapsed | pace | projected by reset |
| --- | --- | --- | --- | --- |
| rolling | 17% | 83% | 0.2x | 20% |
| weekly | 77% | 89% | 0.9x | 87% |
| monthly | 38% | 10% | 3.7x | **371%** |

The binding bucket was the weekly, so the policy saw eight points expiring in
nineteen hours and hurried to spend them, while the month they also came out of
was on course to be gone in a week. A bucket projecting past 100 per cent now
takes the provider out of the cheap bands, the same treatment as a measured
burn-rate overrun, so what is left is kept for work with nowhere cheaper to go.

Right after a reset the ratio means nothing, so pacing is only computed once 5
per cent of the window has passed and at least 2 per cent has been spent.

**Burn rate.** Each fresh probe stores a snapshot. Given two readings and a
reset time:

```
rate      = (percent_now - percent_before) / seconds_between
projected = percent_now + rate * seconds_until_reset
overrun   = projected > 100
```

An overrunning provider stops being offered for cheap work **while it still has
room**, which is rule 4: drop a tier before the wall, not at it. Its remaining
headroom is reserved for the expensive work that has nowhere else to go.

## 3. Judgment: five questions, one request

The model is asked what kind of work the task is. It is never asked which
provider to use: providers change every few months, the questions do not, and
the numbers are already exact in step 2.

| id | type | what it decides |
| --- | --- | --- |
| `tier` | choice | mechanical / implementation / design / diagnosis / high_stakes |
| `size` | score | one file, one package, or cross-repo |
| `second_opinion` | noul | is a different model worth putting on the review |
| `spec_complete` | noul | can a worker finish this without asking a question |
| `destructive` | noul | is there a step that editing code cannot undo |

They are independent, so they go in one request and are answered in parallel.
Without `TYPESAFE_API_KEY`, a keyword heuristic stands in and says so in every
line of output.

`spec_complete` is not about routing at all. Below
`thresholds.spec_complete_min` (0.35) the dispatch is **blocked**, because a
brief that depends on context the worker does not have fails on every provider.
Exit code 1, so a script can stop there.

## 4. Band: how much model the work needs

```
mechanical, implementation            -> band 1
design, diagnosis, high_stakes        -> band 3
size >= size_escalates_band (1.5)     -> +1 band (never past 3 by this route)
high_stakes OR second_opinion >= 0.6 -> require independent review, band unchanged
```

Band 1 is the default because price is the burn multiplier on a percent-bucket
plan: a model at `$3/M` input eats a weekly allowance twenty times faster than
one at `$0.15/M`. Escalation has to be earned by the judgment, not chosen by
mood.

## 5. Pick: spend the bucket that expires first

Walk the band's ladder from `config.json`, dropping candidates whose provider:

- is not eligible;
- has **unknown** headroom, while the band is below 3 and the provider is not
  free. An unreadable window must never masquerade as spendable capacity;
- is **overrunning**, while the band is below 3.

Then the order depends on how expensive this dispatch is.

**Cheap bands take the soonest reset.** Capacity that resets in three hours is
about to be thrown away; a monthly bucket is the scarce thing. Ladder position
is only a tiebreak, so the ladder is a preference, not a priority.

**At band 3 the order inverts: the roomiest plan wins.** Rule 3 exists so that
vanishing capacity is not wasted, and the way to waste it is to spend it on the
most expensive rung. A bucket with a handful of points and a reset in the
morning should absorb as much cheap work as it can; expensive work belongs on
the plan with a week of room, which would otherwise sit idle.

This came from a real dispatch. On 2026-09-20 money.financial had opencode's
weekly at 77 per cent used with eight points left and nineteen hours to run,
while Codex sat at 0 per cent with a week, and a design task went to
`opencode:glm-5.3` on the emptying bucket. That repo worked around it by raising
its own opencode reserve; the policy now handles it, and the same state resolves
to `codex:gpt-6-astra` at high effort.

A candidate whose bucket cannot cover the dispatch's estimated cost at all is
passed over rather than merely ranked low. `expensive_band` (3) moves the
threshold; 4 disables the inversion.

Free providers have no reset time and sort last, which is exactly right for
OpenCode Zen's `-free` models, which cost no quota. They are currently off
every ladder, though: measured on 2026-09-20 (docs/FREE-MODELS.md), eight calls
across two models and two coding tasks returned **no usable answers at all**,
every one failing with `Rate limit exceeded. Please try again later.` before any
output.

Note what that corrects, because the mistake is easy to repeat. This document
used to say those models were *slow*, on the strength of a call that took over
two minutes. Timing a call that never produces an answer measures the wait for a
refusal, not the model. Their speed and their coding ability are unmeasured, not
bad; the retest is one command and is in `config.json` beside the ladder.

**Task fit first.** Task profiles declare required capabilities and a minimum
band. Model profiles declare provisional capabilities, maximum approved band,
family and supported effort levels. Missing profiles and incompatible models
are excluded before quota ordering. These assignments preserve the existing
ladder; they are not comparative quality results.

The shipped capability and family assignments are provisional operator policy,
not model-certified or comparative benchmark results. Supported effort controls
come from the installed runtime/catalog evidence described in [TASK-FIT.md](TASK-FIT.md).
The completed paired pilot selected the same Codex models on both arms; its20/20
acceptance per arm does not establish cross-model superiority. Do not assign a
capability merely to make a blocked task run.

**Nothing eligible?** Preserve the task's original band and any stricter task
or retry floor. A single route may relax forecast pacing within that band
(existing measured-burn/reserve checks still apply), then try higher bands.
It returns no pick when qualified capacity is unavailable. Never downgrade a
high-stakes task because a cheap plan has quota.

**Review leg.** The review uses the same quality floor and task requirements.
Band1 uses the configured review ladder; higher floors use the corresponding
band ladder. Exclude the same provider and the same declared model family,
including a model served by different subscription providers. A missing review
is explicitly outstanding, not completed. Planner debits use the review band.

High-stakes tasks require independent review even below the optional score
threshold. Other tasks require it at `second_opinion >= 0.6`. A missing reviewer
remains outstanding. The advisory outcome store requires linked completion,
evidence-backed acceptance and a qualifying accepted review after any recorded
repairs. External worker settlement remains separate. See the exact receipt and
event commands in [CONSUMER-WORKFLOW.md](CONSUMER-WORKFLOW.md).

**Irreversible steps.** When `destructive` clears its threshold, the decision is
marked `confirm_first`. rightsize never decides that a migration or a deploy may
run unattended; it only makes sure nobody dispatches one without noticing.

## 6. Effort: the second dial

The model profile supplies supported effort levels and task-specific defaults.
An explicit ladder effort overrides the default only when supported. Retry
count increases effort by that many supported steps; wide blast radius or an
irreversible step adds one further step, capped at the model's highest supported
level. Never translate an effort label into an equivalent amount of reasoning
across providers. Models with no exposed effort control receive no flag.

Older configurations without model profiles retain provider-level defaults.
The shipped configuration uses profiles. See [task-fit details](TASK-FIT.md).

## 7. Reroute: the work is better evidence than the brief

The first judgment sees only what was written down. What happened when someone
tried it is a stronger signal, so `rightsize rerun` judges again with the
outcome appended to the state, and applies three rules that a fresh route
cannot:

- the band starts one above the band that already failed, capped at 3;
- the `provider:model` that failed is excluded from every ladder;
- effort goes up a level.

This is the intended path when a worker comes back with work that does not hold
up, and it is deliberately not automatic: something has to observe the outcome,
and rightsize is not in the token path.

## Fan-out: many dispatches, one quota reading

A quota reading says what has been **billed**, not what is about to be. Route a
hundred tasks inside one cache window and every one of them sees the same
untouched headroom, so every one picks the same provider. Rule 3 makes this
worse rather than better: "spend the bucket that expires first" is
deterministic, so the pile-up is not even spread by luck.

Measured before the fix: 100 ordinary tasks, 100 dispatches to one provider,
`usable` unchanged at 9 points throughout.

Three things close that gap.

### Reservations

A dispatch that has been decided but not finished holds a cost:

```
points held = dispatch_cost[provider] * band
usable      = 100 - percent - reserve - points held by live reservations
```

`dispatch_cost` starts as a coarse estimate in `config.json`, because the exact
burn is unknowable before the worker runs, and being roughly right is enough to
stop a fan-out from overcommitting a plan. Multiplying by the band is the cheap
approximation of "a deeper model costs more".

It does not have to stay a guess. `rightsize calibrate` measures it:

```
opencode
  weekly window opened 6d 4h ago, 226 dispatches since
  measured 0.34 points per dispatch, config says 0.6
  tokens 35,652,505 in (35,268,071 cached), 3,883,646 out, 174,938 per dispatch
  -> set dispatch_cost.opencode to 0.34
```

The arithmetic needs no history, because a bucket's window already has a start:
its reset time minus its length. Orca records every session it sees from
opencode's database and Claude's transcripts, so counting the sessions inside
that window gives the denominator, and the bucket's own percentage gives the
numerator. Percentage points divided by dispatches is `dispatch_cost` in the
unit the config uses. `--apply` writes it.

Two things make the answer an upper bound, and both are printed when they
apply: a session record that begins after the window did cannot have counted
every dispatch, and a scan that last ran hours ago has not seen what happened
since. Providers with no local session record (Codex, OpenRouter, the Zen free
models) keep their estimate and say so.

When Claude's native percentage is unavailable, calibration can report
transcript tokens and suggest a declared `weekly_token_budget`. That fallback
is explicitly an estimate, not native subscription telemetry or managed
eligibility. The transcript scan includes categories omitted by older Orca
usage summaries; do not mix counters without checking their definitions.

Reservations are taken by `route --reserve` and by `plan --reserve`, never by a
plain `route`: a decision made to look at the numbers must not eat capacity
nobody is going to spend. They are released by
`rightsize report <provider> --done`, and they expire on their own after
`reservation_ttl_seconds` (1800), because a worker that dies silently must not
hold a plan hostage.

Managed admission is the same idea with stronger guarantees: it is an
atomic `managed.sqlite3` write under `BEGIN IMMEDIATE`, with point and slot
commitments taken together, idempotency keys and per-attempt receipts. The
legacy compatibility lock stops concurrent advisory writers from racing a
managed hold, and `managed run` re-validates the native quota and account
fingerprint before issuing a launch claim. Advisory `state.json` and
managed `managed.sqlite3` stay separate on disk; nothing here is claimed to
exist as a single unified ledger.

### An in-flight limit per provider

`max_inflight` caps how many dispatches may be running on one provider at once.
A provider that is full is blocked the same way one below its reserve is, so
the next task goes to the next eligible provider instead of queueing behind it.
This is what actually spreads a fan-out; the quota debit alone would not,
because a free provider has no quota to debit.

### Waves

`rightsize plan` routes a whole batch at once. Judgments are independent, so
they go out in parallel; allocation is not, so tasks are placed one at a time
against headroom the earlier ones have already spent.

When every provider is full, the remaining tasks go into the next **wave**
rather than being sent somewhere that suits them worse. A wave boundary returns
the in-flight slots (those workers have finished) but keeps every quota debit,
so a plan eventually runs out of capacity instead of scheduling waves forever.

100 tasks, a fresh OpenCode week, this machine's real config:

```
wave 1:  37 tasks  (openrouter 12, opencode 10, opencode_zen 8, codex 4, claude 3)
wave 2:  37 tasks  (same shape)
wave 3:  26 tasks  (openrouter 12, opencode 10, codex 4)
```

### One rule inverts inside a batch

A single `route` may escalate to a higher band, but never drops below the
task/retry quality floor. If no qualified capacity exists, it returns no pick.

Inside a batch there is a next wave, so `plan` does the opposite and holds the
task. Sending ordinary implementation work to a band 3 model because the cheap
plans are momentarily busy is the expensive mistake this tool exists to
prevent, and at a hundred tasks it is expensive a hundred times over. Before
this rule existed, a 100-task fan-out put three band 1 tasks on
`claude-opus-5`.

Tasks blocked for a low `spec_complete` are not a capacity problem and never
enter a wave. They are listed separately, because the fix is to the brief.

## Rule 5: fallback is code

rightsize decides and steps out. It is not a proxy and never sits in the token
path, so it cannot see a worker die. The caller that does see it reports back:

```bash
rightsize report opencode --quota-error
```

That marks the provider exhausted until the latest known denied bucket reset (or a
`--minutes` cooldown when the provider publishes no reset time), so the next
route picks the next eligible candidate instead. `--clear` lifts it early.

Managed native outcomes retain their own evidence-backed lifecycle and denial
checks. Do not treat an advisory success report as managed completion or assume
these paths are isolated from the shared legacy denial/commitment checks.

## Changing any of this

Everything above is in `config.json`: reserves, ladders per band, the review
ladder, thresholds, cache lifetime, launcher templates. The code holds the
rules; the file holds the numbers. If a change needs a code edit, that is worth
an issue: it usually means a rule is hard-coded that should have been a knob.

## Why the questions are measured

A question that never fires looks exactly like a question with nothing to
report. `./eval_questions.py` runs each question against labelled fixtures and
demands a **margin**, not merely the right ordering.

The first `spec_complete` failed that check: it scored 0.02 to 0.11 across four
fixtures, ordering them correctly and discriminating nothing. Its criteria asked
whether "every file, name, expected behaviour and acceptance check needed is
stated", which is false for any real task, since an implementation always
touches something the brief did not name. A System One model reads criteria
literally and answered exactly that.

| fixture | original | current |
| --- | --- | --- |
| "fix the invoices thing" | 0.02 | 0.12 |
| "make the change we discussed" | 0.02 | 0.07 |
| "add pagination to the invoices list endpoint" | 0.07 | 0.61 |
| a task naming repo, file, params and tests | 0.11 | 0.76 |

Separation went from 0.09 to 0.49 with no code change: the criteria now describe
what an engineer needs in order to start, and grant them the codebase and their
own judgment for the rest. The threshold sits at 0.35, in the gap rather than
beside either side.

Two fixture labels turned out to be wrong rather than the model, and were
deleted rather than argued with. That is the expected outcome often enough to be
worth saying out loud: a disagreement between a question and a fixture is not
automatically the question's fault.

## Explicit quota denial (2026-09-21)

A bucket reporting `rate-limited`, `exhausted`, `quota-exceeded`, or measured
usage of at least 100% vetoes every band. It must never become merely unknown
headroom because its numeric percentage is absent. Persist the denial across
failed/missing refreshes; remove it only when a fresh live reading shows that
window healthy. Other healthy buckets and older snapshots cannot override it.

A quota-error report considers only denied buckets when selecting a reset,
waiting for the latest if several bind. If a denied window lacks a future reset,
use the bounded caller cooldown and re-probe, not the first healthy reset in the
response. Reports invalidate cached telemetry. This repairs eligibility;
mandatory launch admission across clients remains an architecture decision.

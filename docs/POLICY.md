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

| Provider | Source | `source` field |
| --- | --- | --- |
| OpenCode Go | `GET /zen/go/v1/usage`, three buckets with reset times | `live` |
| Codex | `rate_limits` in the newest rollout transcript | `stale-lower-bound` |
| Claude Code | tokens reconstructed from transcripts, divided by a declared budget | `computed` or `no-budget-set` |
| OpenRouter | `GET /api/v1/key` for credit, a local counter for free requests | `live` / `local-count` |
| OpenCode Zen free models | no meter exists | `free: true` |

### A reading has an age, and age is not the same as caching

Caching is about not asking the same question twice in a minute. **Staleness is
about a source that cannot be asked at all.** OpenCode and OpenRouter answer
live whenever you ask. Codex does not: its numbers exist only in the transcript
of the last Codex session, so between sessions the file ages while the real
quota moves underneath it.

That failed in exactly the dangerous direction. A 19-hour-old reading of 93 per
cent used kept Codex hard-blocked after its quota had moved on, and nothing in
the output said the number was from yesterday. An old **high** reading is worse
than no reading, because it looks authoritative and removes a provider.

So a file-sourced bucket now carries `age_seconds`, and:

- past `staleness_seconds` (6h for Codex) the percentage becomes `None`, which
  means escalation-only rather than unusable, and `probe` prints
  `expired-reading  observed 19h 51m ago`;
- if its `resets_at` has already passed, the window rolled over and the bucket
  is counted as **empty**, with the reset time rolled forward. Codex usage only
  accrues by running Codex, and running Codex writes a newer reading, so a
  window newer than the newest transcript has nothing spent in it.

Refreshing it needs an interactive Codex session. Verified 2026-09-20:
`codex exec` answers but writes no rollout, and neither Codex's own sqlite
stores nor Orca's local files carry the numbers, which is what
[Orca issue #21746](https://github.com/stablyai/orca/issues/21746) asks for.

Probes run in parallel and the reading is cached for `cache_seconds` (60 by
default). A dispatch takes minutes; a quota number from a minute ago is the
same number. `rightsize probe` and `route --fresh` always re-read.

The Claude transcript scan is the slowest thing in the tool and only `rightsize
probe` pays for it: with no budget set, the percentage is `None` whatever the
count says, so routing would be buying a number that cannot change its answer.

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
second_opinion >= 0.6                 -> add a review leg, band unchanged
```

Band 1 is the default because price is the burn multiplier on a percent-bucket
plan: a model at `$3/M` input eats a weekly allowance twenty times faster than
one at `$0.15/M`. Escalation has to be earned by the judgment, not chosen by
mood.

## 5. Pick: spend the bucket that expires first

Walk the band's ladder from `config.json`, dropping candidates whose provider:

- is not eligible;
- has **unknown** headroom, while the band is below 3 and the provider is not
  free. This is the rule that keeps Claude Code escalation-only: an estimate
  must never masquerade as spendable capacity;
- is **overrunning**, while the band is below 3.

Everything that survives is sorted by **reset time, soonest first**, with ladder
position only as a tiebreak. So the ladder is a preference, not a priority.
Capacity that resets in three hours is about to be thrown away; a monthly bucket
is the scarce thing.

Free providers have no reset time and sort last, which is exactly right for
OpenCode Zen's `-free` models: they cost no quota but a single-word reply can
take over two minutes, so they are overflow capacity, not the default.

**Nothing eligible?** Fall down one band at a time, then, if still nothing below
band 3, escalate to band 3 rather than return nothing. A run is never stranded
because the cheap options are gone.

**Review leg.** When `second_opinion` clears its threshold, a second candidate
is picked from `review_ladder`, filtered to a different provider than the
primary. A model reviewing its own work is worth less than a cheap model from
another vendor reviewing it.

**Irreversible steps.** When `destructive` clears its threshold, the decision is
marked `confirm_first`. rightsize never decides that a migration or a deploy may
run unattended; it only makes sure nobody dispatches one without noticing.

## 6. Effort: the second dial

The band chooses which model. For CLIs that expose a reasoning-effort setting,
`config.effort` maps band to a level per provider, and three things raise it one
step without touching the model choice:

```
size >= size_escalates_band   -> +1   (a wide blast radius wants more care)
destructive >= destructive_min -> +1  (an irreversible step wants more care)
attempt (a rerun)             -> +1   (the last attempt at this level failed)
```

A candidate that names its own effort on the ladder (`codex:gpt-6-astra:high`)
starts from that. A provider absent from `config.effort` gets `None`, and its
launcher template leaves the flag off rather than inventing a level.

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

Claude is the exception in the other direction: it publishes no percentage to
divide, so calibration reports tokens instead and suggests a
`weekly_token_budget` from them. That number comes from rightsize's own
transcript scan rather than Orca's totals, which keep only the most recent
sessions and omit cache creation: 18M against an actual 218M.

Reservations are taken by `route --reserve` and by `plan --reserve`, never by a
plain `route`: a decision made to look at the numbers must not eat capacity
nobody is going to spend. They are released by
`rightsize report <provider> --done`, and they expire on their own after
`reservation_ttl_seconds` (1800), because a worker that dies silently must not
hold a plan hostage.

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

A single `route` never strands a task: a full band drops to a cheaper one and
then escalates, because the alternative is a run that does not happen.

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

That marks the provider exhausted until its binding bucket's known reset (or a
`--minutes` cooldown when the provider publishes no reset time), so the next
route picks the next eligible candidate instead. `--clear` lifts it early.

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

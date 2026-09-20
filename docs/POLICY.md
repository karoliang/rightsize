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

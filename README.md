# rightsize

Pick the subagent provider and model for a coding task, from live quota.

If you run coding agents across several plans at once (an OpenCode
subscription, Codex, Claude Code, OpenRouter's free tier), every dispatch is a
small decision: which plan has headroom, which bucket is about to reset and
expire unused, and how much model this particular task actually needs. This
answers that in about two seconds, with a stated reason.

```
$ rightsize route --task "add a rate limit to the signup endpoint" --orca
judgment   jev (jev-1.13.0)
           tier=implementation size=0.83 second_opinion=0.21 spec_complete=0.94 destructive=0.03
dispatch   band 1 -> agent opencode, model deepseek-v4.1-flash
  why      tier implementation starts at band 1
  quota    codex:gpt-5.6-luna:low skipped: below reserve on primary-10080m
  quota    opencode:deepseek-v4.1-flash chosen: its weekly bucket resets in 21h 50m
           with 9 points usable, so spend it before it expires

orca orchestration worker-start --spec "<task>" --worktree current --agent opencode --json
# then inside that terminal: opencode -m opencode-go/deepseek-v4.1-flash
```

The split it is built on: **quota is arithmetic, the task is a judgment.**
Headroom, reserves, burn rate and fallback are ordinary code, because the
numbers are already exact and a model asked to do subtraction can be wrong
about them. What kind of work a task is, and how much model it needs, goes to
a typed judgment. The model is never asked which provider to use, because
providers change every few months and the questions do not.

It decides and steps out. It is not a proxy and never sits in the token path.

No dependencies. Python 3 standard library only.

## Install

```bash
git clone https://github.com/karoliang/rightsize ~/Github/rightsize
ln -s ~/Github/rightsize/rightsize ~/.local/bin/rightsize   # anywhere on PATH
rightsize refresh
rightsize probe
```

Keys are optional to start: without them you get fewer providers and a crude
heuristic in place of the judgment, and the output says so. See
[docs/KEYS.md](docs/KEYS.md) for where each one comes from, what it costs, and
what you lose without it. The short version: `TYPESAFE_API_KEY` is the one that
matters, at roughly three cents per thousand routing decisions.

## Use

```bash
# What is left, everywhere.
rightsize probe

# Decide one dispatch.
rightsize route --task "add a rate limit to the signup endpoint"
rightsize route --spec task.md --orca          # print the Orca worker-start line
rightsize route --spec task.md --launcher shell # print a plain CLI invocation
rightsize route --spec task.md --json          # for scripts; exit 1 if blocked

# Decide a whole fan-out, spread across plans, in waves.
rightsize plan --specs tasks.txt --reserve --launcher orca

# Tell it how a dispatch went, so the next one knows.
rightsize report opencode --done               # that worker finished, release its capacity
rightsize report opencode --quota-error        # skip that plan until its bucket resets

# It came back and the work does not hold up: judge again, knowing that.
rightsize rerun --spec task.md --previous opencode:deepseek-v4.1-flash \
  --because "changed the response shape and could not make the tests pass"

# What the catalogues offer, and what got cheaper overnight.
rightsize models
rightsize deals
```

## Effort, and changing your mind later

The band picks the model. **Effort** is the second dial on that model, for the
CLIs that take it (Codex and Claude here, configured per band in
`config.json`). Three things turn it up without changing the model: a wide blast
radius, a step that cannot be undone, and a retry after an attempt that failed.
A provider with no effort setting gets no flag, rather than a made-up one.

The first judgment is made from the brief alone. What the work turns into is
better evidence, and `rerun` uses it:

```
$ rightsize rerun --spec task.md --previous opencode:deepseek-v4.1-flash \
    --because "changed the response shape and could not make the tests pass after three tries"
rerun      opencode:deepseek-v4.1-flash did not finish it: changed the response shape ...
dispatch   band 2 -> agent opencode, model minimax-m3
  why      a previous attempt was band 1, so this starts at band 2
```

It judges the task again with the outcome attached, starts one band above the
attempt that failed, raises effort a level, and takes the model that just failed
out of the running. A task is allowed to turn out harder than it read.

## Fanning out: ten agents, or a hundred

A quota reading says what has been billed, not what is about to be. Route a
hundred tasks inside one cache window and all hundred see the same untouched
headroom, so all hundred pick the same provider. That was measured, not
theorised: 100 tasks, 100 dispatches to one plan, `usable` unchanged at 9
points throughout.

`rightsize plan` routes the batch instead of the task:

```
$ rightsize plan --specs tasks.txt --reserve
   0  w1  band 1  opencode:deepseek-v4.1-flash                       add cursor pagination to GET /api/invoices
   2  w1  band 3  opencode:glm-5.3                          CONFIRM  drop the legacy invoices_v1 table
   7  -   band 1  -                                         BLOCKED  make the change we discussed
  10  w1  band 3  claude:claude-opus-5                      CONFIRM  rotate the Stripe webhook secret

waves (each one runs after the previous reports done)
  wave 1:  37 tasks  (openrouter x12, opencode x10, opencode_zen x8, codex x4, claude x3)
  wave 2:  37 tasks
  wave 3:  26 tasks
```

What makes that work:

- **Judgments in parallel, allocation in sequence.** The five questions are
  independent per task, so they go out concurrently (`--concurrency`, default
  8; 100 tasks judge in about ten seconds and cost roughly three cents). Each
  task is then placed against headroom the earlier ones have already spent.
- **Reservations.** A decided-but-unfinished dispatch holds an estimated cost
  (`dispatch_cost[provider] * band`) so it stops being invisible.
  `rightsize report <provider> --done` gives it back, and a reservation expires
  on its own after 30 minutes so a worker that dies silently cannot hold a plan
  hostage.
- **An in-flight cap per provider** (`max_inflight`). A full provider is
  blocked like one below its reserve, so the next task goes elsewhere instead of
  queueing. This is what spreads the fan-out; a quota debit alone would not,
  since a free provider has no quota to debit.
- **Waves.** When everything is full, remaining tasks wait for the next wave
  rather than being sent to a model that suits them worse. A wave returns the
  in-flight slots but keeps every quota debit, so a plan runs out of capacity
  eventually instead of scheduling forever.

One rule deliberately inverts here. A single `route` never strands a task: a
full band drops a tier and then escalates. Inside a batch there is a next wave,
so `plan` holds the task instead. Sending ordinary implementation work to a
band 3 model because the cheap plans are momentarily busy is the expensive
mistake this tool exists to prevent, and across a hundred tasks it is expensive
a hundred times over.

## Wire it into your agent

The decision is only worth having if it happens on every dispatch, not on the
ones you remember. `hooks/claude_pretooluse.py` is a working Claude Code
PreToolUse hook: it watches for Bash commands that start a worker, routes the
task, and injects the answer as context.

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
  {"type": "command", "command": "python3 /path/to/rightsize/hooks/claude_pretooluse.py", "timeout": 25}
]}]}}
```

It routes with `--reserve`, so twenty workers launched one after another are
not all handed the same untouched headroom, and it tells the agent to run
`rightsize report <provider> --done` when the worker finishes.

It is advisory by construction: it prints, it never blocks, and it exits 0 on
every failure path. A routing helper that can stop a dispatch is a routing
helper that can strand a run. Ports to other orchestrators are welcome, and
[docs/ADAPTERS.md](docs/ADAPTERS.md) has the JSON contract they build on.

## Where the quota numbers come from

| Provider | Source | Quality |
| --- | --- | --- |
| OpenCode | `GET https://opencode.ai/zen/go/v1/usage` returns rolling, weekly and monthly percent with reset times | exact, live |
| Codex | `rate_limits` in the newest `~/.codex/sessions/**/rollout-*.jsonl` | exact, but only written when Codex runs, so it is a lower bound |
| Claude Code | token counts reconstructed from `~/.claude/projects/**/*.jsonl` | estimate; needs a budget in `config.json` before it counts as headroom |
| OpenRouter | `GET https://openrouter.ai/api/v1/key` for credit, plus a local daily counter for free requests | live for credit, local for the free-tier cap |

The differences matter and the policy keeps them. An exact number can be spent
down to a thin reserve. An estimate cannot, so a provider whose headroom is
unknown is only ever used for work that has already earned an escalation.

Probes run in parallel and a reading is cached for 60 seconds, because a
dispatch takes minutes and a quota number from a minute ago is the same number.
`rightsize probe` and `route --fresh` always re-read.

## The policy, in five rules

1. **Band from the judgment.** Mechanical and ordinary implementation sit in
   band 1. Design, diagnosis and anything touching auth, money, migrations or
   published output start at band 3. A large blast radius raises the band.
2. **Eligibility is a filter, not an opinion.** A provider qualifies when its
   binding bucket has headroom left above its configured reserve.
3. **Spend the bucket that expires first.** Among candidates that all clear the
   band, take the one whose quota resets soonest. Capacity that resets in three
   hours is about to be thrown away; a monthly bucket is the scarce thing.
4. **Drop a tier before the wall, not at it.** If the current burn rate
   projects past 100 percent before the reset, that provider stops being
   offered for cheap work while it still has room for the expensive work.
5. **Fallback is code.** A worker that comes back with a quota error is reported
   with `rightsize report`, which marks that provider exhausted until its known
   reset; the same spec then routes to the next eligible one.

Price is the burn multiplier on a percent-bucket subscription: a model at
`$3/M` input eats your weekly allowance twenty times faster than one at
`$0.15/M`. That is why band 1 is the default and escalation needs a reason.

[docs/POLICY.md](docs/POLICY.md) walks the whole decision, including the
arithmetic for burn rate and the rule that keeps an estimate from masquerading
as spendable capacity.

## The judgment

One request, five independent questions over the same state, answered by
[TypeSafe's](https://typesafe.ai) Jev:

| id | type | judgment |
| --- | --- | --- |
| `tier` | choice | mechanical, implementation, design, diagnosis, high_stakes |
| `size` | score | one file / one package / cross-repo or new subsystem |
| `second_opinion` | noul | would a different model catch something on review |
| `spec_complete` | noul | could an agent finish this without asking a question |
| `destructive` | noul | is there a step that cannot be undone by editing code |

`spec_complete` earns its place independently of routing: a low answer blocks
the dispatch entirely, because a brief that relies on context the worker does
not have will fail on any provider.

Without `TYPESAFE_API_KEY` the tool falls back to a crude keyword heuristic and
says so in its output, so a route made without a judgment is never mistaken for
one made with it.

### The questions are measured, not assumed

`./eval_questions.py` runs each question against labelled fixtures and requires
a **margin**, not merely the right ordering. A question that never fires looks
exactly like a question with nothing to report.

The first version of `spec_complete` failed that check. Its criteria asked
whether "every file, name, expected behaviour and acceptance check needed is
stated", which a literal reader answers "no" for any real task, since an
implementation always touches something the brief did not name:

| fixture | original wording | current wording |
| --- | --- | --- |
| "fix the invoices thing" | 0.02 | 0.12 |
| "make the change we discussed" | 0.02 | 0.07 |
| "add pagination to the invoices endpoint" | 0.07 | 0.61 |
| a fully specified task naming repo, file, params and tests | 0.11 | 0.76 |

Separation went from 0.09 to 0.49. Nothing changed but the wording: the
criteria now describe what the engineer needs in order to start, and grant them
the codebase and their own judgment for the rest.

Two fixture labels were wrong rather than the model. "fix the invoices thing"
was expected to be ordinary implementation and came back `high_stakes`, which
is defensible when the only noun is money-adjacent, and its size came back
mid-scale, which is right for unknown scope. Both expectations were removed
rather than argued with.

## Configuration

Everything tunable is in `config.json`: reserves per provider, the ordered
candidate ladder per band, the review ladder, thresholds, cache lifetime, and
the launcher templates. Edit that, not the code.

Claude Code publishes no quota API, so `claude.weekly_token_budget` is `null`
by default and Claude stays escalation-only. Set a token budget to let it take
ordinary work; `rightsize probe` prints the raw token counts to pick one from.

## Credentials

Read in this order, and never written into the repository:

1. Environment variables.
2. [Infisical](https://infisical.com), when the repo is linked with
   `infisical init` and the CLI is installed.
3. For OpenCode only, the existing `~/.local/share/opencode/auth.json` that the
   OpenCode CLI already maintains.

| Variable | Needed for |
| --- | --- |
| `TYPESAFE_API_KEY` | the Jev judgment; without it the heuristic runs |
| `OPENROUTER_API_KEY` | OpenRouter credit and model catalogue |
| `OPENCODE_API_KEY` | OpenCode Go quota; falls back to the CLI's own auth file |
| `OPENCODE_ZEN_API_KEY` | the OpenCode Zen catalogue; falls back to the same file |

Codex and Claude Code need no credential here: both are read from files their
own CLIs already write. Full instructions per provider, including costs and
free-tier limits, are in [docs/KEYS.md](docs/KEYS.md).

## Daily refresh

`bin/rightsize-daily` refreshes the catalogues and appends the result to
`daily.log`. Free models and price cuts are the point of running it: a model
that becomes free is the cheapest way to move work off a metered bucket.

```bash
sed "s|REPO_PATH|$HOME/Github/rightsize|g" com.rightsize.refresh.plist \
  > ~/Library/LaunchAgents/com.rightsize.refresh.plist
launchctl load ~/Library/LaunchAgents/com.rightsize.refresh.plist
```

## Tests

```bash
python3 test_rightsize.py    # policy, offline, no tokens
./eval_questions.py          # the judgments, needs TYPESAFE_API_KEY, costs a fraction of a cent
```

Synthetic quota states and judgments, asserted end to end. No network, no
tokens spent, no dependency on the machine's own quota. Every policy rule above
has a case, including the ones that are easy to get wrong: unknown headroom must
not be treated as free capacity, an exhausted ladder must escalate rather than
return nothing, and a reported quota error must take a provider out of the next
route.

## Documentation

- [docs/POLICY.md](docs/POLICY.md): how a decision is made, rule by rule.
- [docs/KEYS.md](docs/KEYS.md): getting each API key, what it costs, what
  breaks without it.
- [docs/ADAPTERS.md](docs/ADAPTERS.md): adding a launcher (config), a caller
  (JSON), or a provider (one function).
- [CONTRIBUTING.md](CONTRIBUTING.md): the design rules, and the evidence a
  question change needs.
- [SECURITY.md](SECURITY.md): what this reads, what it writes, what it sends.

## Licence

MIT.

# rightsize

Pick the subagent provider, model and effort for a coding task, from live quota.

If you run coding agents across several plans at once (an OpenCode
subscription, Codex, Claude Code, OpenRouter's free tier), every dispatch is a
small decision: which plan has headroom, which bucket is about to reset and
expire unused, which one is quietly on course to run out before its window
does, and how much model this particular task actually needs. This answers that
in about two seconds, with a stated reason.

```
$ rightsize route --task "add a rate limit to the signup endpoint" --orca
judgment   jev (jev-1.13.0)
           tier=implementation size=0.83 second_opinion=0.21 spec_complete=0.94 destructive=0.03
dispatch   band 1 -> agent opencode, model deepseek-v4.1-flash
  why      tier implementation starts at band 1
  quota    codex:gpt-5.6-luna:low skipped: below reserve on primary-10080m
  quota    opencode:deepseek-v4.1-flash chosen: its weekly bucket resets in 21h 50m
           with 9 points usable, so spend it before it expires

orca worktree create --name add-rate-limit-signup-endpoint --json > /dev/null
HANDLE=$(orca terminal create --worktree name:add-rate-limit-signup-endpoint \
  --command 'opencode -m opencode-go/deepseek-v4.1-flash' --json | ...)
orca orchestration worker-start --spec "..." --worktree name:... --terminal "$HANDLE" --json
```

The split it is built on: **quota is arithmetic, the task is a judgment.**
Headroom, reserves, pacing and fallback are ordinary code, because the numbers
are already exact and a model asked to do subtraction can be wrong about them.
What kind of work a task is, and how much model it needs, goes to a typed
judgment. The model is never asked which provider to use, because providers
change every few months and the questions do not.

It decides and steps out. It is not a proxy and never sits in the token path.

No dependencies. Python 3 standard library only.

## Install

```bash
git clone https://github.com/karoliang/rightsize ~/Github/rightsize
ln -s ~/Github/rightsize/rightsize ~/.local/bin/rightsize   # anywhere on PATH
rightsize refresh
rightsize doctor     # config, catalogue, credentials and state, before you rely on it
rightsize probe
```

`doctor` exists because of a real failure: a band 1 ladder entry named an
OpenRouter model the provider had retired, and routing kept picking it, because
nothing checked a model id against the catalogue until a worker tried it and
failed. It also catches a stale catalogue, a missing key, reservations nobody
released, and a daily refresh that ran without credentials.

Keys are optional to start: without them you get fewer providers and a crude
heuristic in place of the judgment, and the output says so. See
[docs/KEYS.md](docs/KEYS.md) for where each one comes from, what it costs, and
what you lose without it. The short version: `TYPESAFE_API_KEY` is the one that
matters, at roughly three cents per thousand routing decisions.

## Use

```bash
# Is anything wrong before I rely on this?
rightsize doctor

# What is left, everywhere, in a unit you can think in.
rightsize probe

# Decide one dispatch.
rightsize route --task "add a rate limit to the signup endpoint"
rightsize route --spec task.md --orca            # print the launch commands
rightsize route --spec task.md --launcher shell  # a plain CLI invocation
rightsize route --spec task.md --json            # for scripts; exit 1 if blocked

# Decide a whole fan-out, spread across plans, in waves.
rightsize plan --dir specs/ --launcher orca      # a file per task
rightsize plan --specs tasks.txt                 # or one task per line

# It came back and the work does not hold up: judge again, knowing that.
rightsize rerun --spec task.md --previous opencode:deepseek-v4.1-flash \
  --because "changed the response shape and could not make the tests pass"

# Tell it how dispatches went.
rightsize report --from-orca                     # release everything that has settled
rightsize report opencode --done                 # or release one by hand
rightsize report opencode --quota-error          # skip that plan until its bucket resets

# Did the workers run what was picked, and did it get anywhere?
rightsize audit

# What does a dispatch actually cost? Measure it instead of guessing.
rightsize calibrate                              # --apply writes it into config.json

# What the catalogues offer, and what got cheaper overnight.
rightsize models
rightsize deals
```

## What is left, in a unit you can think in

Every plan meters something different: percent of a rolling week, dollars of
credit, requests a day, tokens against a declared budget. Those cannot be
compared as they stand, and none of them is the question being asked. Once a
dispatch has a measured cost they all convert into the same one:

```
opencode    ok       usable 8 pts     binding weekly    resets 18h 30m
                     about 23 band 1, 7 band 3 dispatches left before the reserve
    rolling              used 17%     live
    weekly               used 77%     live
    monthly              used 38%     live
                         3.7x over pace, projects to 369% by reset with 90% of the window left
codex       ok       usable 89 pts    binding primary-10080m    resets 6d 21h
                     about 74 band 1, 24 band 3 dispatches left before the reserve
```

## The policy, in six rules

1. **Band from the judgment.** Mechanical and ordinary implementation sit in
   band 1. Design, diagnosis and anything touching auth, money, migrations or
   published output start at band 3. A large blast radius raises the band.
2. **Eligibility is a filter, not an opinion.** A provider qualifies when its
   binding bucket has headroom above its configured reserve, and can cover this
   dispatch's estimated cost.
3. **Spend the bucket that expires first, for cheap work only.** Capacity that
   resets in three hours is about to be thrown away. But the way to waste it is
   to spend it on the most expensive rung, so **band 3 goes to the roomiest
   plan** instead, which would otherwise sit idle.
4. **Pace every window, not just the binding one.** The windows are nested:
   every token against the weekly is also against the monthly, so a weekly that
   looks cheap to empty can be what exhausts the month. A window on course to
   pass 100 per cent takes its provider out of the cheap bands. Whole-window
   pace catches the average; a burn rate measured between two readings catches
   the acceleration an average hides.
5. **Nothing is spent twice.** A decided dispatch holds its estimated cost
   until the work settles, so a fan-out cannot hand twenty workers the same
   untouched headroom.
6. **Fallback is code.** A quota error takes a provider out until its known
   reset; a full band waits for the next wave rather than being answered with a
   model that suits it worse.

Price is the burn multiplier on a percent-bucket subscription: a model at
`$3/M` input eats your weekly allowance twenty times faster than one at
`$0.15/M`. That is why band 1 is the default and escalation needs a reason.

[docs/POLICY.md](docs/POLICY.md) walks the whole decision, including the
arithmetic for pacing and the rule that keeps an estimate from masquerading as
spendable capacity.

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
not have will fail on any provider. Routed from their titles alone, three real
issues scored 0.29, 0.57 and 0.30; the same work, specified against the
repository's own records, scored 0.87.

Without `TYPESAFE_API_KEY` the tool falls back to a crude keyword heuristic and
says so in its output, so a route made without a judgment is never mistaken for
one made with it.

### The questions are measured, not assumed

`./eval_questions.py` runs each question against labelled fixtures and requires
a **margin**, not merely the right ordering. A question that never fires looks
exactly like a question with nothing to report.

The first version of `spec_complete` failed that check. Its criteria asked
whether "every file, name, expected behaviour and acceptance check needed is
stated", which a literal reader answers "no" for any real task:

| fixture | original wording | current wording |
| --- | --- | --- |
| "fix the invoices thing" | 0.02 | 0.12 |
| "make the change we discussed" | 0.02 | 0.07 |
| "add pagination to the invoices endpoint" | 0.07 | 0.61 |
| a fully specified task naming repo, file, params and tests | 0.11 | 0.76 |

Separation went from 0.09 to 0.49 with nothing changed but the wording.

## Effort, and changing your mind later

The band picks the model. **Effort** is the second dial on that model, for the
CLIs that take it, configured per band in `config.json`. Three things turn it
up without changing the model: a wide blast radius, a step that cannot be
undone, and a retry after an attempt that failed. A provider with no effort
setting gets no flag rather than a made-up one.

The first judgment is made from the brief alone. What the work turns into is
better evidence, and `rerun` uses it: it judges again with the outcome
attached, starts one band above the attempt that failed, raises effort, and
takes the model that just failed out of the running.

## Fanning out: ten agents, or a hundred

A quota reading says what has been billed, not what is about to be. Route a
hundred tasks inside one cache window and all hundred see the same untouched
headroom, so all hundred pick the same provider. That was measured, not
theorised: 100 tasks, 100 dispatches to one plan, `usable` unchanged
throughout.

```
$ rightsize plan --dir specs/ --launcher orca
   0  w1  band 1  opencode:deepseek-v4.1-flash                 add cursor pagination to GET /api/invoices
   2  w1  band 3  claude:claude-opus-5 (xhigh)        CONFIRM  rotate the Stripe webhook secret
   7  -   band 1  -                                   BLOCKED  make the change we discussed

waves (each one runs after the previous reports done)
  wave 1:  37 tasks  (openrouter x12, opencode x10, opencode_zen x8, codex x4, claude x3)
  wave 2:  37 tasks
  wave 3:  26 tasks
```

- **Judgments in parallel, allocation in sequence.** 100 tasks judge in about
  ten seconds and cost roughly three cents. Each is then placed against
  headroom the earlier ones have already spent.
- **Reservations.** A decided but unfinished dispatch holds an estimated cost.
  A hold taken before a launch (`plan --reserve`) expires on a short clock,
  because a plan may be printed and never run; a hold taken after a launch has
  actually run keeps the long one. `rightsize report --from-orca` releases
  everything the orchestrator says has settled, across every Run.
- **An in-flight cap per provider** (`max_inflight`). This is what spreads a
  fan-out; a quota debit alone would not, since a free provider has no quota to
  debit. Two coordinators cannot both take the last slot: the count is checked
  inside the same lock that writes the hold, and the loser routes again.
- **Waves.** When everything is full, remaining tasks wait rather than being
  sent to a model that suits them worse. A wave returns the in-flight slots but
  keeps every quota debit, so a plan runs out of capacity eventually instead of
  scheduling forever.

One rule deliberately inverts here. A single `route` never strands a task: a
full band drops a tier and then escalates. Inside a batch there is a next wave,
so `plan` holds the task instead.

## Check that the pick was used, and that it worked

For opencode the model is chosen inside the terminal, not by a launch flag, so
an orchestrator records the provider and a null model. That makes two very
different things look identical: the pick being applied, and the pick being
ignored while the worker runs whatever `~/.config/opencode/opencode.json`
names. Six real workers in a row ran the config default, and there was no way
to tell which had happened.

So the Orca launcher **binds the model at launch**, creating the terminal with
`opencode -m <model>` and attaching the worker to it, rather than printing the
model as a line for a human to run afterwards. (`OPENCODE_MODEL` does not work:
it is accepted and ignored.) And every routing decision is logged, so it can be
compared with what actually ran and what became of it:

```
$ rightsize audit
ran as picked     12m ago  band 1  add-cursor-pagination-get-412
                  deepseek-v4.1-flash, 58 messages
OBEYED BUT FAILED  2h ago  band 3  rotate-stripe-webhook-secret
                  ran claude-opus-5 over 41 messages and the worker failed
                  rightsize rerun --task '...' --previous claude:claude-opus-5 --because "..."
```

Decisions are joined to outcomes by the **brief**, which both sides share,
rather than by a worktree name that rightsize only suggests. A route made to
read the numbers is recorded as such, so it is never counted as a dispatch that
went missing. [docs/OUTCOMES.md](docs/OUTCOMES.md) inventories every signal
this can see, with a real value read for each, and states plainly what it still
cannot know: whether a different model would have done better.

## What a dispatch costs, measured

`dispatch_cost` was the one number in the policy with nothing behind it, and a
fan-out reserves capacity with it. It does not have to stay a guess, and the
measurement needs no history: a bucket's window already has a start, and the
sessions inside it are the denominator.

```
$ rightsize calibrate
opencode
  weekly window opened 6d 4h ago, 226 dispatches since
  measured 0.34 points per dispatch, config says 0.6
  -> set dispatch_cost.opencode to 0.34
```

A percentage point is not the same size in every window: one dispatch is 2.3
per cent of a five hour rolling allowance and 0.44 per cent of a weekly one, so
calibration uses the tightest window lasting a day or more and says which.
Claude publishes no percentage, so it reports tokens and suggests a
`weekly_token_budget` that leaves room above the reserve rather than one that
blocks Claude the moment it is set.

## Wire it into your agent

The decision is only worth having if it happens on every dispatch, not on the
ones you remember. `hooks/claude_pretooluse.py` is a working hook for Claude
Code Bash events:

```json
{"hooks": {
  "PreToolUse":  [{"matcher": "Bash", "hooks": [{"type": "command",
     "command": "python3 /path/to/rightsize/hooks/claude_pretooluse.py"}]}],
  "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
     "command": "python3 /path/to/rightsize/hooks/claude_pretooluse.py"}]}]}}
```

It routes on PreToolUse and injects the decision as context; it reserves on
PostToolUse, after the launch has actually run, because a command that was
drafted, refused or failed must not hold a plan. It recognises a worker started
from a task id as well as one carrying a brief, and stays silent on text that
merely contains a launch line, which is why it has its own test suite.

It is advisory by construction: it prints, it never blocks, and it exits 0 on
every failure path. A routing helper that can stop a dispatch is a routing
helper that can strand a run.

## Where the quota numbers come from

| Provider | Source | Quality |
| --- | --- | --- |
| OpenCode | `GET /zen/go/v1/usage`, rolling, weekly and monthly percent with reset times | exact, live |
| Codex | `rate_limits` in the newest rollout under any Codex home | exact when written, and only an interactive session writes it |
| Claude Code | tokens reconstructed from `~/.claude/projects/**/*.jsonl` | estimate; needs a budget in `config.json` before it counts as headroom |
| OpenRouter | `GET /api/v1/key` for the key's own limit and the live free-request counter | live |

The differences matter and the policy keeps them. An exact number can be spent
down to a thin reserve. An estimate cannot, so a provider whose headroom is
unknown is only ever used for work that has already earned an escalation.

**A reading has an age.** Codex writes its numbers only when it runs, so
between sessions the file ages while the real quota moves. Past six hours it is
treated as unknown, which means escalation-only rather than blocked, because an
old high reading is the dangerous direction: it looks authoritative and removes
a provider that may have reset. Codex homes are discovered on disk as well as
read from `CODEX_HOME`, since an orchestrator may give each account its own and
a launchd job inherits nothing.

Probes run in parallel and a reading is cached for 60 seconds, because a
dispatch takes minutes. `rightsize probe` and `route --fresh` always re-read.

## Configuration

Everything tunable is in `config.json`: reserves per provider, the ordered
candidate ladder per band, the review ladder, thresholds, effort per band,
dispatch costs, in-flight caps, cache and reservation lifetimes, and the
launcher templates. Edit that, not the code.

A project can pin its own rules without touching anyone's home directory: the
nearest `.rightsize.json` at or above the working directory is merged over the
packaged config, dicts merging key by key and a list replacing. Every command
names the overlay it used. That file can change the commands rightsize prints,
so treat it like a Makefile.

## Credentials

Read in this order, and never written into the repository: environment
variables; explicitly scoped [Infisical](https://infisical.com) when the repo is linked; and for
OpenCode the auth file its own CLI already maintains. Codex and Claude Code
need no credential here. Full instructions, costs and free-tier limits are in
[docs/KEYS.md](docs/KEYS.md).
Vault reads fetch only an allowlisted name. Linked projects must specify
`workspaceId`, `defaultEnvironment` and `rightsizePath`; an incomplete or failed
vault read does not silently switch to a native key. Doctor inspects credential
metadata without fetching secret values.

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
python3 hooks/test_hook.py   # what the dispatch hook does and does not match
./eval_questions.py          # the judgments, needs TYPESAFE_API_KEY, costs a fraction of a cent
```

Synthetic quota states asserted end to end, with no network and no dependency
on this machine's own quota. Two suites of them, `adversarial()` and
`adversarial_two()`, exist because workers were dispatched to attack the policy
and returned twelve cases where two rules met and produced an answer nobody
would defend. Every one is kept. A twelve-process case covers the state file,
which once lost eleven of twelve concurrent writes and corrupted itself; writes
are now atomic and serialised by a lock.

## Documentation

- [docs/POLICY.md](docs/POLICY.md): how a decision is made, rule by rule.
- [docs/KEYS.md](docs/KEYS.md): getting each API key, what it costs, what
  breaks without it.
- [docs/ADAPTERS.md](docs/ADAPTERS.md): adding a launcher (config), a caller
  (JSON), or a provider (one function).
- [docs/OUTCOMES.md](docs/OUTCOMES.md): every signal available about a
  dispatch, and what none of them can prove.
- [docs/STATE.md](docs/STATE.md): the state file under concurrent access.
- [docs/SIMPLIFY.md](docs/SIMPLIFY.md): which rules still earn their place.
- [CONTRIBUTING.md](CONTRIBUTING.md): the design rules, and the evidence a
  question change needs.
- [SECURITY.md](SECURITY.md): what this reads, writes and sends.

## Licence

MIT.

### Launch receipts and quota failures

`report --started` records the provider that actually launched. It never judges
or routes the task again:

```bash
rightsize report opencode --started --model glm-5.3 \
  --task 'Review input validation and preserve boundary behavior' \
  --dispatch ctx_actual --worktree /path/to/actual-worktree
```

Use the orchestrator dispatch ID, or a stable tool-call ID when none is returned.
Repeated receipts with the same retained ID do not take another hold. The Claude
Bash hook recognizes the full shipped multiline OpenCode launcher and sends this
receipt after a successful launch with explicit model information. Other clients
can send the same receipt; they do not inherit Claude hooks. A failed launch or
unknown model is not booked. Hooks remain advisory, so direct launches can still
bypass a recommendation. Cross-client admission enforcement is planned in
[#10](https://github.com/karoliang/rightsize/issues/10).

An explicit quota denial blocks every band and survives missing telemetry until
a fresh healthy reading for that window arrives. `report --quota-error` uses the
latest reset among known denied windows, or the bounded `--minutes` cooldown
when reset attribution is unknown, and invalidates the probe cache. `--clear`
clears the reported cooldown, not a live quota denial.

Audit now distinguishes unknown dispatch evidence, ambiguous session matches,
and sessions with zero recorded output. Unmatched OpenCode sessions are shown
separately instead of being hidden behind a claim that nothing launched. A
verified model or a nonzero token count is not proof that the work was accepted.

Architecture planning and the repair history live in
[#1](https://github.com/karoliang/rightsize/issues/1) and [docs/BACKLOG.md](docs/BACKLOG.md).

### Use the active coding agent's judgment

An agent that has already read the task and relevant skills can pass its judgment
instead of making a separate Jev call. This needs no new judge key:

```bash
rightsize route --spec examples/caller-task.md \
  --judgment examples/caller-judgment.json --json
```

The committed example is illustrative, not a request to edit this repository.
For your own task, write the following document using the active agent's actual
classification. Bind it to `hashlib.sha256(Path("task.md").read_text().encode("utf-8")).hexdigest()`:

```json
{
  "schema_version": 1,
  "actor": "active-agent",
  "task_sha256": "<SHA-256 of the exact routed task text>",
  "judgment": {
    "tier": "implementation",
    "size": 0.6,
    "second_opinion": 0.2,
    "spec_complete": 0.9,
    "destructive": 0.0
  }
}
```

`size` is 0-2; the other scores are 0-1. Tiers are `mechanical`,
`implementation`, `design`, `diagnosis`, `high_stakes`. Actor is a 1-80 character
identifier using letters, digits, `.`, `_`, `:`, `/`, `-`, starting alphanumeric.
The JSON is capped at 64 KiB; unknown/duplicate fields, nonfinite numbers, an
unsupported version or a changed task are rejected before routing. For `--spec`,
hash the text as Python `read_text()` reads it, including its trailing newline;
for `--task`, hash the exact argument. Actor is attribution, not authentication.

This skips only the judgment request. Native quota probes and the shell wrapper's
existing vault setup still apply; scoped credential discovery is tracked in #14.
Without `--judgment`, existing routing behavior is unchanged. The option currently
applies to `route`, not batch planning or retries. JSON consumers must inspect
`pick`, `blocked` and `quota`, as before; a valid JSON decision is not permission
to launch. This option does not load skills or execute a worker by itself.

See [ADR0001](docs/decisions/0001-local-task-router.md) for the staged architecture.

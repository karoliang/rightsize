# rightsize

Pick the subagent provider and model for a coding task, from live quota.

If you run coding agents across several plans at once (an OpenCode
subscription, Codex, Claude Code, OpenRouter's free tier), every dispatch is a
small decision: which plan has headroom, which bucket is about to reset and
expire unused, and how much model this particular task actually needs. This
answers that in about two seconds, with a stated reason.

The split it is built on: **quota is arithmetic, the task is a judgment.**
Headroom, reserves, burn rate and fallback are ordinary code, because the
numbers are already exact and a model asked to do subtraction can be wrong
about them. What kind of work a task is, and how much model it needs, goes to
a typed judgment. The model is never asked which provider to use, because
providers change every few months and the questions do not.

No dependencies. Python 3 standard library only.

## Install

```bash
git clone https://github.com/<you>/rightsize ~/Github/rightsize
ln -s ~/Github/rightsize/rightsize ~/.local/bin/rightsize
rightsize refresh
rightsize probe
```

## Use

```bash
# What is left, everywhere.
rightsize probe

# Decide one dispatch.
rightsize route --task "add a rate limit to the signup endpoint"
rightsize route --spec task.md --orca      # prints the worker-start command
rightsize route --spec task.md --json      # for scripts

# What the catalogues offer, and what got cheaper overnight.
rightsize models
rightsize deals
```

Example route:

```
judgment   jev (jev-1.13)
           tier=implementation size=0.83 second_opinion=0.21 spec_complete=0.94 destructive=0.03
dispatch   band 1 -> agent opencode, model deepseek-v4.1-flash
  why      tier implementation starts at band 1
  quota    opencode:deepseek-v4.1-flash chosen: its weekly bucket resets in 21h 50m
           with 9 points usable, so spend it before it expires
```

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
5. **Fallback is code.** A worker that comes back with a quota error marks its
   provider exhausted until the known reset and the same spec is re-dispatched
   to the next eligible one.

Price is the burn multiplier on a percent-bucket subscription: a model at
`$3/M` input eats your weekly allowance twenty times faster than one at
`$0.15/M`. That is why band 1 is the default and escalation needs a reason.

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

## Configuration

Everything tunable is in `config.json`: reserves per provider, the ordered
candidate ladder per band, the review ladder, and the thresholds. Edit that,
not the code.

Claude Code publishes no quota API, so `claude.weekly_token_budget` is `null`
by default and Claude stays escalation-only. Set a token budget to let it take
ordinary work.

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
own CLIs already write.

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
python3 test_rightsize.py
```

Synthetic quota states and judgments, asserted end to end. No network, no
tokens spent. Every policy rule above has a case, including the two that are
easy to get wrong: unknown headroom must not be treated as free capacity, and
an exhausted ladder must escalate rather than return nothing.

## Licence

MIT.

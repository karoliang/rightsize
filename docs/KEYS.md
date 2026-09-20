# Getting the keys

rightsize reads credentials from the environment first, then from
[Infisical](https://infisical.com) when the repo is linked, then (for OpenCode
only) from the auth file the OpenCode CLI already maintains. Nothing is ever
written into the repository.

Every key here is optional in the sense that the tool still runs without it.
What you lose is stated per provider, because a routing decision made with less
information should be recognisable as one.

| Variable | Buys you | Without it |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | the judgment: what kind of work the task is | a keyword heuristic, labelled as such in every line of output |
| `OPENCODE_API_KEY` | live OpenCode Go quota, the main workhorse budget | OpenCode is `no-credential` and never offered |
| `OPENCODE_ZEN_API_KEY` | the Zen catalogue for `rightsize models` / `deals` | the free-model list goes stale |
| `OPENROUTER_API_KEY` | OpenRouter credit and its catalogue | OpenRouter is `no-credential`, its free tier unreachable |
| (none) | Codex and Claude Code quota | nothing: both are read from files their own CLIs write |

## TypeSafe (Jev), the one that matters most

Jev answers the five questions that decide the band. It is a System One model:
it returns typed judgments and probabilities rather than text, in one round
trip, which is why routing can afford to ask before every dispatch.

1. Sign in at [console.typesafe.ai](https://console.typesafe.ai).
2. Create a key at [console.typesafe.ai/keys](https://console.typesafe.ai/keys).
3. Export it as `TYPESAFE_API_KEY`.

```bash
export TYPESAFE_API_KEY=...
rightsize route --task "add a rate limit to the signup endpoint"
# the first line must read: judgment   jev (jev-1.13.0)
```

Cost, from the published rate of **$0.042 per million input tokens, output
free**: a route sends roughly 800 input tokens (the task plus five question
definitions), so a thousand routing decisions cost about **three cents**. The
request cap is 64k tokens total, 32k for the state plus the longest single
question, which no task spec here comes close to.

Try the questions before you wire anything: the
[playground](https://console.typesafe.ai/playground) takes the same state and
criteria that live in `QUESTIONS` in `rightsize.py`.

Without the key, `heuristic()` runs a regex table over the task text and every
output line says `heuristic (no TYPESAFE_API_KEY)`. It is deliberately crude.
It exists so the tool still routes on a fresh machine, not because it is a
substitute.

## OpenCode (Go subscription, and the Zen catalogue)

The CLI already stores both keys, so the usual answer is: log in once and let
rightsize read what is there.

```bash
opencode auth login          # writes ~/.local/share/opencode/auth.json
opencode auth list           # opencode-go and opencode should both appear
rightsize probe              # opencode must not say no-credential
```

`auth.json` holds one entry per provider (`opencode-go` for the subscription,
`opencode` for Zen). rightsize reads them only when `OPENCODE_API_KEY` /
`OPENCODE_ZEN_API_KEY` are unset, so an explicit environment variable always
wins.

The Zen `-free` models are served **only through the OpenCode client**: a
direct `curl` to the API is refused with `OpenCode's free tier can only be used
from within OpenCode`, while `opencode run -m opencode/<model>-free` works. That
is why free models are dispatched by launching the CLI rather than by calling an
endpoint.

## OpenRouter

1. Create a key at
   [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys).
2. Export it as `OPENROUTER_API_KEY`.

The free tier is **50 requests a day**, or **1000 a day** once the account has
bought at least 10 USD of credit at any point. Set the number you actually have
in `config.json` under `openrouter.free_requests_per_day`; OpenRouter does not
publish a counter for it, so rightsize counts locally and only sees the requests
it is told about:

```bash
rightsize report openrouter --free-request   # after dispatching one
```

## Codex and Claude Code

Neither needs a credential here, and neither publishes a usage API.

- **Codex**: usage comes from the `rate_limits` block in the newest
  `~/.codex/sessions/**/rollout-*.jsonl`. It is exact, but only written while
  Codex runs, so it is a lower bound: run Codex once to refresh it.
- **Claude Code**: usage is reconstructed from token counts in
  `~/.claude/projects/**/*.jsonl` (cache reads excluded deliberately; counting
  them overstates usage several times over). There is no published budget to
  divide by, so Claude stays escalation-only until you set
  `claude.weekly_token_budget` in `config.json`. Run `rightsize probe` to see
  the raw token counts and pick a number from them.

## Storing them

Environment is simplest. For something less manual:

```bash
infisical init                 # link this repo to a project, writes .infisical.json (gitignored)
rightsize probe                # the wrapper exports the project's secrets for this run only
```

The `rightsize` wrapper exports secrets into its own shell rather than running
under `infisical run`, so the exit code stays rightsize's own and a relative
`--spec` path still resolves against your working directory. Anything already
in the environment wins over the stored secret.

Whatever you use, keep keys out of the repository. `infisical scan` over the
working tree and the full history is part of the release check; see
[SECURITY.md](../SECURITY.md).

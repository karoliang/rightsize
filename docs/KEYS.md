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

## Optional task judgment: caller or TypeSafe (Jev)

The active coding agent can supply `route --judgment FILE`, avoiding a separate
judge key and model call. See the task-bound JSON example in README. Existing
quota authentication still applies. Without this option, Jev answers the five
questions that decide the band. It is a System One model:
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

What the subscription publishes is percentages, nothing more:

```bash
curl -s -H "Authorization: Bearer $OPENCODE_API_KEY" https://opencode.ai/zen/go/v1/usage
```

```json
{"usage": {
  "rolling": {"status": "ok", "percent": 13, "resetsAt": "2026-09-20T05:54:11.460Z"},
  "weekly":  {"status": "ok", "percent": 76, "resetsAt": "2026-09-21T00:00:00.000Z"},
  "monthly": {"status": "ok", "percent": 38, "resetsAt": "2026-10-17T03:24:16.000Z"}}}
```

There are no token counts, no request counts and no dollar figures, so
"remaining" for OpenCode can only ever mean percentage points on the binding
bucket. That is why the whole policy is expressed in percentage points, and why
`dispatch_cost` has to be an estimate in the same unit: the provider gives
nothing finer to divide.

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
bought at least 10 USD of credit at any point.

OpenRouter does publish that counter, contrary to what this file said until
2026-09-20, and rightsize now reads it rather than keeping a local tally:

```bash
curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" https://openrouter.ai/api/v1/key
```

```jsonc
{"data": {
  "limit": 0.01,              // this KEY's spend cap in USD, null when unset
  "limit_remaining": 0.01,    // what is left of it
  "usage_daily": 0,           // spend, not requests
  "is_free_tier": false,      // false once the account has bought credit
  "free_model_daily_requests": {"used": 0, "limit": 1000, "remaining": 1000}
}}
```

`free_model_daily_requests` is the number that matters for `:free` models, and
it is authoritative: a local count only ever sees the dispatches it was told
about and misses everything else using the same key. `/api/v1/credits` gives the
account balance (`total_credits` minus `total_usage`) when a key carries no
limit of its own.

Two traps worth checking on your own key:

- **A key can have its own spend cap.** This machine's read `limit: 0.01`, so
  paid OpenRouter models were capped at one cent regardless of a healthy account
  balance. Raise it at [openrouter.ai/settings/keys](https://openrouter.ai/settings/keys).
- **Free models will not take an agent-sized request.** Verified 2026-09-20: a
  direct API call to `deepseek/deepseek-v4-flash-0731:free` answers fine, but
  dispatching the same model through the `opencode` CLI returns
  `413 Request too large`, because an agent sends a system prompt plus tool
  definitions. That is why OpenRouter is probed but is not on a worker ladder
  by default. `rightsize report openrouter --free-request` remains as a manual
  fallback for a caller that spends free requests some other way.

## Codex and Claude Code

Neither needs a credential here, and neither publishes a usage API.

- **Codex**: usage comes from the `rate_limits` block in the newest
  `sessions/**/rollout-*.jsonl` under any Codex home. There is usually more
  than one: Orca gives each Codex account its own `CODEX_HOME` under
  `~/Library/Application Support/orca/codex-accounts/<id>/home`, so a session
  started from an Orca terminal writes there and leaves `~/.codex` untouched.
  rightsize searches `CODEX_HOME`, `ORCA_CODEX_HOME`, `~/.codex` and every
  discovered Orca account home, and the newest rollout wins. Discovery matters
  as much as the environment variables: a launchd job inherits `CODEX_HOME`
  from nobody, so an environment-only lookup reads correctly from an Orca
  terminal and wrongly from the timer. It is exact when written, but only an
  **interactive** Codex session writes it. Verified 2026-09-20: `codex exec`
  runs fine and writes no rollout, and the numbers are not in Codex's sqlite
  stores either. Past `staleness_seconds` (6h) rightsize stops trusting the
  file and treats Codex as unknown, which means escalation-only rather than
  blocked; `rightsize doctor` says so and `rightsize probe` prints the age.
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
# Set defaultEnvironment and rightsizePath explicitly in .infisical.json.
rightsize probe                # reads only the named keys needed by its probes
```

The link must contain `workspaceId`, `defaultEnvironment`, and `rightsizePath`
(for example `/` for a single deployable). Existing links need the explicit
path added; missing environment or path is never guessed. The CLI fetches each
allowlisted name with imports, expansion and personal-secret overrides disabled.
It never bulk-exports secrets or evaluates their contents as shell code.
Existing environment values still win. A linked vault failure returns no key,
without falling through to a potentially different native account. Unlinked
OpenCode installations retain their native auth-file fallback.

No login or vault contents are changed by this migration. To roll back, restore
the prior code while leaving link metadata and native credentials intact; the
additional `rightsizePath` metadata is ignored by the prior entry point.

Whatever you use, keep keys out of the repository. `infisical scan` over the
working tree and the full history is part of the release check; see
[SECURITY.md](../SECURITY.md).

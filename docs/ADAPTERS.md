# Adapters: launchers, providers, callers

rightsize was built against one orchestrator ([Orca](https://github.com/stablyai/orca))
and one set of plans. Neither is baked in. There are three seams, in increasing
order of effort.

## 1. Launchers: turning a decision into a command (config only)

A launcher is a set of command templates. `config.json`:

```json
"launchers": {
  "orca": {
    "default": "orca orchestration worker-start --spec {spec} --worktree current --agent {agent} --json\n# then inside that terminal: opencode -m {model_ref}",
    "codex": "orca orchestration worker-start --spec {spec} --worktree current --agent codex --json --model {model} --effort {effort}"
  },
  "shell": {
    "default": "opencode run -m {model_ref} {spec}",
    "codex": "codex exec --model {model} -c model_reasoning_effort={effort} {spec}",
    "claude": "claude -p --model {model} {spec}"
  }
}
```

Lookup is by provider, with `default` as the fallback. Available fields:

| field | example | notes |
| --- | --- | --- |
| `{provider}` | `opencode` | the plan the quota came from |
| `{agent}` | `opencode` | the CLI to launch, from `agents` in config |
| `{model}` | `deepseek-v4.1-flash` | as the ladder spells it |
| `{model_ref}` | `opencode-go/deepseek-v4.1-flash` | with the provider prefix a CLI expects |
| `{effort}` | `low` | `medium` when the candidate names none |
| `{band}` | `1` | |
| `{spec}` | `"$(cat task.md)"` | already quoted, or `"<task>"` |
| `{spec_path}` | `task.md` | bare path |

Then:

```bash
rightsize route --spec task.md --launcher shell
```

Adding a launcher for Aider, Cursor, a Makefile, a CI job or your own dispatcher
is a new key in that object. No code change, no fork.

## 2. Callers: the JSON contract

`rightsize route --json` is the stable interface. Everything the human output
shows is in it, and the fields below will not be removed without a major
version:

```jsonc
{
  "blocked": null,                    // string when the brief is not self-contained
  "band": 1,
  "judgment": {                       // "source" says jev or heuristic
    "tier": "implementation", "size": 0.82, "second_opinion": 0.68,
    "spec_complete": 0.58, "destructive": 0.10, "source": "jev (jev-1.13.0)"
  },
  "pick":   {"provider": "opencode", "model": "deepseek-v4.1-flash", "effort": null},
  "agent":  "opencode",
  "review": {"provider": "openrouter", "model": "deepseek/deepseek-chat-v3.1:free", "effort": null},
  "confirm_first": false,
  "reasons": ["tier implementation starts at band 1"],
  "notes":   ["opencode:deepseek-v4.1-flash chosen: its weekly bucket resets in 21h ..."],
  "quota":   {"opencode": {"usable": 9.0, "bucket": "weekly", "resets_in": "21h 2m",
                           "eligible": true, "blocked": null}}
}
```

Exit codes: `0` decided, `1` blocked or nothing eligible, `2` bad usage.

`pick` is `null` when every provider is blocked. Always check `blocked` before
dispatching: a low `spec_complete` is a problem with the brief, and no provider
fixes it.

Feed outcomes back so rule 5 can fire:

```bash
rightsize report <provider> --quota-error     # skip it until its bucket resets
rightsize report <provider> --clear           # lift the mark early
rightsize report openrouter --free-request    # count one free-tier request
```

### Batch: `rightsize plan --json`

For a fan-out, route the whole set in one call rather than looping `route`:
one probe instead of N, judgments in parallel, and allocation that knows what
the earlier tasks in the same batch already took.

```bash
rightsize plan --specs tasks.txt --reserve --launcher orca
rightsize plan --dir specs/ --glob '*.md' --json
cat tasks.txt | rightsize plan --concurrency 16
```

Each task in the result carries `index`, `spec`, `wave`, `points` and the same
`decision` object a single route returns. `waves` groups them: **wave 2 is
dispatched after wave 1 reports done**, not immediately. `blocked` lists tasks
whose brief is not self-contained (fix the brief), `unplaced` lists tasks the
plans genuinely cannot afford (wait for a reset, or add a provider).

Exit code is 1 when anything is blocked or unplaced, so a script can stop and
look.

### Telling it a dispatch finished

Capacity held by a reservation comes back three ways: explicitly, by expiry, or
when the plan resets.

```bash
rightsize report opencode --done          # release the oldest reservation
rightsize report opencode --done --id 1a2b3c
rightsize report opencode --quota-error   # it failed on quota: skip it until reset
```

An integration that reserves should release. One that cannot (a fire-and-forget
launcher) can rely on `reservation_ttl_seconds`, at the cost of looking busier
than it is until the TTL passes.

### Example: a pre-dispatch hook

`hooks/claude_pretooluse.py` is a working example for Claude Code. It watches
Bash commands that start a worker, extracts the task from `--spec` / `--prompt`
(including `"$(cat file)"`), calls `route --json`, and injects the decision as
context. It is advisory by construction: it prints, it never blocks, and it
exits 0 on every failure path. A routing helper that can stop a dispatch is a
routing helper that can strand a run.

Register it alongside whatever else owns the event:

```json
{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
  {"type": "command", "command": "python3 /path/to/rightsize/hooks/claude_pretooluse.py", "timeout": 25}
]}]}}
```

The same shape works for any orchestrator with a pre-dispatch hook. Ports are
welcome; keep them in `hooks/`, keep them advisory.

## 3. Providers: a new plan to route over (code)

A provider is one function returning one dict:

```python
def probe_myplan() -> dict:
    return {
        "name": "myplan",
        "status": "ok",                  # or "no-credential", or "error: ..."
        "buckets": [{
            "id": "weekly",              # your name for the window
            "percent": 42.0,             # percent USED, or None when unknown
            "resets_at": 1789816245,     # epoch seconds, or None
            "source": "live",            # live | stale-lower-bound | computed | local-count
        }],
    }
```

Then register it in `probe_all`, give it a reserve in `config.reserves`, a CLI
in `config.agents`, a prefix in `config.model_prefixes` if its models need one,
and put at least one model on a band ladder.

Three rules a new probe must respect, because the policy above depends on them:

1. **`percent` is percent used, and `None` means unknown.** Never return 0 for
   "no idea". Unknown headroom is offered only for work that already earned an
   escalation.
2. **Say how good the number is** in `source`. An estimate that reads like a
   live number is the failure mode this whole design exists to avoid.
3. **`resets_at` is an absolute epoch**, not a duration. The pick sorts on it.

If the provider publishes no meter at all, return `{"free": True}` and no
buckets, the way the Zen free models do. Free candidates are eligible in every
band and sort last, since they cost nothing and are usually slower.

Add a case to `test_rightsize.py` with a synthetic probe for the new provider.
The tests run offline and spend nothing; a provider without one is a provider
nobody can refactor safely.

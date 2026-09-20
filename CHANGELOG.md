# Changelog

Dates are absolute and ISO. This project is pre-1.0: the JSON output of
`rightsize route` is the interface treated as stable, everything else may move.

## Unreleased

### Added

- `rightsize report <provider>` closes the loop rule 5 always described: a
  worker that dies on quota can now be reported, which marks that provider
  exhausted until its bucket's known reset and takes it out of the next route.
  `--clear` lifts it early, `--free-request` counts an OpenRouter free-tier call.
- Config-driven launchers. `--launcher <name>` renders a command from templates
  in `config.json`, with `orca` and `shell` shipped. Adding a launcher for
  another orchestrator is config, not code. `--orca` remains as shorthand.
- `hooks/claude_pretooluse.py`: a working Claude Code PreToolUse hook that
  routes automatically when a Bash command starts a worker, and injects the
  decision as context. Advisory: it never blocks and exits 0 on every failure.
- `docs/POLICY.md` (how a decision is made), `docs/KEYS.md` (getting each
  credential, including TypeSafe), `docs/ADAPTERS.md` (the three seams),
  `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, CI on 3.11 to 3.13.

- `rightsize plan` routes a whole fan-out: one probe, judgments in parallel,
  then allocation task by task against headroom the earlier ones already spent.
  Tasks that do not fit are scheduled into later waves rather than being
  downgraded or dropped. Reads `--specs file`, `--dir`, or stdin.
- In-flight reservations. A decided-but-unfinished dispatch holds
  `dispatch_cost[provider] * band` points, taken by `route --reserve` /
  `plan --reserve`, released by `rightsize report <provider> --done`, and
  expiring on their own after `reservation_ttl_seconds`. `max_inflight` caps how
  many dispatches may run on one provider at once. Without these, 100 dispatches
  inside one cache window all went to the same provider: measured, not
  theorised.

- Effort is now a per-provider, per-band setting (`config.effort`) that rises a
  level for a wide blast radius, an irreversible step, or a retry. Providers
  without an effort knob get no flag instead of a default one, and the Orca and
  shell launchers pass `--effort` where it applies.
- `rightsize rerun --because "<what happened>"` reroutes a task that has already
  been tried: it judges again with the outcome attached, starts one band above
  the failed attempt, raises effort, and excludes the model that failed.
- `hooks/test_hook.py`, because the hook's matching was wrong twice in the
  permissive direction: a config file full of launcher templates and a heredoc
  writing documentation both read as dispatches. Heredoc bodies are now
  stripped, a launch must sit at the start of a command segment, text-producing
  commands (echo, grep, git commit) are skipped, and a placeholder like
  `<self-contained task>` is not treated as a brief.

- `rightsize doctor`: a preflight over config, catalogue, credentials and state.
  Every check is there because something was wrong once, the first being a
  ladder entry that named a retired model id.

- Repo-local config: the nearest `.rightsize.json` at or above the working
  directory is merged over the packaged config, so a project can pin reserves,
  ladders or launchers without editing one user's home directory. Every command
  names the overlay it used.
- A second Orca launcher, `orca-current`, for the case where sharing the
  coordinator's checkout is deliberate.

- `rightsize calibrate` measures `dispatch_cost` instead of guessing it, from
  the percentage a bucket has burned in its current window divided by the
  sessions Orca recorded inside that window. `--apply` writes the result. For
  Claude, which publishes no percentage, it suggests a `weekly_token_budget`
  from rightsize's own transcript scan. Measured here: 0.34 points per
  opencode dispatch against a configured guess of 0.6.

- `rightsize audit` compares every recommendation against what opencode's own
  database says actually ran, and names reservations holding capacity with
  nothing running. Routing now logs its decisions (capped at 200) so there is
  something to compare against.
- The Orca launcher binds the model at launch, creating the terminal with
  `opencode -m <model>` and attaching the worker to it, instead of printing the
  model as a line for a human to run afterwards.

- The expensive rung now goes to the roomiest plan instead of the
  soonest-expiring one. Expiring capacity is worth more spent on cheap work,
  and a band 3 dispatch usually has a roomier plan available. A candidate that
  cannot cover the dispatch's estimated cost is passed over entirely.

### Changed

- Routing is about three times faster. Probes run in parallel instead of
  serially, and a reading is cached for `cache_seconds` (60 by default), so a
  warm route is roughly 2 seconds against 6 before.
- The Claude transcript scan, the slowest read in the tool, no longer runs
  during routing. With no budget set its percentage is `None` whatever the count
  says, so only `rightsize probe` (where a human is choosing a budget from the
  number) asks for it.
- A cached reading no longer overwrites the burn-rate baseline with a copy of
  itself, which would have flattened the rate to zero.

- Inside a batch, a task whose band is full now waits for the next wave instead
  of falling to a cheaper band or escalating to band 3. A single `route` still
  escalates rather than stranding one task; the two cases want opposite answers,
  and a fan-out that escalates is wrong a hundred times over.

### Fixed

- An opencode worker launched with `--agent opencode` runs whatever
  `~/.config/opencode/opencode.json` names, because that flag takes no model and
  `OPENCODE_MODEL` is accepted and ignored (verified). The model was printed as
  a follow-up line for a human to run inside the terminal, so skipping it was
  silent and indistinguishable from applying it: six consecutive real workers
  ran the config default. The launcher now binds the model when the terminal is
  created, verified end to end on a fresh worktree.

- Codex usage was read only from `~/.codex`, but Codex does not always write
  there: Orca gives each account its own `CODEX_HOME`, so every session started
  from an Orca terminal wrote where rightsize could not see. The newest visible
  rollout was 20 hours old and recorded 93 per cent of a weekly bucket on a plan
  that had since been replaced, while the live rollout sat at 0 per cent. The
  staleness guard behaved correctly on that bad input, reporting unknown rather
  than trusting it, which is why the failure was silent: Codex was never picked,
  and nothing errored. Every Codex home is now searched, including Orca's
  per-account homes discovered on disk rather than only read from the
  environment, because a launchd job inherits `CODEX_HOME` from nobody.

- The Orca templates printed `--worktree new-child` without `--name`, which
  Orca rejects with `invalid_argument: New worktrees require --name`, so the
  command was unrunnable as printed the moment `new-child` became the default.
  A name is now derived from the task (an issue number kept, filler dropped,
  unique within a batch), which also puts something recognisable on the branch
  list.
- `worker-start` fails with `consumer_fenced` when no Run is bound, and the
  printed line assumed one existed. The templates now carry the `run-create`
  prerequisite as a comment above the command, conditional on
  `orca orchestration run-current` showing none.

- Every shipped Orca launcher used `--worktree current`, which puts the worker
  in the main checkout, sharing the coordinator's tree and branch: it can write
  to `main` mid-merge, and cleanup can never find it because there is no
  worktree to remove. One such worker ran in a main checkout for 29 hours after
  its run ended. The `orca` launcher now uses `--worktree new-child` for every
  provider, and a test fails if `--worktree current` reappears anywhere except
  the explicitly opt-in `orca-current`.

- A stale Codex reading was treated as current, and hard-blocked the provider.
  The number is only written when Codex runs, so a 19-hour-old 93 per cent kept
  Codex out of every ladder long after its quota had moved on, with nothing in
  the output saying the reading was from yesterday. File-sourced buckets now
  carry `age_seconds`; past `staleness_seconds` the percentage becomes unknown
  (escalation-only, not blocked), and a window whose reset has passed counts as
  empty with its reset rolled forward.
- `rightsize probe` now warms the cache the router reads, so looking at the
  numbers and then routing no longer probes twice.

- OpenRouter's free-tier counter was kept locally and guessed from config, while
  OpenRouter publishes it: `free_model_daily_requests` on `/api/v1/key` gives
  used, limit and remaining. A local tally only ever saw the dispatches it was
  told about. Credit now reads the key's own spend limit when it has one, and
  falls back to the account balance from `/api/v1/credits`. `probe` prints the
  real units (requests left, USD left) instead of only a percentage.
- OpenRouter is no longer on a worker ladder. Its free models answer a direct
  API call but return `413 Request too large` when dispatched through the
  `opencode` CLI, because an agent sends a system prompt and tool definitions.
  It is still probed, and the config says what would have to change to put it
  back.

- Band 1 and the review ladder named `openrouter:deepseek/deepseek-chat-v3.1:free`,
  which does not exist in OpenRouter's 447-model catalogue. Routing picked it
  constantly, since its daily bucket resets soonest, so every OpenRouter
  dispatch would have failed on an unknown model. Replaced with
  `deepseek/deepseek-v4-flash-0731:free`, verified against the live catalogue,
  and `rightsize doctor` now fails on this class of drift.
- The launchd refresh job ran with launchd's default PATH, which has no
  Homebrew, so `command -v infisical` failed and the daily refresh ran with no
  credentials: OpenRouter's catalogue came back empty and the probe logged
  `no-credential` every night. The plist now sets PATH.
- `plan --launcher` printed `--spec "<task>"` for every task instead of the
  brief, so the commands it produced were not runnable. Batch tasks come from
  lines rather than files, so the text is now quoted onto the command line.

- `test_rightsize.py` pointed at the real `~/.local/state/rightsize/state.json`,
  so results depended on the machine's live quota snapshots. It now uses a temp
  file.

## 2026-09-20

First public release. Probing, eligibility, the five judgments, bands, the
expiry-first pick, catalogue refresh and deals, the daily launchd job.

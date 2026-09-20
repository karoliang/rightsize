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

- `test_rightsize.py` pointed at the real `~/.local/state/rightsize/state.json`,
  so results depended on the machine's live quota snapshots. It now uses a temp
  file.

## 2026-09-20

First public release. Probing, eligibility, the five judgments, bands, the
expiry-first pick, catalogue refresh and deals, the daily launchd job.

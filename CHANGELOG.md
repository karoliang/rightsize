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

### Fixed

- `test_rightsize.py` pointed at the real `~/.local/state/rightsize/state.json`,
  so results depended on the machine's live quota snapshots. It now uses a temp
  file.

## 2026-09-20

First public release. Probing, eligibility, the five judgments, bands, the
expiry-first pick, catalogue refresh and deals, the daily launchd job.

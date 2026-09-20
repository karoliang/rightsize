# Routing reliability review, 2026-09-21

The original investigation used private project sessions and quota readings.
Those observations are retained in the private Brain. This public report keeps
reproducible mechanisms and repair evidence without publishing account usage,
private task descriptions, local paths or session identifiers.

## Reproduced defects

1. A provider reports a healthy rolling bucket, a `rate-limited` weekly bucket
   with no percentage, and a healthy monthly bucket. The weekly denial became
   unknown telemetry, so a higher-band task could select that provider.
2. The shipped multiline OpenCode launcher begins with worktree creation. The
   hook stopped at that setup command, found no task, and skipped accounting.
3. Post-launch accounting called the router again instead of recording the
   provider/model that actually launched. A changed recommendation could charge
   another provider.
4. Quota reporting chose the first future bucket reset. A healthy rolling
   reset could reopen a provider before its exhausted weekly window reset.
5. Audit inferred never launched from missing joins/receipts and did not expose
   unmatched sessions with zero output. A missing join is unknown evidence.

## Repairs

Tracked in GitHub [#2](https://github.com/karoliang/rightsize/issues/2),
[#3](https://github.com/karoliang/rightsize/issues/3),
[#4](https://github.com/karoliang/rightsize/issues/4),
[#5](https://github.com/karoliang/rightsize/issues/5), and
[#6](https://github.com/karoliang/rightsize/issues/6).

Explicit denials now veto every band and survive missing refreshes until a
healthy live reading. Cooldowns use denied windows and invalidate telemetry.
The hook finds the worker after setup and records actual launch receipts without
rerouting. Confirmed holds release against dispatch identity. Audit shows
unknown, ambiguous, zero-output and untracked evidence explicitly.

Offline regressions cover synthetic quota states, the shipped launcher through
the hook, exact/idempotent receipts, failures, dispatch release, renamed worktrees,
multiple attempts, JSON output and a read-only SQLite session fixture.

## Remaining boundary

The hook is advisory and belongs to the Claude Bash integration. These repairs
do not turn it into a cross-client launch gate, migrate active workers, or
establish model quality from token counts. Immutable decision-to-session joins,
automated failure recovery and architecture choices are planning work in
[#1](https://github.com/karoliang/rightsize/issues/1).

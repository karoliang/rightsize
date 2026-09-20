# Task routing boundary

Decision date: 2026-09-21. Planning ticket: #7. Baseline: `a168a85`.

## Chosen direction

Rightsize becomes a local task/model router with an explicit managed-dispatch
path. The active coding agent contributes task judgment and relevant context;
Rightsize validates it, applies deterministic quota/capability policy, and binds
the selected account/model to a launch receipt. Native tools retain execution,
authentication, tool calls and interactive permissions.

| Option | Strength | Cost / reason not selected first |
| --- | --- | --- |
| Existing advisor only | Works with pasted commands; almost no execution ownership | Cannot enforce a quota veto at launch or reliably observe outcomes |
| Managed task dispatch plus advisor compatibility | Reuses native agents, accounts, streams and worktrees; enforces admission on its own path | Requires process/adaptor lifecycle and explicit receipts; cannot govern bypass launches |
| Universal request gateway | A common API can switch providers per request | Needs provider protocol/tool-stream translations, API credentials, billing scope and stateful retry semantics; subscription sign-ins are not interchangeable API keys |
| Full agent/runtime rebuild | Maximum control over skills and tool execution | Duplicates working runtimes, permissions, session recovery and authentication; no demonstrated need |

Choose managed task dispatch incrementally. Keep `route`/`plan` advisory and
compatible. Add a separately named managed launch command only after admission
and lifecycle contracts pass replay tests. A gateway is a future product option,
not the mechanism for fixing local task routing.

## Compatibility and ownership

| Client | Current supported path | Next adapter / boundary |
| --- | --- | --- |
| Codex CLI | Existing generated command; caller supplies judgment JSON | Native `exec` JSON events; pinned account home; session receipt |
| Claude Code | Existing Bash hook and explicit receipt API | Native print-mode events or orchestration receipt; do not depend on hooks from other clients |
| OpenCode CLI | Explicit provider/model terminal binding | Native JSON events; observed session identity and quota-denial mapping |
| Orca | Current launcher templates, worker/task reporting | Worktree/terminal/dispatch identity and cancellation reconciliation; adapter contract needs fake-process proof |
| Other coding agents / IDEs | Call the CLI JSON contract, then report launch/outcome | No claim of automatic integration; a client must implement that contract |
| API applications | No request forwarding today | Deferred; use existing provider SDKs until a separately justified API adapter exists |

A native runtime owns streaming, tool calls, permission decisions and native
session continuation. Rightsize consumes events, never rewrites tool-call IDs.
Cancellation must reach the owned process or orchestrator dispatch and be
confirmed before capacity is released. A provider switch creates a new attempt
from preserved artifacts and an explicit handoff; it does not transplant an
opaque conversation between runtimes.

## Scope and uncertainties

Local single-user operation first. No hosted multi-tenant gateway, billing
resale, OAuth-token export, global vault identity, vector database, or automatic
provider shopping. No automatic extra spending when subscriptions are full.
Windows support is not established: current state locks use `fcntl`. New adapter
claims require fixtures plus a supported-platform test, not a generated command.

Source: existing `rightsize.py`, `config.json`, hook and receipt regression suite;
external capabilities are recorded in [CREDENTIALS.md](CREDENTIALS.md).

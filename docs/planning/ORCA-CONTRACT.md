# Orca managed-dispatch integration gap

Inspected 2026-09-21, installed Orca 1.4.205. Dependency:
[#21](https://github.com/karoliang/rightsize/issues/21), under #14/#17/#20.

## Evidence

Read the version-matched `orca-cli` and `orchestration` guides, command schema,
worker-start help, bounded worker/terminal metadata, and the installed CLI's
`out/cli/handlers/orchestration/worker-launch-handler.js`.

The worker-start handler sends agent, model, effort, placement, task/run identity
and durable mutation-request context. It does not send an account reference.
The runtime advertises worker launch-preference, lifecycle-settlement and stop-
verdict capabilities. The inspected worker receipt includes:

- Exact run/task/dispatch and process-incarnation fields.
- `startOptions.launch.requested` and `.effective` agent/model/effort.
- Ownership, liveness, settlement and resource-cleanup evidence.

The inspected worker and terminal responses did not identify the native quota
account, native session or turn/message for the actual launch. The terminal
listing/show fields identify a PTY/agent surface, not its credential binding.
Account-list metadata describes managed/current accounts separately; it does not
join one of them to a prepared worker.

No worker was launched, mail consumed or lifecycle ownership changed. This is
evidence for the inspected interfaces/build, not a claim that every Orca backend
can never expose richer proof. The Brain's referenced local source checkout is
absent. The installed application was not modified.

## Contract needed before enabling managed Orca

1. Before inference, obtain a supported prepared worker bound to an explicit
   account reference, or resolve and verify the current account. Keep credentials
   native. Return native quota-account/session identity, host/runtime incarnation,
   and effective model/effort/permissions.
2. The same prepared context must consume the task. Account changes invalidate
   preparation. A caller launch key must be durable before side effects, with
   exact run/task/dispatch and native turn/message acknowledgement afterward.
3. Structured output, usage, failure and cancellation evidence must join that
   exact attempt. Cancellation acknowledgement, terminal liveness and resource
   release remain distinct. One consumer owns lifecycle messages.
4. Read-only recovery must resolve an uncertain start without replaying the
   prompt or trusting names. Older/unsupported runtimes fail before dispatch.

Reuse existing fields where they prove the contract. Do not invent a capability
name or assume an unadvertised parameter is accepted. Parent `CODEX_HOME`, a
global active-account setting and requested model arguments are insufficient
alone. A live bounded task plus mismatch/rotation/restart/unknown-start tests are
required before closing #21 or #17's Orca gate.

Rightsize keeps this integration gated. Native Codex, Claude and OpenCode Go
adapters remain separate supported paths. Managed admission also refuses a
provider for which no native execution adapter exists, even if its quota is
healthy or a custom ladder lists it. Advisory routing remains available.

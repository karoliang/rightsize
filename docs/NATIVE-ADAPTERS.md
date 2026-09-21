# Native execution adapters

Managed runtime support is incremental. Codex subscription execution is available;
Claude, OpenCode, Orca and vault-to-runtime credential delivery remain in #14/#17.
The 20-task paired pilot and promotion gates remain #18/#19. A successful smoke
test is not a quality or cost benchmark.

| Adapter | Current evidence |
| --- | --- |
| Codex app-server, ChatGPT native login | Installed protocol/schema inspected; real isolated read-only smoke completed and passed deterministic review; offline lifecycle and recovery fixtures |
| Claude Code | Native subscription identity hashes and Keychain-aware cache invalidation tested; real setup-only control handshake inspected without a task; execution adapter pending |
| OpenCode | Native CLI/event interface inventoried; execution and vault delivery pending |
| Orca | Legacy advisory commands exist; managed terminal/account binding pending |

## Claude integration evidence (execution pending)

Installed Claude Code 2.1.267 supports bidirectional `stream-json` with `-p`,
`--verbose`, explicit `--session-id`, `--permission-mode` and
`--permission-prompts none`. A real setup-only control request, without any user
task, returned `account`, `models`, `current_permission_mode`, `pid` and
`session_state`. Account metadata fields were `email`, `organization`,
`subscriptionType`, `apiProvider`; model entries expose `resolvedModel` and
supported effort levels. Requested `manual` permission mode returned `default`.
Native startup hooks ran during setup. Setup therefore is not a side-effect-free
quota probe, even though no inference request was sent.

The [official Python SDK control implementation](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)
uses `control_request` with a request ID and `request.subtype=initialize`, then
matches a `control_response`. Interrupt uses the same control envelope. The
[official headless documentation](https://code.claude.com/docs/en/headless)
documents structured init, result, retry and permission-denial events. SIGTERM
does not produce a completed turn; cancellation must use native terminal evidence.

Remaining proof before enabling execution: bind the setup account to the admitted
native status identity; resolve exact model and effort before inference; bind the
acknowledged user message/session to the launch key; verify cancellation and
read-only recovery. Do not replace missing proof with CLI argument assumptions or
an exit code. Subscription budget estimates remain distinct from native quota.

## Run an admitted Codex attempt

```sh
rightsize managed run --attempt ATTEMPT_ID --spec task.md --repo /path/to/repo
rightsize managed cancel --attempt ATTEMPT_ID
rightsize managed reconcile --attempt ATTEMPT_ID
rightsize managed review --attempt ATTEMPT_ID --accept --evidence test-report.txt
```

`run` checks the exact admitted spec hash and creates a detached Git worktree
from the source repository's committed HEAD. Uncommitted source edits are not
copied. The default sandbox is read-only; choose `--sandbox workspace-write`
explicitly for implementation work. The adapter requests native `on-request`
approval behavior and verifies the sandbox returned by Codex. Interactive tool
approval requests are declined; existing native rules still apply. There is no
permission-bypass flag. Returned commands/text are never executed by Rightsize.

The foreground owner holds one execution lock and consumes native events. A
second invocation returns existing state, not another launch. The default runtime
deadline is 300 seconds (`--timeout`, maximum 3600). A deadline or separate cancel
request sends native `turn/interrupt`; the lease is released as cancelled only
after the matching native interrupted outcome. If the stream is lost or malformed,
the hold remains reconciling. Child processes are closed/reaped on every path.

## Account, setup and launch proof

The adapter reuses the admitted native home without extracting OAuth credentials.
It checks native auth mode is ChatGPT, fresh included-usage permission is explicitly
true, no published bucket is denied, the quota account matches admission, and the
credential fingerprint has not changed. API-key billing is not a substitute for
subscription quota. Codex `ordinaryUsageAllowed=false` is a denial even with low
reported percentages; unknown permission cannot clear it.
An explicit quota-account identity is required for managed Codex. Before inference,
fresh headroom must still cover active commitments, the admission's conservative
external holds, and the stricter of its original/current reserve.

Thread setup is separate from inference. The native thread ID, returned model,
effort, OpenAI provider, working directory, sandbox and approval policy are checked
before `turn/start`. A prepared receipt is persisted before the inference request;
the launch key is supplied as `clientUserMessageId`. The returned turn ID becomes
the dispatch ID. A rejected/mismatched setup is confirmed no-launch; losing the
turn response is uncertain and never triggers a repeated inference request.

Recovery reads only the exact recorded native thread. With a missing turn receipt,
it requires a unique native user-message marker matching the launch key and the
expected model/effort. A matching completed/failed/interrupted native turn can
settle the hold; missing, ambiguous or active history leaves it unresolved.
Recovery neither starts a new turn nor transplants conversation history. It does
not compete with a running owner for messages.

## Output and acceptance

The ledger records first output, cumulative native token counters, output bytes,
typed failures and native completion. Token counters are retained as separate
categories; cached input is not added again to the native total. Native text is
written to a local mode-0600 `output.txt` artifact under the attempt's execution
directory. JSON command output reports its path and hash, not the full response.
Worktrees and artifacts are retained for review. They may contain private task
data; they are not automatically published or copied into a memory database.

Transport bounds are 1 MiB per event, 8 MiB per execution stream, and 2 MiB pending
input. Reads and writes are nonblocking with deadlines, including partial lines
and a native process that stops reading stdin. Native stderr and raw error text
are not persisted in routing evidence. Usage/auth/tool/runtime failures remain
distinct; quality rejection comes from review, not a text/error heuristic.

Completion is not acceptance. `review` records an explicit accept/reject decision
and the hash of a nonempty local evidence file (maximum 1 MiB). Rightsize does not
claim an arbitrary caller-provided review file proves correctness. The live smoke
used an exact expected-output check; coding-task pilots require actual test and
review criteria.

Selected context is delivered when admission and run both receive the same
`--context-manifest` and explicit `--context-root` approvals. Sources are rechecked
before launch, and the manifest hash is bound to the admitted intent. See
[context delivery](CONTEXT.md#managed-execution). Additional runtimes and Orca
recovery remain outstanding in #17.

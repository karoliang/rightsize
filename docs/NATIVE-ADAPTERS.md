# Native execution adapters

Managed runtime support is incremental. Codex subscription execution is available;
Claude, OpenCode, Orca and vault-to-runtime credential delivery remain in #14/#17.
The 20-task paired pilot and promotion gates remain #18/#19. A successful smoke
test is not a quality or cost benchmark.

| Adapter | Current evidence |
| --- | --- |
| Codex app-server, ChatGPT native login | Installed protocol/schema inspected; real isolated read-only smoke completed and passed deterministic review; offline lifecycle and recovery fixtures |
| Claude Code | Native authentication/status and headless event interfaces inventoried; execution adapter pending |
| OpenCode | Native CLI/event interface inventoried; execution and vault delivery pending |
| Orca | Legacy advisory commands exist; managed terminal/account binding pending |

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

Selected context-manifest delivery, additional runtimes and Orca recovery are
still outstanding in #17. The current native path requires `context_hash=none`;
the caller can already use bounded manifests when forming its judgment, and the
native runtime continues loading its own repository instructions.

# Native execution adapters

Managed runtime support is incremental. Codex, Claude and OpenCode Go adapters are available; live Claude
execution validation, Orca and remaining vault delivery proof remain in #14/#17. Offline replay is complete (#18); the
20-task paired pilot and promotion gates remain #19. A successful smoke
test is not a quality or cost benchmark.

| Adapter | Current evidence |
| --- | --- |
| Codex app-server, ChatGPT native login | Installed protocol/schema inspected; real isolated read-only smoke completed and passed deterministic review; offline lifecycle and recovery fixtures |
| Claude Code | Native identity/quota/setup verified; offline execution, cancellation and journal recovery tests pass; live task waited below weekly reserve, so execution remains unproven on the installed CLI |
| OpenCode Go | Reviewed native smoke, matching native cancellation, and GET-only history recovery for both outcomes verified; scoped-vault live proof remains |
| Orca | Legacy advisory commands exist; managed terminal/account binding pending |

## OpenCode Go execution and Orca preparation

OpenCode 1.18.31 exposes a native OpenAPI schema at `/doc`. Its session APIs
separate session creation, asynchronous prompt submission, exact message lookup
and abort. See the [native server contract](https://opencode.ai/docs/server/).
`native_opencode.Server` starts an owned server on loopback with an ephemeral port
and a random in-memory password, overriding inherited server username/password.
It never attaches to an arbitrary endpoint, follows redirects or modifies auth.
Startup is bounded to 15 seconds and 64 KiB; JSON requests are bounded to 2 MiB,
responses default to 4 MiB, and requests have an overall 15-second deadline,
including trickled headers/body. Native output and error bodies are not forwarded.
The process is reaped on startup failure and close. The managed execution adapter uses this transport for session/message lifecycle calls.

Six fake-child tests cover authentication, partial endpoint output, startup
failure/reaping, trickled responses, error redaction, redirects and byte limits.
A real health/schema-only check confirmed installed 1.18.31 and reaped the child;
no session or inference was requested. The installed schema exposes effective
provider source/key/options, session model/permissions and message parent/model
IDs. Managed execution binds credential equivalence to quota, then verifies native
session/message receipts and reads exact native history for recovery. Never publish
provider responses containing keys or replace native auth files to obtain proof.

Credential preparation now resolves the exact admitted Go binding with the
existing native/environment/scoped-vault resolver. Only the owned child receives
the selected key, through an environment reference in inline native configuration;
no auth/config file or parent environment is written. It enables only OpenCode Go
and disables sharing and auto-update for that child. Effective provider inspection
checks the resolved key, official Go endpoint, native model ID, transport package
and selected effort variant. Credential/endpoint overrides in model or variant
options, custom headers and additional connected providers are rejected.

The installed `/api/provider/opencode-go` route returned 404 despite appearing in
the advertised schema. The established `/provider` route works with only Go
enabled. A real explicitly selected native-account setup passed effective binding
and model verification, without creating a session or submitting a task. The
configured exact vault read returned no key; its path stopped without native
fallback. Vault delivery is covered by synthetic tests but not live-verified.
Managed admission and run now use these helpers; missing or rotated vault keys
stop before inference. Launch reads the exact key again after setup, outside
admission locks. A key-derived quota identity pools the same key across native
homes without persisting its value.
Native HTTP errors retain only their status code; error bodies remain private.


OpenCode execution creates an owned native session with exact model/variant,
permission rules and launch-key metadata before submitting the prompt. The
message ID derives from the durable launch key; its native user record must echo
model/variant before a dispatch receipt is accepted. Main and small-model defaults
are pinned to the selected Go model. Only this provider is enabled. Inference
submission is never repeated after an ambiguous acknowledgement.

Permissions are native tool rules, **not an OS sandbox**: read-only permits Read,
Glob and Grep equivalents; workspace-write additionally permits native file edits.
Shell, delegation, external-directory access and other tools remain denied. There
is no permission-bypass option. Repository work happens in an isolated worktree.

Polling verifies session/message/parent/model/variant identity, emits text once,
and records native counters once per assistant message. Native total is recorded
only when supplied for every message; no synthetic total combines possibly
overlapping categories. Tool failures are counted separately. Completion requires
a matching terminal assistant outcome and an idle session; compaction summaries
and an abort acknowledgement cannot settle a task. Recovery starts an owned native
server and reads only the exact recorded session/history, without resuming or
submitting a prompt. Missing, active or inconsistent evidence retains reservations.

Thirteen offline lifecycle tests cover this path, including CLI/worktree artifacts.
A real setup-only session returned the requested model, rules and launch metadata.
The first real smoke waited on an unattributed legacy denial. On 2026-09-21 the
user confirmed that record belonged to the existing native Go login. A fresh
same-context probe reported healthy rolling/weekly/monthly buckets (30/12/56%).
The denial was reconciled under the state lock, preserving a private byte-exact
backup and attribution/probe audit. Healthy quota alone was not used to infer
historical account ownership. The configured vault path was not changed.

A subsequent isolated read-only task returned the exact expected text and was
separately marked accepted. A one-second cancellation test ended with matching
native `cancelled` evidence. GET-only history recovery restored both outcomes
on private ledger copies with dispatch/outcome evidence removed, leaving the
production accepted/cancelled records unchanged. No prompt was resubmitted and
no active live attempts remained after these checks. This establishes these
installed-runtime paths, not pilot quality or scoped-vault delivery.

Recovery exposed a native normalization: the creation response omitted an
unrequested variant, but the persisted session later added `variant: "default"`.
The adapter now accepts that exact normalization only when no effort was
requested. Other session fields and user/assistant message identity stay exact;
an explicit effort change remains unresolved. A regression test failed before
the fix and now passes, alongside the real history-recovery check.

The tiny successful task reported 32,678 input and 10 output tokens (native total
32,688). Pilot budgets must include native session overhead rather than treating
prompt bytes as all input. The cancelled run reported zero counters; this is
recorded native telemetry, not proof that the provider charged zero usage.

Orca's installed CLI advertises model/effort flags, durable request IDs and
run/task/dispatch identities, but no worker-start account selector. Account-list
metadata alone does not prove which account a new worker uses. Managed Orca
execution remains gated until account binding is proven; no worker was started
during this read-only inventory.

## Claude integration evidence

Installed Claude Code 2.1.267 supports bidirectional `stream-json` with `-p`,
`--verbose`, explicit `--session-id`, `--permission-mode` and
`--permission-prompts none`. A real setup-only control request, without any user
task, returned `account`, `models`, `current_permission_mode`, `pid` and
`session_state`. Account metadata fields were `email`, `organization`,
`subscriptionType`, `apiProvider`; model entries expose `resolvedModel` and
supported effort levels. Requested `manual` permission mode returned `default`.
Native startup hooks ran during ordinary setup. Quota discovery therefore uses
native `--safe-mode`, an isolated temporary directory, no tools and no session
persistence. This disables user/project customizations while retaining native
login and administrator policy. No inference request is sent. Discovery is a
native subprocess, not the strictly side-effect-free replay/shadow path.

The [official Python SDK control implementation](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/query.py)
uses `control_request` with a request ID and `request.subtype=initialize`, then
matches a `control_response`. Interrupt uses the same control envelope. The
[official headless documentation](https://code.claude.com/docs/en/headless)
documents structured init, result, retry and permission-denial events. SIGTERM
does not produce a completed turn; cancellation must use native terminal evidence.

Further setup-only verification found `get_settings.applied.model` and `.effort`
match explicit model/effort flags. `get_usage` with `skip_behaviors: true` returns
subscription windows without a transcript scan. Published Agent SDK 0.3.278's
`SDKControlGetUsageResponse` marks the `model_scoped` array as endpoint-answer
evidence and omits it for cached/unknown answers. Healthy data without that marker
is unavailable; explicit denial survives missing freshness. Required rolling/weekly
windows, additional utilization windows, model windows and active generic limits
are retained conservatively. Usage-credit enablement is separate from plan capacity.
The experimental control/schema can change: malformed or unsupported responses
never become fresh empty capacity, and the existing computed-budget fallback stays
labelled as an estimate when the control itself is unavailable.

Initialize reports organization display name, whereas auth status reports both
name and canonical ID. Rightsize compares a separate hashed setup signature and
rechecks native identity after the query; the canonical ID hash still owns quota
pooling. No raw identity or credential is returned by discovery. Installed 2.1.267
rejected `list_permission_rules`; do not rely on that newer SDK control yet.

Claude admission requires fresh native control quota, canonical and setup account
identity, and explicit `usage_credits_enabled=false`. Computed estimates cannot
admit a managed Claude task. Before sending the task, the adapter verifies account,
model, effort, native permission mode, fast mode off, no advisor/ultracode, unchanged
identity and sufficient remaining headroom. Native auth stays owned by Claude.

Execution uses a new session and a user-message UUID derived from the durable
launch key. Both native init and message acknowledgement must match; losing this
acknowledgement retains the hold. Assistant output must use the admitted model;
the terminal result must identify exactly the submitted task. Cancellation uses
native interrupt with queued cancellation and requires a matching terminal result.
An interrupt acknowledgement or process exit alone cannot release a hold.

For Claude, `--sandbox read-only` means native plan permissions with only Read,
Glob and Grep tools and an empty strict MCP configuration. It is **not an OS
sandbox**. Workspace-write requests native acceptEdits permissions. Native hooks
and configuration still apply; prompts for additional permission are denied.

Before settling the ledger, a matching terminal result is fsynced to a private,
hashed metadata journal. Recovery can replay that exact owned receipt and outcome
without inference. Missing/corrupt journals retain the hold; native conversation
history recovery after a lost stream is not implemented. This is a narrower
recovery guarantee than Codex's native history lookup.

Offline fake-process tests cover setup rejection, output deduplication, quota
failure, cancellation, lost streams, and journal recovery. The real bounded smoke
attempt waited below the configured weekly reserve before launch. No reserve was
lowered and no paid fallback was enabled. Live Claude execution and cancellation
remain validation gates; fake-process results do not prove installed behavior.

## Run an admitted native attempt

```sh
rightsize managed run --attempt ATTEMPT_ID --spec task.md --repo /path/to/repo
rightsize managed cancel --attempt ATTEMPT_ID
rightsize managed reconcile --attempt ATTEMPT_ID
rightsize managed review --attempt ATTEMPT_ID --accept --evidence test-report.txt
```

`run` checks the exact admitted spec hash and creates a detached Git worktree
from the source repository's committed HEAD. Uncommitted source edits are not
copied. For Codex, the default sandbox is read-only; choose `--sandbox workspace-write`
explicitly for implementation work. The adapter requests native `on-request`
approval behavior and verifies the sandbox returned by Codex. Interactive tool
approval requests are declined; existing native rules still apply. There is no
permission-bypass flag. Returned commands/text are never executed by Rightsize.

The foreground owner holds one execution lock and consumes native events. A
second invocation returns existing state, not another launch. The default runtime
deadline is 300 seconds (`--timeout`, maximum 3600). A deadline or separate cancel
request sends native interruption (`turn/interrupt` for Codex); the lease is released as cancelled only
after the matching native interrupted outcome. If the stream is lost or malformed,
the hold remains reconciling. Child processes are closed/reaped on every path.

## Codex account, setup and launch proof

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
categories. Codex input includes cached input, which is not added again to its
total. Claude cumulative `modelUsage` has disjoint uncached input, output, cache
read and cache creation counts; their sum is its total. Top-level Claude `usage`
is narrower and is not mixed into those counters. Native text is
written to a local mode-0600 `output.txt` artifact under the attempt's execution
directory. JSON command output reports its path and hash, not the full response.
Worktrees and artifacts are retained for review. They may contain private task
data; they are not automatically published or copied into a memory database.

Codex/Claude JSON-line transport bounds are 1 MiB per event, 8 MiB per execution
stream, and 2 MiB pending input. Reads and writes are nonblocking with deadlines, including partial lines
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

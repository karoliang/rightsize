# Closing the outcome loop

Design only, based on local reads on 2026-09-20 at approximately 05:26 UTC.
No routing policy or configuration changed. Read alongside [POLICY.md](POLICY.md).

**Recommendation:** attach an observed outcome to a particular routing decision
and dispatch, then feed a failed attempt into the existing `rerun` path. Start
with reported failure; show retries and expensive sessions as review signals.
The available data can measure execution and reported completion, but cannot
prove that a different model would have done better.

## Verified evidence

All four requested sources exist. SQLite was opened with `mode=ro`; JSON files
were read directly; both Orca list commands were executed. Values below are
snapshots, not fixtures or hypothetical output. Paths using `~` mean
`/Users/karo` on the inspected machine.

For compact references in the table:

- **DB:** `~/.local/share/opencode/opencode.db`.
- **OC usage:** `~/Library/Application Support/orca/orca-opencode-usage.json`.
- **CC usage:** `~/Library/Application Support/orca/orca-claude-usage.json`.
- **Decisions:** `~/.local/state/rightsize/state.json`, array `decisions`.
- **W:** `orca orchestration worker-list --json`, following every
  `result.page.nextCursor` with `--cursor` until `hasMore` is false.
- **T:** `orca orchestration task-list --run run_05508c5cc2e5 --json`.
- **T-current:** `orca orchestration task-list --run run_0c72cb9b3f31 --json`.
- **S:** OpenCode session `ses_f4413e89affeBkqSEWWh4LYy7U`.
- **B:** OpenCode session `ses_f42dc0af0ffeKUw4Hb2YkMMCME`.
- **C:** Claude session `0278aac7-8249-47db-87e5-1031f81a6d79`.

| Signal | Exact source and field / computation | Real value read |
| --- | --- | --- |
| Session identity and location | DB `session.id`, `directory` | S: `/Users/karo/Github/money.financial/auth-emails` |
| Session parent | DB `session.parent_id` | B: `NULL`; this field is not an Orca task ID |
| Session time bounds | DB `session.time_created`, `time_updated` (epoch milliseconds) | S: `1789859207013`, `1789859438919`; difference **231.906 seconds** |
| Message count and roles | DB `message`, grouped by `session_id`, `json_extract(data,'$.role')` | S: **48 assistant + 1 user = 49 messages** |
| Message time bounds | DB `message.time_created`; `data.time.completed` | S: first created `1789859207516`, last created `1789859433289`, latest completion `1789859438912` |
| Actual model per message | DB `message.data.providerID`, `.modelID`, assistant rows only | S: all 48 assistant rows use `opencode-go` / `deepseek-v4.1-flash` |
| Session model metadata | DB `session.model`, JSON encoded string | B: `{"id":"qwen3.8-flash","providerID":"opencode-go"}` |
| Assistant completion / finish reason | DB `message.data.time`, `.finish` | B message `msg_0bd23f53400181ReTBoG7lIk75`: created `1789879645492`, completed `1789879655059`, finish `tool-calls`; the next assistant message has no completion timestamp |
| Tool invocation and status | DB `part.data.type`, `.tool`, `.state.status`, `.state.time` | B part `prt_0bd2412cd0015kCZ471eR42DDO`: `tool`, `bash`, `completed`, start `1789879654781`, end `1789879654952` |
| Tool failure signal | DB `part.data.state.status` | Part `prt_08fbda49c001HPuzWFKz3iA2BJ`, session `ses_f7042add2ffeSF7OkeUNimbHEi`: tool `task`, status `error` |
| Step tokens / cost | DB `part.data` where `type='step-finish'` | B part `prt_0bd241a5b001pyZETW04YBex17`: input `6`, output `227`, cache write `39530`, cost `0.00801359`, reason `tool-calls` |
| Session tokens / cost | DB `session.cost`, `tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write` | S: cost `0.038843298000000005`, input `74079`, output `10967`, reasoning `17286`, cache read `3593216`, cache write `0` |
| Usage identity and model attribution | OC usage `sessions[].sessionId`, `.primaryModel`, `.hasMixedModels`, `.modelBreakdown` | S: `opencode-go/deepseek-v4.1-flash`, `hasMixedModels=false`, one model breakdown |
| Usage location attribution | OC usage `sessions[].primaryWorktreeId`, `.hasMixedLocations`, `.locationModelBreakdown` | S: `4047971e-91a9-4098-b2b1-5e41e9eb9d5b::/Users/karo/Github/money.financial/auth-emails`, `hasMixedLocations=false` |
| OpenCode usage tokens | OC usage `sessions[].totalInputTokens`, `.totalCachedInputTokens`, `.totalOutputTokens`, `.totalReasoningOutputTokens`, `.totalTokens` | S: `74079`, `74079`, `10967`, `17286`, `102332` respectively |
| OpenCode estimated dollar cost | OC usage `sessions[].estimatedCostUsd` | S: `0.038843298000000005` |
| Usage event count and timestamps | OC usage `sessions[].eventCount`, `.firstTimestamp`, `.lastTimestamp` | S: `1`; both timestamps `2026-09-19T23:10:38.919Z`, despite 49 DB messages over about 232 seconds |
| OpenCode usage freshness / coverage | OC usage `scanState.lastScanCompletedAt`, `.lastScanError`, length of `sessions` | `1789864333642` (`2026-09-20T00:32:13.642Z`), `null`, **295 sessions**; DB contained **355 sessions** |
| Claude identity, model and location | CC usage `sessions[].sessionId`, `.model`, `.lastCwd`, `.primaryWorktreeId`, `.locationBreakdown` | C: `claude-fable-5`, `/Users/karo/Github/money.financial`; breakdown has `turnCount=8011` for that worktree versus session-wide `8694` |
| Claude session timestamps and turn count | CC usage `sessions[].firstTimestamp`, `.lastTimestamp`, `.turnCount` | C: `2026-09-15T22:25:46.293Z` to `2026-09-20T00:32:08.741Z`, `8694` turns |
| Claude token categories | CC usage `sessions[].totalInputTokens`, `.totalOutputTokens`, `.totalCacheReadTokens`, `.totalCacheWriteTokens`, `.totalCacheWrite1hTokens` | C: `17382`, `4982395`, `3380673625`, `15857666`, `12807090` respectively |
| Claude usage freshness / coverage | CC usage `scanState.lastScanCompletedAt`, `.lastScanError`, length of `sessions` | `1789864333901` (`2026-09-20T00:32:13.901Z`), `null`, **50 sessions** |
| Dispatch outcome, distinct from terminal cleanup | W `workers[].workerState`, `.dispatchStatus`, `.terminalState`, `.projection.outcome` | `ctx_e01a278533e1`: `failed`, `failed`, `released`, `failed`; `ctx_f18b5ec642d4`: `succeeded`, `completed`, `released`, `succeeded` |
| A stopped worker is distinguishable | W `workers[].workerState`, `.projection.stage.detail` | `ctx_97d5f6916687`: `stopped`, `process_stopped`, although `.projection.outcome` also says `failed` |
| Task / attempt / location join keys | W `workers[].runId`, `.taskId`, `.dispatchId`, `.resource.worktreeId` | Current worker: `run_0c72cb9b3f31`, `task_372348bff1de`, `ctx_0e58dc54ab47`, worktree suffix `/Users/karo/Github/rightsize/outcome-loop` |
| Worker enumeration coverage | W `result.scope`, `.page`, counts across all pages | Scope `all`; first page `limit=100`, `total=249`, `hasMore=true`; all pages: 229 succeeded, 9 failed, 8 stopped, 3 ready |
| Distinct dispatches per task | W count distinct `dispatchId` grouped by `(runId,taskId)` | All **249** visible workers have distinct task keys; **zero observed tasks with multiple dispatches** |
| Task completion and reported outcome | T `tasks[].status`, JSON-decoded `.result.provenance`, `.result.outcome`, `.completed_at` | `task_ab017edd4985`: `failed`, `worker_report`, `failed`, `2026-09-17T10:17:15.270Z` |
| Reported reason / evidence pointer | T JSON-decoded `tasks[].result.subject`, `.reportPath` | Same task: `1440 reweighted; 3-line ceiling proven unreachable, optimum is 6`; `docs/reviews/compare-table-1440-grid-2026-09-17.md` |
| Full task brief and current dispatch | T-current `tasks[].spec`, `.dispatch_id`, `.status`, `.created_at` | `task_372348bff1de`: spec begins `In /Users/karo/Github/rightsize, design the missing outcome loop`; dispatch `ctx_0e58dc54ab47`, status `dispatched`, created `2026-09-20 05:23:56` |
| Recommended provider, model, band and effort | Decisions entry at `1789881830.883956` | `codex`, `gpt-6-astra`, band `3`, effort `high`, name `outcome-loop` |
| Shortened brief and decision time | Decisions same entry `.task`, `.at`; array length | Task begins `In /Users/karo/Github/rightsize, design the missing outcome loop`; **38 decisions** at inspection |

### Absences and discrepancies are part of the evidence

CC usage has **no cost field** in the inspected session or daily aggregate
records. Its cache-write fields do exist today; treating those fields as absent
would also be wrong. Do not derive Claude dollars from invented prices, or add
the 1-hour cache-write category to the other category without establishing their
relationship.

OC usage's S cache count is `74079`, while the matching DB session cache-read
counter is `3593216`. These counters are not interchangeable. Keep source-labelled
categories, use one source per metric, and never add message, step and session
totals together. The identical cost and token values in B's message and step
records demonstrate the double-counting risk.

The bare requested `orca orchestration task-list --json` returned
`ok=false`, code `run_required`, message `No Run is bound`. The explicit `--run`
commands above succeeded. In the same shell, bare `worker-list` returned *all*
Runs, not just this task's Run. Production collection must set scope explicitly
and follow worker pagination; neither an error nor the first 100 rows is an
empty history. Old completed task records inspected lack `dispatch_id`; W still
provides their task-to-dispatch relationship. Observed worker
`projection.provider` values can be null.

Both usage caches were about 4 hours 54 minutes old at inspection. They cannot
describe work started after the scan. Missing usage is unknown, never zero cost.
OC usage timestamps cannot supply session duration: S would incorrectly have
duration zero. DB time bounds measure an activity span, including waits and
idle time, not model thinking time.

## Linking evidence without manufacturing attempts

The existing `rightsize.py::log_decision` keeps only `at`, `provider`, `model`,
`effort`, `band`, suggested `name`, and a whitespace-normalized 120-character
`task` prefix, capped at 200 entries. It keeps no decision ID, repository path,
task ID, dispatch ID, session ID, launch confirmation, or retry relationship.
Two real `outcome-loop` decisions recommend different providers: first
`opencode:glm-5.3`, then `codex:gpt-6-astra`. They are recommendations, not proof
of two attempts. No Codex execution evidence was inspected in the specified
telemetry sources.

The current `audit` matches a suggested worktree basename and a loose timestamp
bound, then chooses the model group with most messages. That is useful for
diagnosing launch mismatches, but not a safe attribution rule for learning.
`brief_key` further shortens text to 80 characters for reservation release;
matching that prefix is not task identity.

Use these relationships:

1. Orca `(runId,taskId)` groups actual attempts; `dispatchId` identifies one
   attempt. Preserve each attempt's worker state even if the task eventually
   succeeds. Reusing a terminal does not reuse an attempt.
2. OpenCode `session.id = usage.sessions[].sessionId` is an exact join (verified
   with S). Full DB directory matches the path component of Orca `worktreeId`.
   A worktree alone does not identify an attempt: it can have several sessions
   or several tasks over its lifetime.
3. Legacy decisions can produce **candidate matches** by brief prefix, full
   path where recoverable, and time. Never resolve collisions by choosing the
   latest record or largest message count. Leave ambiguous and unmatched rows
   unscored. A unique heuristic match is still labelled heuristic.
4. For new work, the coordinator must explicitly bind the decision to the
   returned task/dispatch IDs and, when known, provider session ID(s). This
   supplies the missing relationship between existing records; it does not
   invent another telemetry source. A reused or mixed-task session needs an
   explicit message interval, otherwise its task-level cost remains unknown.

## Candidate definitions of “the pick was wrong”

These are computable screening rules, not causal verdicts. Only score an
applied recommendation: the decision must be linked to an attempt and the
actual model must match. Model mismatch is a launch problem; absent model proof
is unverified. Compute observed task outcomes separately even when pick quality
cannot be attributed. Thresholds below are proposed, not measured cutoffs.

| Candidate | Exact computation from the verified signals | False positives and limits |
| --- | --- | --- |
| **1. Reported failure** | Linked attempt has W `workerState='failed'` and its matching task result reports `provenance='worker_report', outcome='failed'`. Require an unambiguous dispatch relationship; a later task result must not be attributed to an earlier attempt. Label `reported failure`, retaining the result reason. Exclude `stopped`, blocked, missing and active states. | Bad environment, quota, impossible acceptance criteria, or a deliberately bounded investigation can fail on any model. Real `task_ab017edd4985` reports an unattainable three-line layout target; its failed status alone does not indict its model. Worker-reported success is not independent acceptance either. |
| **2. Three-attempt task** | For one `(runId,taskId)`, count distinct dispatch IDs with worker state in `{failed,succeeded}`, excluding stopped and unexecuted attempts. Flag a linked band-1 failed pick when there are at least three such attempts for the task. Call this `three-attempt task`, not `third attempt succeeded`, unless ordering and final success are also proven. | Infrastructure recovery, changed requirements or intentional repeated trials can create several attempts. Three corrections within one session are not three dispatches; tasks recreated under new IDs are missed. All 249 observed task keys currently have only one dispatch, so the inspected history supplies no positive example. Repeated decisions cannot substitute. |
| **3. Expensive successful session** | Among unambiguously attributed, succeeded, single-model OpenCode attempts with complete usage, group by repository, picked band, actual provider/model and recorded effort. With at least 20 *other* eligible attempts, flag an attempt whose DB activity span **and** OC usage `estimatedCostUsd` both exceed twice their respective cohort medians; require positive medians. Keep the metric sources identical across the cohort. | A legitimate larger task, long test/build waits, context size or tool verbosity can cause an outlier. Band is not task complexity matching. Cache discrepancies and partial sessions can bias the cohort. Small cohorts yield `insufficient sample`; Claude yields `cost unavailable`; there is no verified qualifying cohort here. |

Candidate 1 is the smallest useful first implementation. Candidate 2 captures
the motivating retry failure when task identity is retained. Candidate 3 is a
review queue, not a reason to escalate automatically. User messages, assistant
messages, tool errors, `eventCount` and `turnCount` are diagnostic counters,
never interchangeable counts of failed attempts. In S, one user message led
to 48 assistant messages, not 48 unsuccessful tries.

## Smallest implementation that closes the loop

Keep the single-file, standard-library design and the existing caller-driven
workflow. Proposed names below are additions unless marked existing. No
background service, new model judge, global model blacklist or automatic ladder
tuning is necessary.

| File / function | Proposed responsibility |
| --- | --- |
| `rightsize.py::log_decision` (existing), `print_decision` and `cmd_plan` (existing callers) | Assign and return a stable `decision_id`; expose it in route/plan JSON. Retain the picked fields and record repository path plus a hash of the complete normalized brief. These are new fields, not data claimed to exist today. |
| `rightsize.py::collect_outcome` | Read explicitly scoped/paginated W and T, the read-only DB tables, and `orca_sessions` (existing helper). Take explicit decision/task/dispatch/session bindings; return raw outcome, all observed models, message-role counts, activity span, source-labelled usage and freshness. Distinguish unavailable, active, ambiguous, model mismatch and reported failure. Inspect all assistant models, not just the largest group. |
| `rightsize.py::record_outcome` | Persist an observation in the existing state store keyed by `(decision_id,dispatch_id)`, with task/run/session IDs, observation time, source freshness, picked versus ran fields, outcome and reason. Upsert repeated collections rather than count them as attempts. Retain the decision snapshot so the 200-decision cap does not orphan recorded outcomes. Store compact evidence, not transcripts. |
| `rightsize.py::cmd_report` and `main` (existing) | Extend the current `report --from-orca --run ...` path with an explicit decision/task/dispatch association and optional session IDs. At coordinator settlement, collect and record evidence before releasing capacity. Preserve `--done` as capacity release, not success. Handle this branch before the existing default quota-error path, so a quality failure cannot exhaust an entire provider. |
| `rightsize.py::cmd_rerun` / `rerun` (existing) | Accept an optional recorded decision/dispatch reference. Reuse its failure reason, actual applied model and original picked band, instead of requiring the coordinator to retype them. Preserve current explicit `--because` / `--previous` usage. Link the new decision to the previous one. Unknown attribution and heuristic outliers must not silently trigger this path. |
| `test_rightsize.py` | Verify exact attribution, pagination, missing/stale usage, zero versus missing cost, mixed models, repeated collection idempotence, stopped versus failed, repeated recommendations versus dispatches, and failure-to-rerun linkage. Use synthetic records shaped like the verified sources; no live provider calls. |

The coordinator owns the missing step: after a dispatch settles it reports the
association, reviews the recorded reason, and invokes the linked `rerun` when
another attempt is warranted. The existing rerun policy then raises the band
floor, excludes the failed model and increases effort. Use the recorded band
for this linked path: looking it up in today's configurable ladder may no
longer recover the original choice. A quota failure continues to use the
existing quota fallback instead.

The report should print the observation and linked rerun option immediately;
no separate dashboard command is needed for the first version. A later summary
can compare reported failures and retry burden by model, with counts of
unmatched observations beside the scored denominator. Do not claim every routed
decision was dispatched or silently remove unknown cases from coverage figures.

All writes still share `state.json` with probes, reservations and routing.
Outcome writes need the same serialized read-modify-write protection as other
state mutations; atomic replacement alone would not prevent lost updates.
This design specifies that dependency, not a second state-storage subsystem.

Acceptance for implementation: a linked, model-verified failure survives
collection twice as one observation, can drive the existing rerun policy, and
does not alter unrelated providers or tasks. A missing source, ambiguous join,
stopped worker, duplicate recommendation or stale usage cannot become a failed
pick or free execution. This document does not implement those changes.

## What this cannot know

- Whether the code is correct, tests were meaningful, a reviewer accepted the
  work, or a shipped change later regressed. A result's prose can claim these;
  these sources do not independently verify them.
- Whether a bigger, smaller or different model would have succeeded sooner.
  There is no controlled counterfactual and task difficulty is confounded with
  model choice. These data do not prove over-sizing either.
- Why a worker failed from a status alone, or whether three messages represent
  corrections, new requirements, normal tool use or three actual attempts.
- The executed Codex model, effort, tokens or cost from the sources inspected
  here. A Codex recommendation is visible, but is not execution proof. Claude's
  summary model does not prove every turn used it, or reveal executed effort.
- Actual subscription charges or marginal quota burn per task from estimated
  dollar cost. Claude dollar cost is absent; OpenCode has a dollar estimate,
  not a subscription invoice.
- Complete lifetime totals from caches, retained worker history or a rolling
  200-decision log. Remote sessions, removed records, new task IDs on retries,
  shared sessions and decisions never launched remain coverage gaps.
- Active compute time from wall-clock timestamps. Human pauses, provider
  latency, tool waits and subsequent session reuse can all lengthen the span.

The defensible feedback is: “this applied pick has a reported failure / known
retry burden / unusually expensive session; here is the evidence.” The stronger
claim “this model was the wrong choice” still requires interpretation.

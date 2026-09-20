# OpenCode Zen free-model measurement

Measured **2026-09-20, 07:42:13–07:50:31 UTC** (Pacific/Auckland: 19:42–19:50), using OpenCode **1.18.31** on this machine and account. Repository baseline: `e66ff63228bfd3fe0a89bedccff79615e3ec06e6`.

**Result: 0/8 usable answers. Both models were rate-limited on every task and attempt. Their coding capability and successful-answer latency remain unmeasured.** Temporarily remove both from automatic band 1 fallback selection until availability is demonstrated by a successful rerun. This is a recommendation only: routing code, configuration, hooks, README, and POLICY were not changed.

## Results

`implement` asks for interval merging plus tests; `debug` asks for a pagination repair plus regression tests. Exact prompts follow below. Every call used a fresh session and a distinct verified scratch workspace under `/tmp`; no generated code or harness files were written in this repository.

“Wall seconds” is the monotonic time from process launch through process termination, including CLI startup and retry waiting. “First error seconds” is the first primary-model error timestamp in OpenCode's log minus the UTC launch timestamp; it uses wall clocks and is shown separately from the process timer.

| Model | Task | Attempt | Wall seconds | First error seconds | Usable output |
| --- | --- | ---: | ---: | ---: | --- |
| `nemotron-3-ultra-free` | implement | 1 | 300.046 | 2.814 | No: rate limit; 300 s timeout |
| `mimo-v2.5-free` | implement | 1 | 177.289 | 2.296 | No: rate limit; stopped retry wait |
| `nemotron-3-ultra-free` | debug | 1 | 3.983 | 2.821 | No: rate limit; stopped retry wait |
| `mimo-v2.5-free` | debug | 1 | 3.928 | 2.581 | No: rate limit; stopped retry wait |
| `nemotron-3-ultra-free` | implement | 2 | 2.966 | 2.552 | No: rate limit; stopped retry wait |
| `mimo-v2.5-free` | implement | 2 | 3.883 | 2.649 | No: rate limit; stopped retry wait |
| `nemotron-3-ultra-free` | debug | 2 | 3.135 | 2.535 | No: rate limit; stopped retry wait |
| `mimo-v2.5-free` | debug | 2 | 3.025 | 2.499 | No: rate limit; stopped retry wait |

Every primary-model error was exactly:

```text
AI_APICallError: Rate limit exceeded. Please try again later.
```

The error was logged internally; stdout stayed empty and stderr displayed only the model banner. The first Nemotron call reached the preselected 300-second timeout. The first MiMo call was stopped after the internal errors were discovered. For the remaining six calls, a watchdog stopped the CLI after its first logged primary-model rate-limit error rather than waiting through automatic retry delays. All processes exited with Python return code `-15` (SIGTERM from the harness/watchdog), not a natural provider-error exit. There were no generated answers to execute or judge.

### Medians

| Model | Calls | Usable | Median observed process wall seconds | Median first-error seconds | Median successful-answer seconds |
| --- | ---: | ---: | ---: | ---: | --- |
| `nemotron-3-ultra-free` | 4 | 0/4 | 3.5590 | 2.6830 | Not measurable (no answers) |
| `mimo-v2.5-free` | 4 | 0/4 | 3.9055 | 2.5400 | Not measurable (no answers) |

The process medians combine different stopping policies and must **not** be interpreted as normal model speed, or used to compare the models' speed. Error-arrival medians describe access failure only. The 300-second timeout does not prove that a successfully admitted coding request would take five minutes.

## Recommendation and limits

Temporarily remove both models from the **automatic band 1 ladder on this installation/account**, or otherwise make them ineligible until a later successful health check. A fallback that repeatedly returns no work cannot rescue an exhausted metered plan, and two entries subject to the same observed refusal did not supply independent fallback capacity. Do not promote either above paid candidates on this evidence. Permanent removal based on coding quality is also unsupported: neither model generated code.

The old “slow, therefore overflow capacity” explanation is insufficient. The current observation is an availability failure, not a coding-speed measurement. `docs/POLICY.md` and `config.json` contain that explanation at this baseline; the current README does not contain the exact old claim. Their existing text was left unchanged under this task's ownership boundary. The Nemotron review-ladder entry was not separately benchmarked, and no band 2 model was tested.

Rerun the same matrix after the free tier is available, ideally in more than one time window, before deciding permanent position. Require actual passing code for both contracts and record successful end-to-end latency. An eventual promotion should compare that evidence with a task-relevant baseline; this run intentionally spent no metered quota and provides no paid-model comparison. Eight calls in one short period on one account cannot distinguish account throttling from wider service availability, measure long-run reliability, or establish either model's inherent coding skill. Background title requests used the same free model and may also contend for free-tier capacity.

## Historical timing cross-check

A prior local Nemotron record (`ses_f425761dbffeFvi1r5CKnjm8TS`) has a 721.460-second assistant-message interval, but its output-token count is zero, `finish` is null, and the provider log records a rate-limit error. It is not evidence of a completed twelve-minute answer. Likewise, short database durations need a matched successful output before they support a claim of fast coding. This historical check is separate from the eight new calls and excluded from their medians.

```json
{"session":"ses_f425761dbffeFvi1r5CKnjm8TS","model":"nemotron-3-ultra-free","assistant_interval_seconds":721.46,"tokens_output":0,"finish":null,"message_error":null}
```

```text
timestamp=2026-09-20T07:12:22.014Z level=ERROR run=6ecb3836 message="stream error" providerID=opencode modelID=nemotron-3-ultra-free session.id=ses_f425761dbffeFvi1r5CKnjm8TS small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

This also shows why `message_error: null` alone does not prove success: the refusal was visible in the log while the unfinished message record carried no error.

## Method and quota isolation

- Two identical tasks × two models × two attempts, sequential and interleaved by model; each CLI call starts a fresh session. The matrix lasted about eight minutes, largely because the first two CLI processes waited after an already-recorded error.
- Normal invocation: `opencode run --dir <scratch-attempt-dir> -m opencode/<model>-free "<prompt>"`. No `opencode-go/`, Codex, Claude, or direct model HTTP invocation was used for any benchmark request.
- Both `cwd` and `PWD` were set to the scratch directory. `--dir` was explicit. `XDG_CONFIG_HOME` and `OPENCODE_CONFIG_DIR` pointed at scratch configuration; inherited `ORCA_*` variables were removed for the subprocess only. `OPENCODE_DISABLE_CLAUDE_CODE=true` was set to disable Claude instruction loading. The normal OpenCode data/auth location was retained, without copying or printing credentials.
- `OPENCODE_CONFIG_CONTENT` set `enabled_providers` to `["opencode"]`, and set both `model` and `small_model` to the exact free model. Every known agent override, including `build`, `plan`, `general`, `explore`, `title`, `summary`, `compaction`, and the local `forge` agent, was bound to that same free model. This matters because the user's normal main, small-model, and agent settings select metered Go models. MCP entries were disabled; scratch configuration emitted “Ignoring MCP config entry without type” for those entries, which is not the provider error. No persistent config or hook was edited.
- The clean matrix did not use `--pure` or blanket tool denial. Prompts requested response-only Python code, without tools or delegation. This is a code-generation/diagnosis test, not an evaluation of repository navigation or agentic editing.
- Read-only checks of OpenCode's SQLite records verified all eight actual session directories under `/private/tmp/.../benchmark/` (macOS's canonical `/tmp` path), provider `opencode`, and the requested free model. All eight recorded costs and token counters were zero. Internal title-error log entries also named free models. These are local accounting observations, not an independent billing audit.
- Usability requires a complete implementation, passing model-written tests, and independent contract checks: interval edge cases, invalid intervals, generator input, mutation checks and 200 seeded comparisons; pagination immediate validation, aliasing, 248 length/page-size combinations, and retained-page mutation checks. No response reached this gate, so no functional tests ran and no quality pass/fail rate can be inferred beyond **zero usable deliverables**.

Scratch harnesses and raw files remain locally in `/tmp/rightsize-free-models-20260920/`. The durable evidence is quoted below so this report does not depend on temporary files surviving.

## Exact prompts

### implement

```text
Write Python 3 standard-library code implementing merge_windows(windows).
Contract: windows is an iterable (possibly a one-shot generator) of pairs of integers (start, end) representing half-open intervals. Reject any interval with start > end by raising ValueError, even if another interval contains it. Ignore empty intervals (start == end). Return a new list of tuples sorted by start, merging overlapping OR touching intervals. Do not mutate caller-owned input lists. Empty input returns [].
Return exactly one runnable Python code block containing the function and unittest tests, with unittest.main() under the usual __main__ guard. Tests must cover unsorted input, touching endpoints, nesting, negative coordinates, empty intervals, invalid intervals, a generator, and non-mutation. Do not use tools, read files, install packages, or delegate; answer entirely in the response.
```

### debug

```text
Find and fix the bug(s) in this Python function. Contract: paginate(items, size) accepts any finite iterable including a one-shot generator. size is an integer; size <= 0 raises ValueError immediately when paginate is called, before any iteration. Return an iterator yielding independent lists containing up to size consecutive items, preserving every item in order. Empty input yields nothing; no empty final page; exact multiples are valid.

Current buggy implementation:
def paginate(items, size):
    if size <= 0:
        raise ValueError("size must be positive")
    page = []
    for item in items:
        page.append(item)
        if len(page) == size:
            yield page
            page.clear()
    if page:
        yield page

Return exactly one runnable Python code block containing a brief diagnosis as comments, the corrected paginate function, and unittest regression tests, with unittest.main() under the usual __main__ guard. Cover immediate validation, list aliasing across pages, final partial pages, exact multiples, empty input, and a one-shot input generator. Do not use tools, read files, install packages, or delegate; answer entirely in the response.
```

## Raw clean-matrix outputs

For each call, stdout is quoted as a JSON string to represent the empty stream unambiguously. Stderr is also a JSON string, preserving ANSI escape sequences. The primary-model log line is copied verbatim from `~/.local/share/opencode/log/opencode.log`. Session IDs tie those lines to the independently checked workspace/model records.

### 1. nemotron-3-ultra-free, implement, attempt 1

Started `2026-09-20T07:42:13.132683+00:00`; session `ses_f423c00daffecNl78rSREZVdbp`; wall 300.046 s; first error 2.814 s.

```text
stdout = ""
stderr = "\u001b[0m\n> build \u00b7 nemotron-3-ultra-free\n\u001b[0m\n"
timestamp=2026-09-20T07:42:15.947Z level=ERROR run=60f49e3a message="stream error" providerID=opencode modelID=nemotron-3-ultra-free session.id=ses_f423c00daffecNl78rSREZVdbp small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

### 2. mimo-v2.5-free, implement, attempt 1

Started `2026-09-20T07:47:13.198161+00:00`; session `ses_f42376ce5ffeb5YoZXI62qozuU`; wall 177.289 s; first error 2.296 s.

```text
stdout = ""
stderr = "\u001b[0m\n> build \u00b7 mimo-v2.5-free\n\u001b[0m\n"
timestamp=2026-09-20T07:47:15.494Z level=ERROR run=cbf30a5c message="stream error" providerID=opencode modelID=mimo-v2.5-free session.id=ses_f42376ce5ffeb5YoZXI62qozuU small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

### 3. nemotron-3-ultra-free, debug, attempt 1

Started `2026-09-20T07:50:10.490154+00:00`; session `ses_f4234b869ffep4llcToASHneUa`; wall 3.983 s; first error 2.821 s.

```text
stdout = ""
stderr = "\u001b[0m\n> build \u00b7 nemotron-3-ultra-free\n\u001b[0m\n"
timestamp=2026-09-20T07:50:13.311Z level=ERROR run=e81c142e message="stream error" providerID=opencode modelID=nemotron-3-ultra-free session.id=ses_f4234b869ffep4llcToASHneUa small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

### 4. mimo-v2.5-free, debug, attempt 1

Started `2026-09-20T07:50:14.474509+00:00`; session `ses_f4234a8ebffe64Aiap55hAVEox`; wall 3.928 s; first error 2.581 s.

```text
stdout = ""
stderr = "\u001b[0m\n> build \u00b7 mimo-v2.5-free\n\u001b[0m\n"
timestamp=2026-09-20T07:50:17.056Z level=ERROR run=5fd28dfd message="stream error" providerID=opencode modelID=mimo-v2.5-free session.id=ses_f4234a8ebffe64Aiap55hAVEox small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

### 5. nemotron-3-ultra-free, implement, attempt 2

Started `2026-09-20T07:50:18.403813+00:00`; session `ses_f4234999cffe2Wx2pwUt9hJs7W`; wall 2.966 s; first error 2.552 s.

```text
stdout = ""
stderr = "\u001b[0m\n> build \u00b7 nemotron-3-ultra-free\n\u001b[0m\n"
timestamp=2026-09-20T07:50:20.956Z level=ERROR run=ede8f373 message="stream error" providerID=opencode modelID=nemotron-3-ultra-free session.id=ses_f4234999cffe2Wx2pwUt9hJs7W small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

### 6. mimo-v2.5-free, implement, attempt 2

Started `2026-09-20T07:50:21.371012+00:00`; session `ses_f42348e05ffeThLnOrT9jmy54g`; wall 3.883 s; first error 2.649 s.

```text
stdout = ""
stderr = "\u001b[0m\n> build \u00b7 mimo-v2.5-free\n\u001b[0m\n"
timestamp=2026-09-20T07:50:24.020Z level=ERROR run=3cc12530 message="stream error" providerID=opencode modelID=mimo-v2.5-free session.id=ses_f42348e05ffeThLnOrT9jmy54g small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

### 7. nemotron-3-ultra-free, debug, attempt 2

Started `2026-09-20T07:50:25.255122+00:00`; session `ses_f42347ed7ffeydH1trnwvCuQDc`; wall 3.135 s; first error 2.535 s.

```text
stdout = ""
stderr = "\u001b[0m\n> build \u00b7 nemotron-3-ultra-free\n\u001b[0m\n"
timestamp=2026-09-20T07:50:27.790Z level=ERROR run=a5255e84 message="stream error" providerID=opencode modelID=nemotron-3-ultra-free session.id=ses_f42347ed7ffeydH1trnwvCuQDc small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

### 8. mimo-v2.5-free, debug, attempt 2

Started `2026-09-20T07:50:28.391378+00:00`; session `ses_f42347288ffeL2Q3m27uB2gpp6`; wall 3.025 s; first error 2.499 s.

```text
stdout = ""
stderr = "\u001b[0m\n> build \u00b7 mimo-v2.5-free\n\u001b[0m\n"
timestamp=2026-09-20T07:50:30.890Z level=ERROR run=e5e8cc85 message="stream error" providerID=opencode modelID=mimo-v2.5-free session.id=ses_f42347288ffeL2Q3m27uB2gpp6 small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

## Excluded setup diagnostics

These exploratory calls are disclosed separately and excluded from the clean-matrix medians. An initial eight-call pass used `--pure --format json`, blanket tool denial, and a scratch subprocess working directory, but inherited `PWD`/Orca configuration caused OpenCode to record the repository workspace. All eight failed before generating an answer. A subsequent ordinary-CLI control was interrupted when that workspace attribution was discovered. The clean matrix then explicitly bound the scratch directory and verified actual session records. No setup call generated code or edited repository files.

The initial calls exited naturally with code 1 and returned HTTP 403, with this exact response body:

```json
{"type":"error","error":{"type":"FreeTierError","message":"OpenCode's free tier can only be used from within OpenCode"}}
```

This refusal occurred despite use of the CLI. Because multiple settings differed, these diagnostics do not establish which setting caused it. Do not substitute their short error latency for the clean matrix or for successful response latency.

| Model | Task | Attempt | Wall seconds | Usable output |
| --- | --- | ---: | ---: | --- |
| `nemotron-3-ultra-free` | implement | 1 | 2.986 | No: HTTP 403 |
| `mimo-v2.5-free` | implement | 1 | 2.379 | No: HTTP 403 |
| `nemotron-3-ultra-free` | debug | 1 | 2.381 | No: HTTP 403 |
| `mimo-v2.5-free` | debug | 1 | 2.594 | No: HTTP 403 |
| `nemotron-3-ultra-free` | implement | 2 | 2.388 | No: HTTP 403 |
| `mimo-v2.5-free` | implement | 2 | 2.337 | No: HTTP 403 |
| `nemotron-3-ultra-free` | debug | 2 | 2.439 | No: HTTP 403 |
| `mimo-v2.5-free` | debug | 2 | 2.444 | No: HTTP 403 |

<details>
<summary>Full initial JSON event outputs (eight calls)</summary>


`nemotron-3-ultra-free-implement-1` (stderr empty):

```json
{"type":"error","timestamp":1789889956960,"sessionID":"ses_f423ebc02ffekug30TWm7XWUDA","error":{"name":"APIError","data":{"message":"OpenCode's free tier can only be used from within OpenCode","statusCode":403,"isRetryable":false,"responseHeaders":{"cf-placement":"remote-ORD","cf-ray":"a3df2ce6582be9e7-SJC","connection":"keep-alive","content-encoding":"br","content-type":"application/json","date":"Sun, 20 Sep 2026 07:39:17 GMT","server":"cloudflare","transfer-encoding":"chunked","x-opencode-log-id":"532bae45-a2a3-4504-9799-e3875f4cbc3b"},"responseBody":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode\"}}","metadata":{"url":"https://opencode.ai/zen/v1/chat/completions"}}}}
```


`mimo-v2.5-free-implement-1` (stderr empty):

```json
{"type":"error","timestamp":1789889959305,"sessionID":"ses_f423eb0baffeCnMfyWA9zkqPCv","error":{"name":"APIError","data":{"message":"OpenCode's free tier can only be used from within OpenCode","statusCode":403,"isRetryable":false,"responseHeaders":{"cf-placement":"remote-ORD","cf-ray":"a3df2cf56ce4b876-PDX","connection":"keep-alive","content-encoding":"br","content-type":"application/json","date":"Sun, 20 Sep 2026 07:39:19 GMT","server":"cloudflare","transfer-encoding":"chunked","x-opencode-log-id":"adc8ea4d-efbf-44be-8a7a-1d1ee21551fa"},"responseBody":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode\"}}","metadata":{"url":"https://opencode.ai/zen/v1/chat/completions"}}}}
```


`nemotron-3-ultra-free-debug-1` (stderr empty):

```json
{"type":"error","timestamp":1789889961707,"sessionID":"ses_f423ea76affe1krP0QjJTuiOPo","error":{"name":"APIError","data":{"message":"OpenCode's free tier can only be used from within OpenCode","statusCode":403,"isRetryable":false,"responseHeaders":{"cf-placement":"remote-ORD","cf-ray":"a3df2d0459c114c7-SJC","connection":"keep-alive","content-encoding":"br","content-type":"application/json","date":"Sun, 20 Sep 2026 07:39:21 GMT","server":"cloudflare","transfer-encoding":"chunked","x-opencode-log-id":"6b8ca5ea-dc0d-4695-9fd9-5cd3398b1826"},"responseBody":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode\"}}","metadata":{"url":"https://opencode.ai/zen/v1/chat/completions"}}}}
```


`mimo-v2.5-free-debug-1` (stderr empty):

```json
{"type":"error","timestamp":1789889964306,"sessionID":"ses_f423e9dd5ffefhh1LHUoVhQtY3","error":{"name":"APIError","data":{"message":"OpenCode's free tier can only be used from within OpenCode","statusCode":403,"isRetryable":false,"responseHeaders":{"cf-placement":"remote-ORD","cf-ray":"a3df2d1478b15fe3-SJC","connection":"keep-alive","content-encoding":"br","content-type":"application/json","date":"Sun, 20 Sep 2026 07:39:24 GMT","server":"cloudflare","transfer-encoding":"chunked","x-opencode-log-id":"ffbe1528-0bb1-4aad-ac13-4b4b2e8d56fb"},"responseBody":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode\"}}","metadata":{"url":"https://opencode.ai/zen/v1/chat/completions"}}}}
```


`nemotron-3-ultra-free-implement-2` (stderr empty):

```json
{"type":"error","timestamp":1789889966651,"sessionID":"ses_f423e93fcffe6a95q3gEkLTTNW","error":{"name":"APIError","data":{"message":"OpenCode's free tier can only be used from within OpenCode","statusCode":403,"isRetryable":false,"responseHeaders":{"cf-placement":"remote-ORD","cf-ray":"a3df2d233e44cfe1-SJC","connection":"keep-alive","content-encoding":"br","content-type":"application/json","date":"Sun, 20 Sep 2026 07:39:26 GMT","server":"cloudflare","transfer-encoding":"chunked","x-opencode-log-id":"af65e167-8055-4978-9cdb-61e061e2a81d"},"responseBody":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode\"}}","metadata":{"url":"https://opencode.ai/zen/v1/chat/completions"}}}}
```


`mimo-v2.5-free-implement-2` (stderr empty):

```json
{"type":"error","timestamp":1789889969048,"sessionID":"ses_f423e8aa6ffe8R47a5oQDk11Vk","error":{"name":"APIError","data":{"message":"OpenCode's free tier can only be used from within OpenCode","statusCode":403,"isRetryable":false,"responseHeaders":{"cf-placement":"remote-ORD","cf-ray":"a3df2d322ffb87ab-PDX","connection":"keep-alive","content-encoding":"br","content-type":"application/json","date":"Sun, 20 Sep 2026 07:39:29 GMT","server":"cloudflare","transfer-encoding":"chunked","x-opencode-log-id":"74c71d39-d6a5-456c-a23f-df59378bf3f9"},"responseBody":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode\"}}","metadata":{"url":"https://opencode.ai/zen/v1/chat/completions"}}}}
```


`nemotron-3-ultra-free-debug-2` (stderr empty):

```json
{"type":"error","timestamp":1789889971467,"sessionID":"ses_f423e818cffeWR6cv7cv2XG91t","error":{"name":"APIError","data":{"message":"OpenCode's free tier can only be used from within OpenCode","statusCode":403,"isRetryable":false,"responseHeaders":{"cf-placement":"remote-ORD","cf-ray":"a3df2d414efd7c8f-SJC","connection":"keep-alive","content-encoding":"br","content-type":"application/json","date":"Sun, 20 Sep 2026 07:39:31 GMT","server":"cloudflare","transfer-encoding":"chunked","x-opencode-log-id":"2f1db746-8513-41dc-bd21-0de06e3bfe70"},"responseBody":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode\"}}","metadata":{"url":"https://opencode.ai/zen/v1/chat/completions"}}}}
```


`mimo-v2.5-free-debug-2` (stderr empty):

```json
{"type":"error","timestamp":1789889973907,"sessionID":"ses_f423e77fcffeK4xtijHTJkVVzq","error":{"name":"APIError","data":{"message":"OpenCode's free tier can only be used from within OpenCode","statusCode":403,"isRetryable":false,"responseHeaders":{"cf-placement":"remote-ORD","cf-ray":"a3df2d509be20c31-PDX","connection":"keep-alive","content-encoding":"br","content-type":"application/json","date":"Sun, 20 Sep 2026 07:39:33 GMT","server":"cloudflare","transfer-encoding":"chunked","x-opencode-log-id":"4744e9e9-ea14-4e65-8e8c-54d7c67ff6ab"},"responseBody":"{\"type\":\"error\",\"error\":{\"type\":\"FreeTierError\",\"message\":\"OpenCode's free tier can only be used from within OpenCode\"}}","metadata":{"url":"https://opencode.ai/zen/v1/chat/completions"}}}}
```

</details>

The interrupted ordinary-CLI control was Nemotron/implement/attempt 1, excluded from the benchmark. Its process timer was lost when the runner was stopped: file timestamps and the post-stop record give approximately **140 seconds**, not an exact wall measurement. Its stdout was empty; stderr was the same Nemotron banner quoted above. The internal log recorded:

```text
timestamp=2026-09-20T07:39:55.808Z level=ERROR run=c80ab1ce message="stream error" providerID=opencode modelID=nemotron-3-ultra-free session.id=ses_f423e247effeimfH9Rino5710H small=false agent=build mode=primary error.error="AI_APICallError: Rate limit exceeded. Please try again later."
```

That diagnostic's approximate timing is a collection limitation. Every call in the corrected eight-call acceptance matrix has a retained monotonic process duration and a matched first-error timestamp.

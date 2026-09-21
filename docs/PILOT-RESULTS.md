# Bounded pilot: 2026-09-21

The first live trial stopped at its predeclared native input-token threshold.
**It does not satisfy the complete 20-pair promotion gate.** No default rollout
or savings claim follows. Tracking: [#19](https://github.com/karoliang/rightsize/issues/19),
[follow-up #22](https://github.com/karoliang/rightsize/issues/22).

Candidate: `87c121695711ecbe49ae4d077abc2c71b4564902`.
Historical policy: `a168a85055a3f1fa527da593fced87cf37c10892`.
[Audited aggregate evidence](../fixtures/pilot/results/2026-09-21.json) includes
all task outcomes, source hashes, review joins and unchanged unstarted tasks.
Private account/session/worktree identifiers are omitted.

## Results

| Measure | Baseline | Candidate |
| --- | ---: | ---: |
| Planned tasks | 20 | 20 |
| Attempted tasks | 14 | 14 |
| Accepted tasks | 13 | 14 |
| Native attempts, including retries | 15 | 15 |
| Rejected first attempts | 1 | 1 |
| Budget-triggered native cancellations | 1 | 0 |
| Unstarted tasks | 6 | 6 |
| Unresolved attempts at stop | 0 | 0 |

All 15 captured policy comparisons (14 initial observations and one retry
observation) selected the same Codex provider/model/effort for both arms.
Mechanical/implementation tasks selected `gpt-5.6-luna`, effort low;
diagnosis/high-stakes selected `gpt-6-astra`, effort high.
The apparent 14-versus-13 acceptance difference is budget truncation, not evidence
of a better routing choice. Thirteen complete pairs were accepted on both arms.

Both first stable-deduplication attempts left Python cache directories. Review
rejected them before container execution for violating the task file contract.
TASK.md and Git metadata remained unchanged. Each passed the one predeclared
retry, from the original sources without acceptance answers or generated feedback.
All other finished, reviewed attempts were accepted. The last baseline task,
mechanical-terminal-labels, settled as native cancelled after the input threshold
was observed. Its output was not promoted to accepted.

The four executed high-stakes task pairs were manually inspected: denial/reserve/
slot precedence, nonempty credential-field equality, exact terminal session/turn
proof, and idempotency-key conflict handling. Both arms preserved those contracts.
The fifth high-stakes pair, permission intersection, was not started. These are
isolated coding fixtures, not production credential changes or native lifecycle
proof by themselves.

## Usage and timing

Predeclared per-provider ceilings: Codex 40 attempts/100 estimated dispatch points;
OpenCode 60/50; Claude 20/100. Each had a 2,000,000 input-token and 100,000
output-token stop threshold. Native quota/reserve checks still applied. No limits
were increased, no paid fallback was enabled, and no tasks were replaced.

Only Codex was selected: 30 attempts, 69.6 conservative dispatch points,
2,010,692 native input tokens and 20,187 output tokens. The 10,692-token overshoot
illustrates asynchronous observation/cancellation: this is not a hard billing cap.
Cached input (1,477,376) and reasoning (3,173) are overlapping native categories,
not extra tokens to add. Native total was 2,030,879. Dispatch points are estimates;
shared quota observations do not establish attributable subscription consumption.
No dollar cost or savings is inferred.

Native metrics recorded 22 tool failures. A sampled output reports blocked
Python/Git cache access outside the worktree; a complete attribution is still
required in #22. Independent review, not the model's own test claims, determines
acceptance. Preserve these failures when assessing setup overhead.

| Admission to independent acceptance | Baseline | Candidate |
| --- | ---: | ---: |
| Accepted task samples | 13 | 14 |
| Median, seconds | 36.27 | 31.50 |
| Nearest-rank p95, seconds | 97.32 | 115.96 |

These task times start at the first admission and include retry/interleaved-arm
waiting until acceptance. Successful-attempt-only medians were 35.84/29.90 seconds
and p95 values 46.82/37.41; omitting retry waits would hide real elapsed time.
Cancelled/unstarted tasks are excluded from accepted-result timing and retained
in coverage. First-useful-output latency is unavailable: native first output can
be commentary. Tiny, same-choice samples do not establish a router speed benefit.

## Verification and remaining gates

Before launch, [CI35561341218](https://github.com/karoliang/rightsize/actions/runs/35561341218)
passed Python3.11/3.12/3.13 plus real container acceptance. Source permissions were
prepared and exercised separately. After the run, an independent read-only audit
recomputed every frozen baseline/candidate verdict, checked unique native attempt
IDs and request joins, matched terminal ledger states and metrics, verified each
review digest and exact source/corpus/task hashes, and checked accepted worktrees
for extra files or changed task/Git metadata. All joins passed. Both trial and
global managed ledger had zero active attempts at the final checkpoint.

The trial remains permanently halted with its original limits. #22 tracks
avoidable context/test overhead and a newly predeclared, fairly budgeted complete
evaluation. Native Claude, scoped vault delivery and the Orca prepared-account
contract remain separate gates in #14/#17/#21. No production state migration or
default routing promotion was performed for this trial.

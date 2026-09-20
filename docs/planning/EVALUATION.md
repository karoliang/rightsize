# Evaluation and rollout gates

Date: 2026-09-21. Ticket: #11. Fixed baseline: `a168a85`.

## Established baseline

At the repair checkpoint, policy tests and hook tests passed, along with all
16 reliability tests on Python3.11/3.12/3.13 in
[CI35531462233](https://github.com/karoliang/rightsize/actions/runs/35531462233).
These establish the tested invariants, not overall routing quality. Private
session counts are not a public benchmark and messages are not accepted tasks.
No paid head-to-head model benchmark was run during planning.

## Offline suite before any live rollout

| Fixture family | Pass condition |
| --- | --- |
| Explicit denial, missing telemetry, reset, multiple buckets | Zero admissions to denied account; missing data cannot erase known denial |
| Concurrent reservations | N affordable points and K slots never admit more committed work than either bound |
| Account switch and credential expiry | Probe and launch stay on same account; no silent paid-account fallback |
| Launch failure/timeout/cancel/restart | No duplicate launch on uncertain outcome; no premature release; terminal receipt idempotent |
| Model/effort mismatch and renamed worktree | Actual receipt remains distinct from requested selection; no name-only success inference |
| Zero output, missing join, tool failure, completed but rejected | Distinct outcomes and explicit unknown coverage |
| Caller judgment | Strict schema, exact task binding, zero judge calls, invalid input has zero probe/state side effects |
| Skills/context | Allowed-root enforcement, malicious source text inert, mandatory rules preserved, byte budget recorded |
| Protocol drift and malformed native events | Bounded parse/deadline, labelled unavailable, no hung child |
| Migration and rollback | Old commands/config work; migration idempotent; active holds preserved conservatively |

Extend `test_reliability.py` and focused new contract suites. Use synthetic records
with fake secrets as sentinels; assert no sentinel reaches stdout/stderr/state.
Retain the measured failing baseline where useful through fixtures, not a second
production router implementation. All default CI evaluation remains offline.

## Small live pilot after offline admission gates

Select 20 bounded tasks from ordinary implementation, diagnosis, high-stakes
changes and mechanical work, with deterministic acceptance criteria and explicit
source permissions. Run fixed-baseline and candidate on isolated equivalent
snapshots in alternating order, recording retries and review results. Cap the
pilot at one initial attempt plus one retry per variant per task, and set a
provider-specific usage ceiling before starting; no paid API fallback. This is a
proposed experiment, not an assertion that 20 tasks gives statistical certainty.

| Measure | Definition / proposed acceptance |
| --- | --- |
| Quota safety | 0 known-denied admissions; 0 cross-account observation/launch mismatches |
| Useful result | Accepted tasks / attempted tasks; missing outcomes remain in denominator |
| Quality | Candidate accepted-task rate no worse than baseline on paired tasks; manually inspect every high-stakes failure |
| Usage | Tokens by provider category plus measured subscription points; do not combine unrelated units or double-count session/step totals |
| Latency | Time to first useful output and time to accepted result, report median/p95 and timeouts separately |
| Setup | Existing logged-in CLI user routes with 0 new judge keys; supported account selection is visible |
| Added context | Bytes read and transmitted, optional skills loaded, estimated tokens labelled |
| Observability | Every managed attempt has decision/account/receipt/outcome IDs or an explicit unresolved state |

Price-normalized cost is reported only when pricing and billing mode are verified;
no invented dollars for subscription quota. A refusal is not an answer-time sample.
Bootstrap intervals or raw paired outcomes are preferable to a claimed savings
percentage from a tiny trial. Default promotion requires all safety invariants,
complete lifecycle coverage and no accepted-result regression; revise quality or
latency thresholds explicitly before seeing results if the task mix changes.

## Rollout

Offline replay -> read-only shadow decisions -> a bounded local pilot -> opt-in
managed dispatch. Shadow never books capacity or launches. Stay opt-in until
account, context and outcome evidence survives the pilot. Any quota/account leak,
duplicate side effect or disappearing active lease stops promotion and restores
the legacy path with outstanding attempts reconciled first.

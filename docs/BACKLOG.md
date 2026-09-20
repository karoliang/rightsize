# Rightsize planning backlog

GitHub is the task/status authority. Start with the parent
[#1: reliable routing, skills and minimal setup](https://github.com/karoliang/rightsize/issues/1).

## First: repair the current router

| Ticket | Deliverable |
| --- | --- |
| [#2](https://github.com/karoliang/rightsize/issues/2) | Veto known quota denial in every band |
| [#3](https://github.com/karoliang/rightsize/issues/3) | Recognize the shipped multiline launch |
| [#4](https://github.com/karoliang/rightsize/issues/4) | Record actual provider/model and dispatch, never reroute to account |
| [#5](https://github.com/karoliang/rightsize/issues/5) | Select cooldown from denied windows and refresh before reuse |
| [#6](https://github.com/karoliang/rightsize/issues/6) | Report missing evidence, zero output and unmatched sessions honestly |

Offline proof: `python3 test_rightsize.py`, `python3 hooks/test_hook.py`, and
`python3 test_reliability.py`. These fixes are the baseline for planning.

## Then: decide what the router should become

| Ticket | Question and required output | Depends on |
| --- | --- | --- |
| [#7](https://github.com/karoliang/rightsize/issues/7) | Advisor, launch owner, request router, or hybrid? Supported-client matrix and boundary proposal. | Reliability baseline |
| [#8](https://github.com/karoliang/rightsize/issues/8) | Which existing coding-agent sign-ins/API keys/vault connections can be reused through supported interfaces? Credential and account contract with minimal setup. | #7 boundary draft |
| [#9](https://github.com/karoliang/rightsize/issues/9) | How should task/skill/context reading improve model choice without excess tokens or a mandatory extra judge key? Bounded discovery and trust design. | #7, #8 |
| [#10](https://github.com/karoliang/rightsize/issues/10) | Who admits, reserves, launches, observes, retries and releases work across clients? Identity and lifecycle contract. | #7, #8 |
| [#11](https://github.com/karoliang/rightsize/issues/11) | What proves better outcomes, quota safety and less setup? Replay fixtures, comparison method and acceptance metrics. | Baseline; refine with #9, #10 |
| [#12](https://github.com/karoliang/rightsize/issues/12) | Incremental architecture or rebuild? ADR, smallest vertical slice, migration/rollback and implementation tickets. | #7-#11 |

## Planning constraints

- Reuse supported credentials and existing vault connections where possible;
  subscription OAuth and metered API keys are distinct capabilities to verify.
- The Brain stays canonical project memory. A skill/context reader must not
  create a competing cross-project memory store. Secret-vault credentials stay
  in their existing provider; routing records carry references, not values.
- Read relevant skill metadata first, then selected instructions/content within
  a context budget. Preserve repository rules and distinguish instructions
  from untrusted task/source material.
- Keep quotas, account identity, reservations and cost arithmetic in code.
  Compare active-model judging, optional provider judging and local fallback.
- Judge improvement by accepted results, quota failures, retries, time to first
  output, usage/context overhead and setup steps, not message counts alone.
- Current contribution rules deliberately exclude the token path and keep hooks
  advisory. #7/#12 may propose changing those rules with an explicit ADR; this
  repair does not silently change them.
- Planning ends with an accepted architecture and prioritized implementation
  slices. A rewrite, proxy, new credential store or live migration is not
  automatically authorized by a planning issue.

## Selected architecture and implementation slices (2026-09-21)

The planning deliverables are complete in [ADR0001](decisions/0001-local-task-router.md)
and [docs/planning](planning/). Direction: local task/model routing through native
coding runtimes, caller-supplied task judgment, account-aware admission and exact
outcome records. No universal API proxy or full agent rewrite selected.

| Ticket | Next deliverable | Dependency |
| --- | --- | --- |
| [#13](https://github.com/karoliang/rightsize/issues/13) | Caller judgment for single-task route, without another judge call | Independent first slice |
| [#14](https://github.com/karoliang/rightsize/issues/14) | Native account discovery, real probe deadlines and scoped credential references | Credential contract |
| [#15](https://github.com/karoliang/rightsize/issues/15) | Bounded skill/knowledge context manifests | #13 |
| [#16](https://github.com/karoliang/rightsize/issues/16) | Atomic account-aware admission and launch leases | #13, #14 |
| [#17](https://github.com/karoliang/rightsize/issues/17) | Native session adapters, cancellation and outcome reconciliation | #14, #16 |
| [#18](https://github.com/karoliang/rightsize/issues/18) | Offline replay and side-effect-free shadow comparison | Fixtures can start now; promotion uses #15-#17 |
| [#19](https://github.com/karoliang/rightsize/issues/19) | Versioned state migration, pilot and rollback | #14, #16-#18 |

Current slice implements #13 only. #14 is next. GitHub remains the source of
truth for open/closed state; planning closure does not imply these features ship.

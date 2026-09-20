# Which rules still earn their place?

Audit of `bbef1a4` on 2026-09-20. Read [POLICY.md](POLICY.md) and
[rightsize.py](../rightsize.py) end to end. Locations below refer to that revision.
This is a recommendation, not an implementation change.

**No entire rule in the requested list is universally redundant.** Each has a
counterexample below. The useful simplification is to remove repeated predicates
and consolidate admission, forecasting and fallback, rather than delete a guard
because another guard rejected one particular task too.

## What was acted on, 2026-09-20

Recommendations 1 and 3 are applied: the duplicate `usable is None` predicate is
gone from `pick`, and the pasted second docstring in `plan` is merged into the
first.

Recommendation 2 is now applied: `admission_block` is the shared reserve-floor
and dispatch-affordability calculation used by eligibility, picking, batch
debit, and wave transitions. The reserve veto remains first, so numeric zero
with zero dispatch cost is still rejected, and the existing diagnostic wording
is retained at each observable call site.

The implementation deliberately does not simply erase all three checks. This
report's own reasoning is why: numeric `usable` 0 with cost 0 is a supported
configuration that the reserve veto rejects and `0 < 0` does not, and `eligible`,
`blocked` and the decision notes are observable even when the chosen worker is
identical. This is a merge into one admission calculation, not a relaxation.

The report's other conclusion held up the same day: burn rate, reserves, the two
clocks, the band 3 inversion and `relax_pace` all survived a separate
adversarial pass, and two of them were shown to catch failures nothing else
catches.

## Recommendations, strongest evidence first

### Delete

1. **Delete the redundant `usable is None` arm of the unknown-headroom gate**
   (`pick`, line 1240), keeping the `unknown` check and free-provider exception.
   Every production eligibility record with `usable=None` already has
   `unknown=True`: `headroom` returns both together when there is no known bucket;
   reservation debit and wave transitions never unset `unknown` or change a
   number to `None`. No reachable state distinguishes these predicates. For
   `{'a': P(B(None))}`, both versions reject band 1 and choose `a:deep` at band 3;
   for `{'a': P(free=True)}`, both admit the free candidate when laddered.
   Cost: callers constructing incomplete eligibility dictionaries directly would
   need the invariant documented. This removes a duplicate predicate, **not the
   unknown-bucket taint**. Keep the `usable is None` check used to select the
   explanatory message.

2. **Delete the repeated reserve-floor veto as an independent routing rule,
   conditionally, when consolidating admission.** In `eligibility:969`,
   `debit:1345`, and `start_wave:1361`, `usable <= 0` is already covered by
   `pick:1256`'s `usable < dispatch_cost` for every positive-cost candidate.
   With `a` at 95% used, reserve 10 and cost 1, usable is -5: either veto alone
   rejects every band. Removing only the eligibility veto produced identical
   routing in 198 replay comparisons (used 80 through 101, bands 1 through 3,
   base costs 0.1, 1 and 3). Algebra, rather than that finite sample, establishes
   the positive-cost implication. All shipped metered costs are positive; the
   shipped zero-cost provider has no numeric bucket.

   **Do not simply erase all three checks today.** Numeric usable 0 and cost 0
   is a supported configuration: the reserve veto rejects it while `0 < 0`
   does not. Also `eligible`, `blocked`, `hard_blocked`, probe output and decision
   notes are observable even when the chosen worker is identical. Keep the
   reserve amount in the arithmetic and preserve the floor/zero-cost semantics
   and diagnostics in one admission calculation. This is a merge followed by
   deletion of duplicated vetoes, not permission to spend the reserve.

3. **Delete the second standalone “Route a whole fan-out at once” string in
   `plan:1376`.** Only the first string is its docstring; the second supplies no
   behavior. Fold its useful explanation into the first. This is trivial
   cleanup, not evidence that waves are redundant.

I do **not** recommend deleting burn rate, reserves, the clocks, inversion, or
`relax_pace` outright. Their deletion changes executable examples below.

### Merge

1. **One admission calculation for provider availability, usable budget and
   dispatch affordability.** Keep separate reasons for credentials, reported
   exhaustion, occupied slots, unknown quota and cost. `eligibility`, `debit`,
   `start_wave`, and `pick` currently maintain overlapping views of admission.
   Preserve `usable < cost` (equality is affordable), the reserve floor, and the
   distinction between a slot a wave can return and quota it cannot. In
   particular, stop determining `hard_blocked` from whether a human-readable
   message contains `"in flight"`. This is a consolidation proposal; no claim
   that slot capacity and quota capacity are interchangeable.

2. **One forecast result with its source, not one forecasting formula.** Window
   pace and recent burn already merge into `overrun`; retain both measurements
   but make their provenance explicit. Recent acceleration is not implied by
   a whole-window average, and a nonbinding monthly overrun is invisible to the
   binding-bucket burn calculation. `debit` must update the same forecast state.
   Deleting recent burn could be a deliberate reduction in conservatism, but
   the burn-only witness below is the protection it would lose.

3. **One explicit candidate-attempt sequence for fallback and pace relaxation.**
   Preserve the present order before simplifying it: requested band with pace;
   successively cheaper bands with pace; requested band without pace (only when
   the original band was below 3); finally band 3. Batch allocation attempts
   only the requested band with pace. Combining the loops must not silently
   turn that into “relax every cheaper band” or enable relaxation in a batch.

4. **Keep one expiry mechanism with two TTL inputs.** The implementation already
   does this: both paths call `reserve`, and `sweep_reservations` reads a single
   `expires`. There are two durations, not two lifecycle machines to maintain.
   Removing the short duration saves one setting and reinstates phantom holds;
   removing the long duration frees actual workers' slots earlier. There is no
   redundant clock to delete without choosing one of those costs.

### Keep, but document better

Highest confidence: distinguish a reserve floor from pacing's soft withholding;
distinguish a binding bucket from a forecast; document the actual fallback
sequence and the short/long TTL call sites. Next: describe inversion as a
heuristic over percentage points, and describe unknown-bucket exceptions by
their exact source strings. Lowest confidence: claiming the present burn sample
guard is sufficient evidence of a usable trend. It is not a reset-identity check.

## Reproduction convention

These are synthetic provider names and model labels, so the examples isolate
policy rather than a changing catalogue. They use the actual functions, with
no network calls and a temporary state file. An omitted provider is absent, not
implicitly funded. Unless stated otherwise, state is empty, there are no holds,
`fallback=True`, and the judgment is `J`. All numbers are percent **used**.

Run this setup from the repository root, then the expressions in the inventory:

```python
import copy, pathlib, tempfile
import rightsize as r

T, D, H = 2_000_000_000, 86400, 3600
r.now = lambda: T
r.STATE = pathlib.Path(tempfile.mkdtemp()) / "state.json"
C = {
    "reserves": {"a": 10, "b": 10, "f": 0},
    "dispatch_cost": {"_default": 1, "f": 0},
    "max_inflight": {"_default": 8},
    "bands": {
        "1": ["a:cheap", "b:cheap", "f:free"],
        "2": ["a:mid", "b:mid", "f:free-mid"],
        "3": ["a:deep", "b:deep"],
    },
    "review_ladder": ["b:review"],
    "agents": {"a": "a", "b": "b", "f": "f"},
    "reservation_ttl_seconds": 1800,
    "reservation_ttl_unconfirmed_seconds": 300,
}
J = {"tier": "implementation", "size": .5, "second_opinion": .1,
     "spec_complete": .9, "destructive": .05}
J3 = {**J, "tier": "design"}

def B(p, left=20*H, id="weekly", source="live"):
    return {"id": id, "percent": p, "resets_at": T+left, "source": source}

def P(*b, status="ok", free=False):
    return {"status": status, "buckets": list(b), "free": free}

def E(p, state=None, c=C):
    r.save_json(r.STATE, state or {})
    return r.eligibility(c, copy.deepcopy(p), record=False)

def run(p, j=J, state=None, c=C, **kw):
    return r.decide(j, c, E(p, state, c), **kw)

def held(expires=T+H, points=0, provider="a"):
    return {"provider": provider, "points": points, "expires": expires,
            "at": T-600, "id": "example", "band": 1, "task": "earlier task"}

base = {"a": P(B(20)), "b": P(B(20, 40*H))}
```

“Only rule” below means a single-rule ablation changes the final primary
provider/model, dispatchability, or placement wave while the other policy rules
remain. It does not mean that judgment and affordability stop being necessary
preconditions. “None” means no primary pick; `decide` can still report the last
attempted band as 3. Evidence from a changed config parameter is labelled as such.

## Rule inventory

### 1. Eligibility: probe failure and reported exhaustion

**Failure and source.** POLICY §2 says a provider is blocked when “the probe
failed, or there is no credential.” `cmd_report:1963` says “rightsize decides
and steps out, so it never sees the worker fail.” A reported quota error supplies
information missing from the reading. These are distinct inputs, not redundant
ways to calculate reserve. Initial policy: `7295c27` (follow the rename).

**Unique states.** `run({**base, 'a': P(B(20), status='error: offline')})`
chooses `b:cheap`; clearing only the error chooses `a:cheap`. `no-credential`
has the same result. Separately, `run(base, state={'exhausted': {'a': T+H}})`
chooses `b:cheap`, while deleting only the exhaustion mark chooses `a:cheap`.
Both providers have 70 usable points and no overrun in each case.

**Overlap and deletion cost.** Reserve and affordability cover neither failure.
Keep both facts; merge their evaluation. `no-session-data`, `no-rate-limits`
and `empty` are not hard failures: with no known buckets they become unknown
and can reach band 3. Also `cmd_report:2027` selects the **first future reset**
in cached bucket order, not necessarily the binding bucket reset promised in
POLICY's “Rule 5.” That discrepancy is not a reason to remove the cooldown.

### 2. Reserve amount versus reserve cutoff

**Failure and source.** POLICY §2: “It keeps a slice of every plan unspent, so
that an escalation later in the day still has somewhere to go.” Initial
`7295c27` describes “eligibility above a per-provider reserve.”

**Unique state for the amount.** `run({'a': P(B(89.5, H))})` returns none:
0.5 usable cannot pay 1. With only `C['reserves']['a']` changed to 0, it chooses
`a:cheap` with 10.5 usable. The reserve amount remains decisive through the
cost check even if its separate `<= 0` veto is removed.

**No unique routing state for the cutoff when costs are positive.** The proof
and zero-cost counterexample are in deletion recommendation 2. Preserve
diagnostics; `eligible=False` itself changes if that check is simply removed.
Deleting the **amount**, by contrast, spends the reserved margin and is unsafe
under the stated policy. The prose is misleading: band 3 cannot spend below
the reserve either. It is a universal floor; the soft overrun gate, not this
floor, is what specifically keeps capacity available for escalated work.

### 3. Binding bucket and soonest-reset ordering

**Failure and source.** `headroom:678`: “Binding bucket = the one with least
usable headroom.” POLICY §5: “Capacity that resets in three hours is about to
be thrown away; a monthly bucket is the scarce thing.” Initial `7295c27` calls
for “spend the bucket that expires first.”

**Unique states.** `{'a': P(B(20), B(95, 27*D, 'monthly'))}` binds on monthly,
usable -5, and has no pick. If binding selection retains the first known
bucket instead of the minimum, usable becomes 70 and `a:cheap` can be chosen
via `relax_pace`; retain the monthly bucket and its pacing calculation in this
ablation. Pacing does not reproduce the hard quota veto.

Independently, use `base` but reverse band 1 to `['b:cheap', 'a:cheap']`.
Soonest-reset ordering still chooses `a:cheap` (20h versus 40h); removing that
sort key chooses `b:cheap`. All admission rules pass.

**Overlap and deletion cost.** Keep minimum usable for nested capacity and a
reset-based ordering policy if expiry remains a goal. In this code, reserve is
one number per **provider**, so minimizing `100-percent-reserve` is exactly
maximizing known percent used. POLICY's “not always the one with the most
spent” is false if “spent” means the percentage the code reads. Equal minima
use first bucket order, not earliest reset. Neither percent nor reset ordering
proves which allowance will run out first in real tokens.

### 4. Two-probe burn rate

**Failure and source.** POLICY §2: “drop a tier before the wall, not at it.”
`c4c2ef1` adds the sampling guard because “the two-probe burn rate extrapolated
a two minute sample across a seven day window and projected five thousand per
cent.” Implementation: `headroom:719-734`.

**Unique state.** Probe `{'a': P(B(30, 3.5*D)), 'b': P(B(20, 4*D))}`,
snapshot `{'snapshots': {'a:weekly': {'at': T-.35*D, 'percent': 19}}}`.
Window pace for `a` projects 60%, so it passes. The 8.4-hour sample is exactly
5% of the week; recent burn projects `30 + 11*3.5/.35 = 140%` and rejects `a`
for cheap work. Result `b:cheap`; removing only the snapshot yields `a:cheap`.
Both have ample reserve and cost coverage. Thus pacing does not subsume burn.

**Overlap and deletion cost.** If the recent rate is no greater than the
whole-window average, its projected end usage cannot exceed the window
projection, so window pacing already covers its veto (when pacing is active).
It adds protection specifically for acceleration, missing window lengths, and
some windows excluded by pacing's guards. Deleting it loses that protection.

**Uncertainty.** A fresh snapshot overwrites the previous one on every fresh
probe (`record_snapshot:637`). Regular probes less than 8.4 hours apart never
produce a valid weekly burn sample; a cached route can later compare against
an older snapshot but also has an older current reading. I have not measured
how often this rule actually fires in production. Snapshots do not store reset
identity: a long-enough sample can still straddle a reset. Keep the claim
“recent-rate heuristic” narrower than “forecast proven from two probes.”

### 5. Window pacing, including batch commitments

**Failure and source.** `0a42b9a`: “Every token spent against the weekly is also
spent against the monthly, so a weekly that looks cheap to empty can be exactly
what exhausts the month.” `bucket_pace:653` and `headroom:715` evaluate every
known window, whereas burn evaluates only the binding one.

**Unique state.** `{'a': P(B(77, 19*H), B(38, 27*D, 'monthly')),
'b': P(B(20, 40*H))}`, no snapshots. Weekly usable is 13; weekly projection is
about 86.8%, monthly projection is 380%. Result `b:cheap`; removing only
`bool(over_pace)` from `headroom`'s returned `overrun` selects `a:cheap`.
Reserves, cost, burn and inversion cannot supply this veto.

Batch update also has its own witness. With `a` at 49.8%, resetting in 3.5d,
and `b` at 0%, resetting in 4d, two `J` tasks choose `a` then `b`.
The first debit adds 1/.5 = 2 points to `a`'s end projection, moving 99.6% to
101.6%. Without `debit:1333` updating forecasts, both choose `a`. This is the
failure `c4c2ef1` describes: “a plan that was just inside its rate stayed there
while the fan-out loaded more onto it.”

**Overlap and deletion cost.** Merge forecast plumbing, not the signals.
Pacing's 5%-elapsed and 2%-spent guards intentionally ignore early noisy states;
they are not interchangeable with a two-probe minimum sample duration. With
only one usable cheap provider, `relax_pace` can cancel this veto for a single
route; it cannot cancel it in `plan` or make `a` beat an available on-pace `b`.

### 6. Band 3 inversion

**Failure and source.** `e2e70ae`: “spending the last of an expiring bucket on
the most expensive rung is the worst available use of it.” `pick:1260` changes
the ranking, not admission.

**Unique state.** `run({'a': P(B(77, 19*H)), 'b': P(B(0, 7*D))}, J3)` chooses
`b:deep`, with 90 usable instead of `a`'s 13. Change only `expensive_band` to
4 and it chooses `a:deep`. Both can pay the band 3 cost of 3. Pacing does not
filter band 3, so it cannot reproduce the inversion.

**Overlap and deletion cost.** Affordability already removes candidates too
small for one dispatch; it cannot rank the remaining candidates by room.
Deleting inversion restores the historical expensive-dispatch choice. Keep,
but document it as a heuristic: raw points are not normalized by dispatch cost
or provider allowance. A plan with 30 points and base cost 10 has one band 3
dispatch left, while a plan with 20 points and base cost 1 has six; inversion
prefers the former. Whether that is desirable is not established by the code.

### 7. Cost cannot be covered

**Failure and source.** Same `e2e70ae`: “A candidate that cannot cover the
dispatch's estimated cost at all is passed over rather than ranked low.”
Implementation: `pick:1254-1259`.

**Unique state.** `{'a': P(B(89.5, H))}` has positive usable 0.5, so reserve
eligibility passes, but a band 1 dispatch costs 1. Result none; deleting just
`room < cost` yields `a:cheap`. Ranking and in-flight limits do not protect
this state. At usable exactly 1 the dispatch is allowed.

**Overlap and deletion cost.** This subsumes the positive-cost reserve cutoff,
not the reserve amount. Keep the cost check as the stronger admission rule.
Unknown usable deliberately skips it at band 3; this is permission to spend
without a measured bound, not evidence that the estimate fits.

### 8. Reservations and in-flight limits

**Failure and source.** `f18c38c`: “100 tasks, 100 dispatches to one plan,
usable unchanged at 9 points throughout.” For limits, the same commit says
“a free provider has no quota to debit.” See `reservation_load:753`,
`eligibility:957`, and `debit:1321`.

**Unique reservation state.** Probe `{'a': P(B(87, H))}`, one live hold of 3
points, limit 8. With the hold, usable becomes 0 and there is no pick; with only
its point debit removed, `a:cheap` is available. One occupied slot is below
the limit. A billed reading and a pending commitment are distinct quantities.

**Unique slot state.** Probe `{'f': P(free=True)}`, eight live reservations on
`f`, each holding zero points. Result none; omit the eight holds or raise only
the slot limit and `f:free` is chosen. Numeric budget checks cannot cover a
provider with no meter. A funded metered provider with ample room also exhibits
this distinction.

**Overlap and deletion cost.** Both can reject a heavily loaded provider, but
neither dominates. Keep both dimensions in one admission record. Counting a
reservation and subtracting its points is intentionally two effects, not an
accidental double check. The quota estimate can double-count work already
included in a later live reading; there is no reconciliation here proving
that every held point remains unbilled.

### 9. The two reservation clocks

**Failure and source.** `f18c38c`: expiry prevents a worker that dies silently
from holding a plan hostage. `c7347ad`: “Those holds now expire on a five minute
clock while a hold taken after a launch has actually run keeps the full thirty.”
Call sites: `hold_capacity:1315` uses 1800; `plan:1386` uses 300;
`sweep_reservations:746` applies the same `expires > now()` test to both.

**Unique states.** With probe `{'a': P(B(20))}` and eight zero-point holds made
600 seconds ago, short-clock expiry is `T-300` and `a:cheap` is available;
long-clock expiry is `T+1200` and no pick is available. Positive one-point holds
give the same result, since 62 points remain. The short clock alone releases
an abandoned plan at that age; the long clock alone keeps actual running work
counted. Independently, eight holds at `expires=T-1` versus `T+1` demonstrate
that removing expiry can leave capacity occupied forever.

**Overlap and deletion cost.** `report --done` and `release_settled` cover only
holds someone can match and release; neither covers a silent abandoned plan
without a report. Waves reset slots in the current plan's local eligibility
copy, not these persistent reservations. Keep both durations. The code does
not store “confirmed,” promote a plan hold after launch, or renew it with a
heartbeat: command path selects the TTL. All future-wave plan holds start
their short clock at planning time. Calling these an actual confirmation
protocol would promise behavior that is absent.

### 10. Waves

**Failure and source.** `f18c38c`: “A wave boundary returns the in-flight slots,
because those workers have finished, and keeps every quota debit.”
`c4c2ef1` adds the initially-full case: “It waits for wave 2.”

**Unique states.** Set `r.judge = lambda spec: J` and override
`max_inflight={'_default': 1}`. With `{'a': P(B(20))}` and empty state,
`r.plan(['x', 'y'], c, probes=p)` places waves `[1, 2]`; `max_waves=1` leaves
the second task unplaced. With one existing live `a` hold, a single task is
placed in wave 2 even though wave 1 placed nothing. With `a` at 88.5% and reset
in 1h, two tasks produce `[1, None]`: the first uses 1 of 1.5 usable points,
and a returned slot cannot fund the next task.

**Overlap and deletion cost.** Keeping debits and returning slots are both
necessary; neither reserve nor expiration schedules the later task. `plan`
passes `fallback=False`, so this wait replaces quality/cost-changing fallback.
This is a schedule, not proof that workers have finished: no completion event
or advancing clock is consulted at a wave boundary. The default cap is 12
waves, including an empty initial wave. The stopping rule prevents spinning
when no slots are held and another wave cannot help.

### 11. `relax_pace`

**Failure and source.** `c4c2ef1`: “Being over pace made the router buy the most
expensive model.” `decide:1501` explains that escalating would spend “the same
strained plan on the priciest rung.”

**Unique state.** `{'a': P(B(40, 6*D))}`, `J`, no other provider. Projection is
280%, room is 50, cost is 1. Current result is `a:cheap` in band 1. Disable only
the relaxed attempt and the ordinary escalation attempt selects `a:deep` in
band 3. Reserve, cost, unknown and slot rules all pass in both versions.

**Overlap and deletion cost.** Pacing itself is outcome-neutral for the
primary pick in this exact one-provider state because relaxation undoes its
veto. That does not make either rule globally dead: add an on-pace `b` and the
ordinary pass chooses it before relaxation. Merge their implementation into a
preference/fallback sequence; do not delete relaxation while keeping the hard
pace skip and escalation. That would reintroduce the measured expensive choice.
Also `relax_pace` bypasses **both** forecast sources because it reads `overrun`;
its name sounds narrower than its behavior.

### 12. Unknown-bucket taint

**Failure and source.** `c4c2ef1` says a missing monthly reading must not make
the weekly look fully known. `7687587` narrows that change: “Treating any
unreadable window as a lost reading disabled the main workhorse the moment
OpenCode declined to report one bucket for a minute.” Current
`headroom:699` taints mixed readings only for `source == 'expired-reading'`.

**Unique state.** `{'a': P(B(20), B(None, 27*D, 'monthly', 'expired-reading')),
'b': P(B(20, 40*H))}` chooses `b:cheap`. Remove only the taint assignment and
it chooses `a:cheap`: `a` still has a numeric usable 70, so the older
`usable is None` gate cannot detect the missing window. This can occur for
Codex when an old reading has one post-reset assumed-zero window and another
expired window. The synthetic labels here simply isolate the same shape.

**Overlap and deletion cost.** A wholly unknown provider was already gated;
mixed unknown/known is the new protection. `no-budget-set` and `unavailable`
do not taint a provider with a known bucket. If **all** buckets are unknown,
the early return still sets `unknown=True`, whatever their sources. Keep the
taint and its explicit exceptions; delete only the duplicate predicate in
recommendation 1. “The reserve covers” an undeclared budget is a policy
assumption in the comment, not an arithmetic guarantee about an unknown cap.

### 13. Escalation and band dropping

**Failure and source.** Initial `7295c27`: “escalate rather than return nothing
when everything cheap is blocked.” `decide:1470` says a task drops to a cheaper
band and then escalates rather than returning nothing; `f18c38c` deliberately
disables this inside batches to avoid sending ordinary work to band 3 merely
because cheap plans are busy.

**Unique escalation state.** `{'a': P(B(None, 3*D, 'weekly', 'expired-reading'))}`
and `J` choose `a:deep` at band 3. Without the final escalation there is no
pick: `relax_pace` does not relax unknown quota. `fallback=False` reproduces
the no-pick result for this band-1 example.

**Unique drop state.** `{'f': P(free=True)}` and `J3` choose `f:free-mid` at band
2 because the band-3 ladder has no `f`. Removing downward attempts leaves no
pick. A separate cost witness needs no free provider: `a` with 2.5 usable
cannot pay band 3's 3 but can pay band 2's 2. Inversion cannot choose an
unaffordable candidate, and escalation does not help a task already at band 3.

**Overlap and deletion cost.** Dropping and escalating can both rescue a
single route but reach different ladders with different costs and admissible
unknown/overrun states. Keep or replace them with an explicitly chosen failure
policy. No code proves that a design/high-stakes task remains adequately served
after dropping to band 1. “Never stranded” is not a guarantee: hard blocks,
cost shortfalls, absent candidates and an incomplete brief still prevent work.
Dropping can also undo a rerun's initial `floor_band`; the floor is a starting
band, not a minimum allowed result.

## Remaining decision-path gates

These are not duplicate quota protections, but are included so that “every
rule” does not silently exclude judgment, review, effort or retry behavior.
The witnesses use the same probe/judgment setup above; source wording is from
POLICY §§3, 4, 6, 7 or the named code comments.

| Rule and stated failure | Concrete unique state | Deletion/overlap judgment |
| --- | --- | --- |
| Tier and size, `band_for:1124`: “Band 1 is the default because price is the burn multiplier.” | On `base`, `J` picks `a:cheap`; `J3` picks `a:deep`; changing only `J.size` to 2 picks `a:mid`. | No quota rule classifies the work. Size and tier meet at a capped band but are not redundant. |
| Spec completeness, `decide:1485`: “a brief that depends on context the worker does not have fails on every provider.” | `run(base, {**J, 'spec_complete': .1})` has `blocked` set despite a funded pick; `.9` permits dispatch. | Keep. The function computes a pick even when blocked; `hold_capacity` and `plan` honor the block. `print_decision`'s JSON branch returns 0 before checking it, so POLICY's blanket exit-code claim is too strong. |
| Second opinion and different provider, `decide:1528`: “A model reviewing its own work is worth less.” | On `base`, `second_opinion=.9` adds `b:review`; `.1` does not. With review ladder `['a:review', 'b:review']`, removing the different-provider filter selects `a:review` by earlier reset. | Keep the separate leg and filter. The early second-opinion branch in `band_for` only adds a reason; the later threshold is what actually adds review. It is not a second band escalation. |
| Review debit, `plan:1421`, `c4c2ef1`: “A review leg is a second dispatch and costs like one.” | Two `J` tasks with second opinion .9, `a` at 20% / 2h and `b` at 88.5% / 10h: first review costs 1 of `b`'s 1.5 usable; second has no review. Without review debit both receive one. | Keep separate provider accounting. Persistent plan holds currently put primary-plus-review points on the primary provider; `hold_capacity` only holds primary. The in-memory fix does not prove cross-process review accounting correct. |
| Irreversibility, `decide:1534`: “confirm with a human before the irreversible step.” | On `base`, changing only destructive to .9 sets `confirm_first=True`. | No quota rule supplies this advisory signal. It does not prevent a caller from launching. |
| Effort, `effort_for:1172`: “a retry wants more than the attempt that just failed.” | Add effort table `{'a': {'1': 'low'}}`; on `base`, destructive .9 changes effort to medium while keeping `a:cheap`. `attempt=1` also raises it. | Keep a distinct dial. Size and destructive share one OR bump in code, although POLICY describes them as separate +1 entries. |
| Rerun floor and exclusion, `rerun:1560`: “a retry cannot quietly land on the same rung that already failed.” | `decide(J,C,E(base),floor_band=2)` starts at `a:mid`. Independently `exclude={'a:cheap'}` makes band 1 select `b:cheap`. | Neither band change nor exclusion implies the other. Later fallback can undo the floor. Multiple same-provider ladder entries are not globally dead: excluding the first on retry can expose the next. |
| Cache must not record a snapshot, `probes_cached:614`: stops “overwriting the burn-rate baseline with a copy of itself.” | Use the burn-only 19→30 witness above. Overwrite its snapshot with the current 30 at `T`, as an inappropriate cache recording would, and `b:cheap` becomes `a:cheap`. | Keep this provenance guard while retaining two-probe burn. It is separate from Codex reading staleness. |
| Codex age/reset normalization, `codex_buckets:373`: an old high reading “blocks a provider that may have reset or been topped up hours ago.” | A Codex weekly reading of 95%, observed `T-7*H`, reset `T+H`, becomes unknown; with no alternative a `J3` task can use it. Without expiration, reserve blocks it. A passed reset instead becomes 0 and rolls forward, permitting cheap work. | Keep the distinction, but the post-reset zero assumption depends on all usage being observable through the discovered transcripts. It is not supplied by pacing or reserve arithmetic. |

## What the evidence does and does not establish

- `python3 test_rightsize.py` passed, including its `adversarial()` cases.
  The file invokes `main()` twice, hence two “all checks passed” lines; these
  are not two independent test suites. No test file was edited.
- Replayed the requested-rule cases with a fixed clock, temporary state,
  injected probes and judgments. In-memory single-function ablations checked
  pace, affordability, relaxed fallback and the redundant unknown predicate;
  config/input ablations checked the other examples. The 198-case floor
  comparison measured primary band/provider/model equivalence, not equivalence
  of diagnostics. No live provider or real state file was used.
- Provenance was checked with `git log -S`, and `--follow` where necessary:
  the initial implementation was `agent_route.py` in `7295c27`; `9ef6aac`
  is its rename, not the introduction of the original policy. Relevant later
  commits are `f18c38c`, `e2e70ae`, `0a42b9a`, `c7347ad`, `c4c2ef1` and
  `7687587`. Read both `c4c2ef1` and its narrowing in `7687587` before claiming
  all missing buckets currently taint a provider.
- Synthetic counterexamples establish logical independence, not operational
  value or correct thresholds. No production frequency data establishes that
  keeping both forecasts pays for their complexity. The strongest honest
  deletion claims are therefore the duplicate predicates and descriptions,
  with explicit preconditions, rather than a whole safety rule declared dead
  because it did not fire in one replay.

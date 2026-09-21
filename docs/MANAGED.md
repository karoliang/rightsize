# Managed admission and leases

The managed path is opt-in and uses caller judgment, fresh account-bound quota
and a separate transactional ledger. `route` and legacy `plan` remain advisory.
This admission slice does not launch a native runtime; adapters are tracked in
#17. Do not run a printed legacy command and describe it as managed execution.

```sh
rightsize managed plan --spec task.md --judgment judgment.json --task-id task-123
rightsize managed admit --spec task.md --judgment judgment.json \
  --task-id task-123 --request-id admission-123
rightsize managed status
rightsize managed status --attempt ATTEMPT_ID
```

Both plan and admit validate the exact task-bound caller judgment before probes.
Neither needs a judge key or remote classification call. Plan makes no lease or
state writes. Admit gets fresh probes outside the compatibility lock, then checks
the account fingerprint, observation age, quota points and slots under admission
locks. Stale or changed observations get one bounded re-probe outside the lock;
continued uncertainty returns a structured wait. CLI exits: 0 for a result, 3 for
wait, 2 for invalid input/state. A wait is not permission to use another account
or a cheaper capability floor manually.

The task ID stays stable across attempts. Each admitted attempt has a decision
ID, reservation ID, launch key and request idempotency key. Repeating a successful
request returns the same attempt without another probe; reusing its key for a
different intent fails. Active attempts and completed-but-unreviewed tasks cannot
be retried with another request key. The initial limit is two attempts per task.
Retries cannot lower the original capability floor. `--floor` may raise it.
No automatic retries or higher-priced quota fallbacks run in this slice.

## Durable state and compatibility

`managed.sqlite3` lives beside legacy `state.json`. SQLite `user_version=1` owns
managed attempts, tasks, quota denials and lifecycle events. First initialization
is serialized with writers; transactions use `BEGIN IMMEDIATE`. The ledger file
is created with mode 0600. Corrupt, unversioned-existing or future-version state
fails closed rather than becoming an empty commitment set. No existing JSON
records are deleted or automatically migrated. Versioned migration and rollback
remain #19.

Quota costs round up to integer millionths of a percentage point; capacity rounds
down. A native home is an execution context, not necessarily a distinct quota
account. Provider-confirmed quota identity pools multiple homes using one account;
when identity is unavailable, commitments pool conservatively across the provider.
A known account also counts any unattributed commitments on that provider.
Admission counts every active attempt on the account, plus provider-wide
legacy holds whose account is unknown. Completion returns a slot, but its points
still count against observations older than that completion. A newer quota read
is needed before those spent points can disappear from the snapshot budget.

Managed admission takes the legacy compatibility lock before the ledger lock.
Legacy pre-reservation refuses a provider with active managed commitments, and
legacy quota views include those commitments. This prevents concurrent local
pre-reservation writers from racing a managed admission. Untracked external
launches remain external and are not admission-enforced. Existing provider-wide
denials remain conservative until reconciled; a managed account denial clears
only on healthy live evidence newer than the denial, never just a reset estimate.
An unattributed denial requires the same context and fingerprint to clear; changing
homes or credentials cannot silently erase it.

Unknown quota waits even for expensive work. Native Claude estimates require
usable declared-budget telemetry; vault-to-runtime account delivery and Orca
identity are still gated pending #17. Unsupported paths return a wait instead of
claiming a verified launch. Destructive judgments also wait for explicit approval.

## Adapter lifecycle contract

`managed_router.launch_once` persists the launch claim before invoking an adapter
with the immutable admitted attempt and launch key. Concurrent/repeated calls
cannot invoke the adapter twice. The fake adapter tests establish three outcomes:

- Exact launch receipt: preserve actual dispatch/session/model/effort/account
  separately from the recommendation and enter `started`.
- Positive proof no launch happened: `launch_failed`, releasing the hold once.
- Timeout, exception or malformed/mismatched receipt: `reconciling`, retaining
  points and slots. No automatic repeat launch or release.

The ledger records first output, cancellation request, confirmed cancellation,
typed terminal failures and completion. Native completion is not acceptance:
`accepted` and `rejected` require a separate reviewer evidence reference. Lifecycle
events are idempotent; a repeated ID with different evidence fails. Adapter
evidence is a trusted input, never arbitrary model text. Error messages from a
launcher are not retained as evidence because they may contain credentials.

Leases can be renewed only with the matching launch key and a bounded duration.
Expiry moves active attempts to `reconciling` and retains their commitments; it
does not prove a process stopped. Restart/status inspection is read-only. Adapters
must confirm ownership and termination before releasing uncertain work.

Offline proof: `python3 test_managed_ledger.py` covers 12-process point/slot
contention, concurrent initialization, killed transactions, expiry, stale reads,
denial recovery, account switches, receipts, review, cancellation and corruption.
`python3 test_managed_router.py` exercises the CLI and fake launcher, zero-write
planning, caller validation, lock-free re-probing and legacy compatibility.

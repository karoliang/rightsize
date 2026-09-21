# Versioned state migration and rollback

This is an opt-in generation-2 compatibility path. It does not promote the router
or complete the live pilot gates. Existing installations remain on `state.json`
until an explicit apply. No live state was migrated during implementation;
the real dry run reported zero active holds/managed attempts and one retained
legacy denial scope.

```sh
rightsize managed migrate             # locked preview; no state/ledger changes
rightsize managed migrate --apply     # backup, preserve, fence, activate
rightsize managed rollback            # inspect outstanding work
rightsize managed rollback --apply    # disable admission; wait or restore
```

Preview may create the common empty coordination lock file and parent directory;
it creates no state, backup, ledger or reservation. Apply rechecks under the lock.
All commands print bounded counts/status, not legacy task bodies or credentials.

## Ownership and preservation

Generation 2 retains the SQLite lifecycle ledger and its history. The migration
changes the legacy JSON writer generation, not the SQLite schema. Under the
shared legacy lock and ledger transaction it:

1. Validates existing legacy state and managed SQLite. Invalid evidence is never
   treated as an empty commitment set. Apply copies invalid legacy/SQLite bytes
   to a private quarantine and retains the original; preview does not quarantine.
2. Takes a byte-exact mode-0600 JSON backup under `migrations/<id>/state.json`,
   with SHA-256 source/candidate digests in a versioned transition manifest.
   Unknown history, denials, snapshots and existing fields survive unchanged.
3. Marks currently active legacy holds as unattributed. These holds continue to
   count points/slots after their old TTL and cannot be removed by heuristic
   brief/worktree matching. They require an explicit completion report from the
   caller that has verified the worker stopped. `report --done --id ID` retains
   that existing caller-report contract; it is not independent native proof.
4. Writes `state.v2.json` and atomically exchanges the old `state.json` file with
   a directory fence. Old writers cannot replace a directory with their JSON
   temporary file. There is no missing-file interval in which they can recreate
   an empty state. The original file and separate backup remain private.
5. Publishes the active generation. Current CLI invocations resolve the new file
   and share `state.json.lock`; invocations that selected the old generation
   before the change are rejected when they acquire the lock.

The minimum compatible writer is generation 2, identified by this migration
command's `writer_generation: 2` result. The implementation commit is linked in
GitHub #19. Use the current `rightsize` entry point for old route/plan/report
invocations. Do not point old code directly at `state.v2.json`, bypass the fence,
or run an unrelated script that writes the ledger directly. This is a compatible
CLI contract, not a sandbox against a process deliberately modifying private
state. Advisory commands and untracked external launches remain advisory.

Atomic exchange uses the native macOS/Linux operation through Python's standard
library. Unsupported filesystems/platforms fail before cutover rather than using
a non-atomic rename sequence. The normal package still has no dependencies.

## Interrupted transitions

The manifest records `prepared`, `active`, `restoring`, or `rolled-back`.
Ordinary commands reject incomplete transitions. Rerun the same apply command
to recover a prepared migration or restoring rollback. The operation checks
backup digests and the current fence/source state before continuing. Changed
evidence is an error, never permission to overwrite it.

Repeated successful apply returns the existing migration. A later migration
after rollback creates a new backup generation and preserves the previous v2
state. Recovery does not query a model, change an account, or settle a worker.

## Rollback

Rollback first durably disables new managed admissions. Any admitted, launching,
running, cancelling or reconciling managed attempt, or any remaining legacy hold,
returns a structured wait. TTL expiry and an unconfirmed cancellation cannot
pass this gate. Continue native cancellation/reconciliation and caller-verified
legacy completion through the compatible CLI, then retry rollback.

Once every hold is settled, rollback archives the final v2 JSON and atomically
restores the original JSON backup. The SQLite ledger, outcomes and reviews stay
intact, with admissions disabled. Older denials and reservation records in the
backup may return conservatively; restoration does not claim those old records
are fresh observations. A previously absent source becomes an empty JSON file.
No active work is erased to downgrade. Rerunning rollback is idempotent. A new
explicit migration is required to enable managed admissions again.

## Evidence and limits

Offline tests exercise real filesystem exchange and SQLite: byte-exact restore,
legacy CLI report compatibility, old-writer fencing, migrated holds after TTL,
no heuristic release, disabled admission while waiting, corrupt evidence,
future/broken manifests, backup tampering and crashes before/after each exchange.
The macOS implementation is exercised locally and Linux in CI. Native-adapter
validation and the 20-task paired pilot remain separate gates in #14/#17/#19.

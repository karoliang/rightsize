# state.json under concurrent access

`~/.local/state/rightsize/state.json` is a single JSON document that five
independent kinds of process read and write, with no lock and no atomic write.

```python
def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default          # every caller passes {}

def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
```

`rightsize.py:154-163`. Two properties of those six lines drive everything
below.

**`write_text` truncates first.** It opens the file `"w"`, which sets the
length to zero, and then writes through an 8192-byte buffer. The live document
is 17 KB and grows to about 47 KB once `decisions` fills its 200-entry window,
so one save is several `write()` syscalls, and the file is empty or
half-written for the whole span between them.

**`load_json` cannot tell a broken file from an empty one.** It catches
`ValueError` and returns `{}`. A process that reads mid-write does not fail; it
gets an empty state, proceeds, and then writes `{}` plus its own field back
over the entire document. Nothing logs this.

## Who writes it, concurrently

| Caller | Frequency | Functions |
| --- | --- | --- |
| PreToolUse hook (`hooks/claude_pretooluse.py`) | every Bash tool call that looks like a worker launch | `probes_cached`, `record_snapshot`, `log_decision`, `reserve` via `route --reserve` |
| PostToolUse hook | every matching launch, straight after the PreToolUse one | `reserve` |
| `rightsize plan --reserve` | `reserve` + `log_decision` once **per task**, in a tight loop (`rightsize.py:1296-1302`) | `reserve`, `log_decision`, `record_snapshot` |
| Four worker terminals | whenever their own agent routes or reports | all of them |
| `rightsize report --from-orca` | one `release()` per settled reservation, each its own read-modify-write | `release`, `sweep_reservations` |
| launchd refresh (`com.rightsize.refresh.plist`) | on a timer, unattended | `probes_cached`, `record_snapshot` |

Nothing coordinates them. `plan`'s loop alone issues one full document
rewrite per task, so a 12-task plan rewrites a 47 KB file 24 times in a few
hundred milliseconds while four workers read it.

## Three failure classes

**A. Lost update.** Every writer does `state = load_json(...)` → mutate → 
`save_json(state)`. Two writers that read the same version each write their own
version; the second erases the first's change. The change that is lost is not
only the field being written — the whole document reverts, so a lost `reserve`
also reverts `exhausted`, `decisions` and `probe_cache` to whatever the losing
writer happened to read. Fields that are never touched by the same code path
still clobber one another.

**B. Torn read, silently converted to a reset.** A reader that lands between
the truncate and the last write gets a `ValueError`, `load_json` hands it `{}`,
and the next `save_json` from that process replaces every field with one.

**C. Permanent corruption.** Process A truncates and starts writing a 47 KB
document. Process B truncates and writes a 17 KB one. A's remaining buffered
chunk then writes at A's own file offset, past the end of B's document. The
result is a valid JSON object followed by the tail of another one:
`json.JSONDecodeError: Extra data`. That file never parses again, so
`load_json` returns `{}` from then on and the first write resets the file to a
single field. Reproduced on every run of the demo below.

## Field by field

### `probe_cache`

Written by `probes_cached` (`rightsize.py:584-586`) after a live probe, and by
`cmd_probe` (`1647-1649`) to warm the cache. Read by `probes_cached`,
`cmd_report` and the quota-error path in `cmd_report`.

`probes_cached` re-reads the state immediately before writing, which narrows
the window but does not close it:

```
hook A: probes = probe_all(...)          # ~2 s of HTTP
hook A: state = load_json(STATE)         # re-read, has B's reservation? not yet
plan B: state = load_json(STATE)
plan B: append reservation, save_json    # reservation now on disk
hook A: state["probe_cache"] = ...; save_json(state)   # B's reservation gone
```

The reservation vanishes while its worker runs. In the other order, the probe
cache is what disappears: the next router re-probes (four HTTP calls it should
not need), and `rightsize report <name> --quota-error` finds
`probe_cache.probes[name].buckets` empty, so it silently falls back to the flat
`--minutes` cooldown instead of the provider's own `resets_at`
(`rightsize.py:1848-1853`). A provider that told us exactly when it resets gets
a guessed time.

### `snapshots`

Written only by `record_snapshot` (`594-604`), which is called from
`eligibility(record=True)` on every fresh route. It returns the previous map
and overwrites it with the current one in the same breath.

The interleaving that matters is not a missing field, it is a dead detector.
`headroom` computes a burn rate only when the stored baseline is old enough:

```python
elapsed = now() - before["at"]
climb = bucket["percent"] - before["percent"]
if elapsed > 60 and climb > 0:        # rightsize.py:669-673
```

With four workers and a hook each calling `record_snapshot`, every call stamps
`snapshots[*]["at"] = now()`. The baseline is never more than a few seconds
old, `elapsed > 60` is false on every route, and the `overrun` flag derived
from the climb never fires again. The only surviving overrun signal is
`bucket_pace`, which is a different test. Separately, when two routes race, the
loser's snapshot is discarded and the `previous` it already returned describes
a reading that is no longer on disk.

### `exhausted`

Written by `mark_exhausted` (`849-852`, after a quota error) and by
`cmd_report --clear` (`1829-1831`). Read by `exhausted_until` on every
eligibility pass.

```
worker 1: codex returns 429
worker 1: mark_exhausted("codex", until=18:40)   # reads state, writes it
worker 2: state = load_json(STATE)               # read 2 s BEFORE that write
worker 2: reserve("opencode", ...)  → save_json(state)
```

Worker 2's copy has no `exhausted` entry for codex, so the mark is erased. The
next route finds codex eligible and dispatches straight back into the quota
error. Reverse the order and `--clear` loses: a provider stays blocked after
the operator cleared it, until someone clears it again.

### `reservations`

The field under the most pressure, and the one whose whole purpose is to be
correct under concurrency:

> A quota reading says what has been billed, not what is about to be. Fan out a
> hundred workers inside one cache window and every one of them sees the same
> untouched headroom and picks the same provider. A reservation is the
> difference between those two questions. — `reservation_load`, `rightsize.py:695-700`

Written by `reserve` (`706-724`), `release` (`728-740`), `release_settled`
(`805-829`); `sweep_reservations` (`686-689`) mutates whatever copy its caller
holds and so is written by all three.

- **Lost reserve.** Two `reserve` calls read the same list of length *n*, each
  appends its own entry, each writes a list of length *n+1*. One reservation is
  gone while its worker is running. `reservation_load` then under-reports both
  points and in-flight count, the next route sees headroom that is already
  spent, and the fan-out overcommits the provider — precisely the failure
  reservations exist to prevent. `plan --reserve` makes this the common case,
  not the rare one: its loop reserves each task in turn while the hooks reserve
  alongside it.
- **Lost release.** Symmetric, and harder to spot. A released reservation
  reappears and holds its points for the full `reservation_ttl_seconds` (1800
  by default). `rightsize report` then prints "N reservations in flight, oldest
  … ago" for workers that finished, and headroom stays understated for half an
  hour.
- **`release_settled` compounds both.** It sweeps its own copy of the list and
  then calls `release()` per match, each of which does its own independent
  read-modify-write (`820-826`). Reservations created between the outer read
  and an inner read survive the sweep; a `release()` that loses a race leaves
  its entry on disk while the function still reports it in `released`. The
  caller is told capacity was returned that was not.
- **Resurrected expiries.** `sweep_reservations` drops expired entries in the
  copy that is written last. A writer that read before an expiry writes the
  expired entries back, so a reservation can outlive its TTL.

### `decisions`

Written only by `log_decision` (`2124-2147`), from `route` and once per task
inside `cmd_plan`'s wave loop (`1300`). Capped at the last 200 entries. Read by
`audit`.

Concurrent appends lose entries the same way reservations do: `plan` appends
decisions for tasks 1..n in a loop while a hook-driven `route` appends its own,
and the last writer's list wins. The damage lands in `audit`, which compares
each recommendation against the opencode session database and prints:

```
no session — nothing ran in a worktree of that name; not dispatched, or dispatched elsewhere
```

A decision that was never written is indistinguishable from a pick that was
ignored. That distinction is the only reason the log exists
(`2124-2132`), so the race destroys exactly the evidence the feature was built
to supply.

### `openrouter_free`

Written by `count_free_request` (`532-538`), read by `free_requests_today`
(`521-529`).

```python
counter = state.get("openrouter_free", {})
count = counter.get("count", 0) if counter.get("day") == day else 0
state["openrouter_free"] = {"day": day, "count": count + 1}
```

Two concurrent `report <name> --free-request` calls both read *k* and both
write *k+1*: one request is uncounted. The count feeds
`percent = round(100.0 * used_today / cap, 1)` in `probe_openrouter`
(`510-512`), so the free-requests bucket reads emptier than it is and routing
keeps sending work at a cap that has already been hit. The day field races too:
a process that read yesterday's counter and writes late restores
`day = yesterday`, and today's count restarts from a stale base.

## Demonstration

`/tmp/rs-race/race.py`. Runs against a throwaway `HOME`, so the real state file
is untouched. Twelve processes spin until a shared wall-clock instant and then
each perform one `reserve()` (then one `log_decision()`), against a seeded
document the size of a real one.

```
==============================================================
1. reserve(): N processes, one reservation each
==============================================================
   reserve() calls that returned an id : 12
   reservations actually in state.json : 1
   LOST                               : 11
   survivors: task 2

==============================================================
2. log_decision(): N processes, one decision each
==============================================================
   log_decision() calls               : 12
   decisions actually in state.json   : ALL LOST - Extra data: line 14 column 1 (char 200) (202 bytes on disk)

==============================================================
3. torn read: what load_json() hands back mid-write
==============================================================
   reads while another process saved  : 14736
   complete JSON                      : 14161
   truncated to zero bytes            : 575
   half-written (partial JSON)        : 0
   distinct file sizes observed       : 0 .. 47112 bytes
   -> load_json returns {} for 575 of those reads, and the caller
      then save_json()s that {} over the whole document.
```

Eleven of twelve reservations lost (class A). The decisions run ended with a
file that no longer parses (class C) — `Extra data` is the tail of a longer
document surviving past a shorter one written into the same truncated file;
from that moment every `load_json` returns `{}`. Roughly 4 % of reads during a
three-second write storm saw a zero-length file (class B), each of which would
have been handed `{}` and would have written that `{}` back.

Both lost-update and corruption reproduce on every run.

## The smallest fix

Keep the format, add no dependency: **`os.replace` in `save_json`, plus an
`fcntl` lock held across each read-modify-write.** Both are stdlib, together
about twenty lines, and no on-disk change.

```python
# save_json: write beside the target, then rename over it
tmp = path.with_suffix(".tmp")
tmp.write_text(json.dumps(value, indent=2) + "\n")
os.replace(tmp, path)

# and one context manager the eight mutators use instead of load/save pairs
@contextmanager
def locked_state():
    ...  # open a lock file, fcntl.flock(LOCK_EX), load inside, yield, save inside, release
```

`reserve`, `release`, `release_settled`, `mark_exhausted`, `record_snapshot`,
`log_decision`, `count_free_request`, `probes_cached` and `cmd_probe`'s cache
warm each become a `with locked_state() as state:` block. `cmd_report --clear`
too.

### Options considered

**`os.replace` alone.** `rename(2)` is atomic, so a reader sees either the old
document or the new one and never a partial or zero-byte file. Two lines inside
`save_json`, no other call site changes, and it kills classes B and C outright
— including the permanent corruption the demo produced, which is the most
expensive failure here because it is silent and unrecoverable. It does nothing
for class A: two `reserve` calls still lose one, they just lose it cleanly.
Required either way, because it is what makes an *unlocked* reader safe, and
readers are everywhere (`reservation_load`, `exhausted_until`, `audit`,
`cmd_report`).

**`fcntl.flock(LOCK_EX)` around the read-modify-write.** The only option that
fixes class A, which is the one that misroutes work. Costs:
- Advisory only. It binds processes that take it; a `jq`, an editor, or a
  future caller that writes without locking is unaffected.
- `flock` is held per open file description, so it must live in one module-level
  helper rather than being sprinkled ad hoc.
- A blocking acquire can stall the PreToolUse hook, which runs on *every* Bash
  tool call. Use a bounded wait (`LOCK_NB` with a short retry loop) and, on
  timeout, degrade to the hook's existing silent-failure path rather than
  hanging the user's shell.
- Without `os.replace`, a non-locking reader still gets a torn read, so the
  lock alone would force every pure reader to lock too — more call sites, and
  more chances for the hook to block. Pairing the two avoids that.
- POSIX only. rightsize ships a launchd plist and reads `~/.codex` rollouts, so
  macOS-only is already true.

**One file per reservation** (`reservations/<id>.json`, `unlink` to release).
`open(O_CREAT|O_EXCL)` and `unlink` are atomic on POSIX, so the field under the
most pressure needs no lock at all, and a crashed worker leaves exactly one
stale file. Rejected:
- It changes the on-disk format, which the brief rules out.
- It fixes one field. `decisions`, `exhausted`, `snapshots`, `openrouter_free`
  and `probe_cache` still race, so this is not *instead of* the lock, it is a
  second mechanism beside it.
- `reservation_load` runs on every eligibility pass and would become a
  directory scan plus *n* opens instead of one read of a document already in
  memory.
- `report` and `audit` lose the "read one file, see the whole world" property
  they currently rely on.

### How to check the fix

Re-run `/tmp/rs-race/race.py`. Section 1 must report 12 reservations kept and 0
lost, section 2 must report 12 decisions and no parse error, and section 3 must
report 0 zero-byte and 0 half-written reads.

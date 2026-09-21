"""Owned foreground execution in isolated Git worktrees; no shell evaluation."""

import fcntl
import hashlib
import os
from pathlib import Path
import subprocess

from managed_ledger import Ledger, LedgerError
from managed_router import ledger_path
import native_codex
import native_claude
import native_opencode_execution


def git(arguments, cwd):
    try:
        result = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True,
                                text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LedgerError("Git workspace operation unavailable") from exc
    if result.returncode:
        raise LedgerError("Git workspace operation failed; no native launch requested")
    return result.stdout.rstrip("\n")


def worktree(repo, target):
    repo = Path(git(["rev-parse", "--show-toplevel"], repo)).resolve()
    entries = git(["worktree", "list", "--porcelain", "-z"], repo).split("\0")
    paths = [Path(entry[9:]).resolve() for entry in entries
             if entry.startswith("worktree ")]
    if target.exists():
        if target.resolve() not in paths or target.resolve() == paths[0]:
            raise LedgerError("existing execution path is not the owned isolated worktree")
    else:
        git(["worktree", "add", "--detach", str(target), "HEAD"], repo)
    return target.resolve()


def execute(args, config, api, *, adapter_factory=None):
    ledger = Ledger(ledger_path(api))
    attempt = ledger.read(args.attempt)
    if not attempt:
        raise LedgerError("attempt not found")
    if attempt["state"] != "admitted":
        return {"status": "existing", "attempt": attempt}
    if attempt["pick"]["provider"] not in ("codex", "claude", "opencode"):
        raise LedgerError("native adapter not yet enabled for this provider")
    with Path(args.spec).open("rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024 or hashlib.sha256(raw).hexdigest() != attempt["spec_hash"]:
        raise LedgerError("execution spec does not match admitted task")
    from context_manifest import for_execution
    _, prompt = for_execution(args, raw.decode("utf-8"), attempt["context_hash"])
    root = api.STATE.parent / "executions" / attempt["attempt_id"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "owner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "existing", "attempt": ledger.read(args.attempt)}
        try:
            attempt = ledger.read(args.attempt)
            if attempt["state"] != "admitted":
                return {"status": "existing", "attempt": attempt}
            cwd = worktree(Path(args.repo).resolve(), root / "worktree")
            output_path = root / "output.txt"
            fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                def write(text):
                    output.write(text + "\n")
                    output.flush()
                adapter_class = {"codex": native_codex.Codex, "claude": native_claude.Claude,
                                 "opencode": native_opencode_execution.OpenCode}[attempt["pick"]["provider"]]
                adapter = (adapter_class(ledger, config, prompt, cwd, sandbox=args.sandbox)
                           if adapter_factory is None else adapter_factory(
                               attempt["pick"]["provider"], ledger, config, prompt, cwd, sandbox=args.sandbox))
                result = native_codex.run(ledger, args.attempt, adapter, timeout=args.timeout, output=write)
            return {**result, "worktree": str(cwd), "output_artifact": str(output_path),
                    "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest()}
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def reconcile(args, config, api, *, opencode_adapter_class=None):
    ledger = Ledger(ledger_path(api))
    attempt = ledger.read(args.attempt)
    if not attempt:
        raise LedgerError("attempt not found")
    if attempt["pick"]["provider"] not in ("codex", "claude", "opencode"):
        raise LedgerError("native reconciliation not yet enabled for this provider")
    root = api.STATE.parent / "executions" / attempt["attempt_id"]
    if not root.is_dir():
        return {"status": "existing", "attempt": attempt}
    with (root / "owner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "owner-running", "attempt": attempt}
        try:
            adapter = {"codex": native_codex, "claude": native_claude,
                       "opencode": native_opencode_execution}[attempt["pick"]["provider"]]
            if attempt["pick"]["provider"] == "opencode" and opencode_adapter_class is not None:
                return adapter.reconcile(ledger, args.attempt, config, adapter_class=opencode_adapter_class)
            return adapter.reconcile(ledger, args.attempt, config)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)

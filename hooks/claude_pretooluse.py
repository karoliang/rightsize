#!/usr/bin/env python3
"""Claude Code PreToolUse hook: route before a subagent is launched.

Registered against the Bash tool, this watches for a command that starts a
worker (Orca's `worker-start`, `worktree create`, `terminal create`, or a bare
`opencode`/`codex`/`claude` run) and answers the question the agent is about to
guess at: which provider and model that task should get, given what each plan
has left right now.

It is advisory by construction. It prints one block of context and exits 0,
always, whatever happens: a routing helper that can block a dispatch is a
routing helper that can strand a run.

Install (a second entry alongside whatever else owns PreToolUse):

    {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
      {"type": "command", "command": "python3 /path/to/rightsize/hooks/claude_pretooluse.py"}]}]}}
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RIGHTSIZE = ROOT / "rightsize"
TIMEOUT = 20

# Commands that mean "a worker is about to start". Extend freely: a miss here
# costs nothing but the advice.
LAUNCHERS = re.compile(
    r"orca\s+(orchestration\s+worker-start|worktree\s+create|terminal\s+create)"
    r"|^\s*(opencode|codex|claude)\s+(run|exec|-p)\b"
)
# The task text, in the spellings the launchers use.
SPEC = re.compile(r"--(?:spec|prompt|task|message)[= ]+(\"[^\"]*\"|'[^']*'|\S+)")
CAT = re.compile(r"\$\(\s*cat\s+([^)]+?)\s*\)")
# Do not route a rightsize call. Matched as an invocation, not as a substring:
# a path can contain the word (a worktree named after this repo, for one).
SELF = re.compile(r"(?:^|[|;&]\s*|\s)rightsize\s+(?:route|probe|report|refresh|deals|models)\b")


def spec_text(command: str) -> str | None:
    match = SPEC.search(command)
    if not match:
        return None
    value = match.group(1).strip("\"'")
    inner = CAT.search(value)
    if inner:
        try:
            return Path(inner.group(1).strip("\"'")).expanduser().read_text()
        except OSError:
            return None
    return value or None


def advise(spec: str) -> str | None:
    try:
        result = subprocess.run(
            # --reserve books this dispatch's estimated cost, so a fan-out of
            # twenty workers does not hand all twenty the same untouched
            # headroom. It expires on its own if the launch never happens.
            [str(RIGHTSIZE), "route", "--task", spec, "--json", "--reserve"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        decision = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    pick = decision.get("pick")
    if not pick:
        return "rightsize: no provider has headroom for this; every candidate is blocked."
    judgment = decision.get("judgment", {})
    lines = [
        f"rightsize says: band {decision['band']} -> --agent {decision['agent']}, "
        f"model {pick['model']}" + (f", effort {pick['effort']}" if pick.get("effort") else ""),
        f"  judged {judgment.get('tier')} by {judgment.get('source')}",
    ]
    for note in decision.get("notes", []):
        if "chosen:" in note and not note.startswith("review:"):
            lines.append(f"  {note}")
    if decision.get("review"):
        lines.append(f"  worth a second opinion from {decision['review']['provider']} "
                     f"{decision['review']['model']} when it comes back")
    if decision.get("confirm_first"):
        lines.append("  this task has an irreversible step: confirm with the user before it runs")
    if decision.get("blocked"):
        lines.append(f"  WARNING {decision['blocked']}")
    lines.append("  Use this agent and model unless you have a stated reason not to.")
    if decision.get("reservation"):
        lines.append(f"  when this worker finishes: rightsize report {pick['provider']} --done")
    return "\n".join(lines)


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        print("{}")
        return 0
    command = (event.get("tool_input") or {}).get("command", "")
    if (
        event.get("tool_name") != "Bash"
        or SELF.search(command)
        or not LAUNCHERS.search(command)
        or not shutil.which(str(RIGHTSIZE))
    ):
        print("{}")
        return 0
    spec = spec_text(command)
    context = advise(spec) if spec else None
    if not context:
        print("{}")
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": context,
    }}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # never let a routing hint break a dispatch
        print("{}")
        raise SystemExit(0)

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

# A worker launch, recognised only at the start of a command segment. Text that
# merely contains a launch line (an echo, a grep, a config file being written)
# is not a launch, and judging it spends a request on a question nobody asked.
LAUNCH = re.compile(
    r"^(?:[\w./-]*/)?(?:"
    r"orca\s+(?:orchestration\s+worker-start|worktree\s+create|terminal\s+create)"
    r"|(?:opencode|codex|claude)\s+(?:run|exec|-p)\b"
    r")"
)
# Commands whose arguments are text about other commands, never a launch.
QUOTING = {"echo", "printf", "cat", "grep", "rg", "sed", "awk", "jq", "diff", "head", "tail",
           "less", "python", "python3", "node", "git", "tee", "rightsize"}
SEPARATORS = re.compile(r"[\n;&|]+")
HEREDOC = re.compile(r"<<-?\s*[\"']?(\w+)[\"']?")
# The task text, in the spellings the launchers use.
SPEC = re.compile(r"--(?:spec|prompt|task|message)[= ]+(\"[^\"]*\"|'[^']*'|\S+)")
CAT = re.compile(r"\$\(\s*cat\s+([^)]+?)\s*\)")
# A worker can be started from a task id instead of a brief, which is how a
# dependency graph dispatches. The brief still exists; it is in the task.
TASK_ID = re.compile(r"--task[= ]+(task_[A-Za-z0-9]+)")


def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies.

    Their lines are data being written to a file, not commands. Writing a
    document that contains a launch line is not launching anything, and every
    line of a heredoc starts at column zero, so without this a docs commit reads
    as a dispatch.
    """
    lines, out, marker = command.splitlines(), [], None
    for line in lines:
        if marker is not None:
            if line.strip() == marker:
                marker = None
            continue
        out.append(line)
        found = HEREDOC.search(line)
        if found:
            marker = found.group(1)
    return "\n".join(out)


def launch_segment(command: str) -> str | None:
    """The part of the command line that actually starts a worker, if any."""
    for segment in SEPARATORS.split(strip_heredocs(command)):
        segment = segment.strip()
        if not segment:
            continue
        first = segment.split()[0].rsplit("/", 1)[-1]
        if first in QUOTING:
            continue
        if LAUNCH.match(segment):
            return segment
    return None


def spec_from_task(command: str) -> str | None:
    """The brief behind a --task id, asked of the orchestrator."""
    found = TASK_ID.search(command)
    if not found:
        return None
    try:
        result = subprocess.run(["orca", "orchestration", "task-list", "--json"],
                                capture_output=True, text=True, timeout=15)
        payload = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for task in ((payload.get("result") or {}).get("tasks") or []):
        if task.get("id") == found.group(1):
            return task.get("spec")
    return None


def spec_text(command: str) -> str | None:
    match = SPEC.search(command)
    if not match:
        return spec_from_task(command)
    value = match.group(1).strip("\"'")
    # `--task task_abc` names the brief rather than carrying it.
    if re.fullmatch(r"task_[A-Za-z0-9]+", value):
        return spec_from_task(command)
    inner = CAT.search(value)
    if inner:
        try:
            value = Path(inner.group(1).strip("\"'")).expanduser().read_text()
        except OSError:
            return None
    return value if usable_brief(value) else None


def usable_brief(value: str) -> bool:
    """Is this an actual task, or the shape of one?

    Template placeholders and one-word arguments come from config files, docs
    and examples. Judging those spends a request to answer a question nobody
    asked, and the answer is noise in the transcript either way.
    """
    text = value.strip()
    if len(text) < 20 or "{" in text:
        return False
    return not (text.startswith("<") and text.endswith(">"))


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
    segment = launch_segment(command) if event.get("tool_name") == "Bash" else None
    if not segment or not shutil.which(str(RIGHTSIZE)):
        print("{}")
        return 0
    spec = spec_text(segment)
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

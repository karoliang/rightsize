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

It runs on two events. On PreToolUse it routes and prints the decision. On
PostToolUse it books the capacity, and only then, because a dispatch that was
drafted and never run must not hold a plan: three such phantoms once blocked
Codex while it had eighty-five points free.

    {"hooks": {
      "PreToolUse":  [{"matcher": "Bash", "hooks": [{"type": "command",
         "command": "python3 /path/to/rightsize/hooks/claude_pretooluse.py"}]}],
      "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
         "command": "python3 /path/to/rightsize/hooks/claude_pretooluse.py"}]}]}}
"""

import json
import re
import shlex
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
HEREDOC = re.compile(r"<<-?\s*[\"']?(\w+)[\"']?")
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
    candidates = []
    for segment in command_segments(command):
        segment = segment.strip()
        if not segment:
            continue
        first = segment.split()[0].rsplit("/", 1)[-1]
        if first in QUOTING:
            continue
        if LAUNCH.match(segment):
            candidates.append(segment)
    # Setup commands in the shipped multiline launcher have no task brief.
    return next((s for s in candidates if re.match(
        r"^(?:[\w./-]*/)?orca\s+orchestration\s+worker-start\b", s)),
        candidates[0] if candidates else None)


def command_segments(command: str) -> list[str]:
    """Split shell operators without treating quoted task text as commands."""
    lexer = shlex.shlex(strip_heredocs(command), posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments, tokens = [], []
    try:
        for token in lexer:
            if token and all(c in ";&|\n" for c in token):
                if tokens:
                    segments.append(shlex.join(tokens))
                    tokens = []
            else:
                tokens.append(token)
    except ValueError:
        return []
    if tokens:
        segments.append(shlex.join(tokens))
    return segments


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
    value = flag_value(shlex.split(command), "--spec", "--prompt", "--task", "--message")
    if not value:
        return spec_from_task(command)
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


def flag_value(tokens: list[str], *names: str) -> str | None:
    for i, token in enumerate(tokens):
        for name in names:
            if token == name and i + 1 < len(tokens):
                return tokens[i + 1]
            if token.startswith(name + "="):
                return token[len(name) + 1:]
    return None


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


def launch_receipt(command: str, segment: str, event: dict) -> dict | None:
    """Read launch arguments and the receipt, not a fresh routing prediction."""
    response = event.get("tool_response") or {}
    if not isinstance(response, dict):
        return None
    if (response.get("success") is False or response.get("is_error") is True
            or response.get("exit_code", response.get("exitCode", 0)) != 0):
        return None
    payload = response
    if response.get("stdout"):
        try:
            payload = json.loads(response["stdout"])
        except (ValueError, TypeError):
            pass
    if isinstance(payload, dict) and payload.get("ok") is False:
        return None
    args = shlex.split(segment)

    agent, model = flag_value(args, "--agent"), flag_value(args, "--model", "-m")
    if Path(args[0]).name in ("codex", "claude", "opencode"):
        agent = Path(args[0]).name
    # The OpenCode launcher binds the model in terminal create, not worker-start.
    if not model:
        for setup in command_segments(command):
            tokens = shlex.split(setup)
            # Assignment prefix is present in the shipped HANDLE=$(orca ...) form.
            if not tokens or not re.fullmatch(r"(?:\w+=\$\()?orca", tokens[0]):
                continue
            if tokens[1:3] != ["terminal", "create"]:
                continue
            nested = flag_value(tokens, "--command")
            if nested:
                inner = shlex.split(nested)
                if inner and Path(inner[0]).name == "opencode":
                    agent, model = "opencode", flag_value(inner, "--model", "-m")
    if not agent or not model:
        return None  # No default model guess can establish what actually ran.
    provider = agent
    if agent == "opencode":
        prefix, separator, model = model.partition("/")
        provider = {"opencode-go": "opencode", "opencode": "opencode_zen",
                    "openrouter": "openrouter"}.get(prefix)
        if not separator or not provider:
            return None
    result = (payload.get("result") or {}) if isinstance(payload, dict) else {}
    worker = result.get("worker") or result
    dispatch = worker.get("dispatchId") or worker.get("dispatch_id") or event.get("tool_use_id")
    if not dispatch:
        return None
    resource = worker.get("resource") or {}
    worktree = resource.get("worktreeId") or flag_value(args, "--name") or flag_value(args, "--worktree")
    if worktree:
        worktree = worktree.split("::")[-1].removeprefix("name:")
        if worktree in ("current", "new-child"):
            worktree = None
    return dict(provider=provider, model=model, dispatch=dispatch, worktree=worktree,
                effort=flag_value(args, "--effort"))


def reserve_for(spec: str, receipt: dict) -> None:
    """Book the capacity, after the launch has actually happened.

    Reserving before the command runs books work that may never start: a
    drafted line, a refused permission, a command that failed. Three of those
    once filled Codex's in-flight limit while it had eighty-five points free.
    """
    try:
        argv = [str(RIGHTSIZE), "report", receipt["provider"], "--started",
                "--model", receipt["model"], "--task", spec, "--dispatch", receipt["dispatch"]]
        for name in ("worktree", "effort"):
            if receipt.get(name):
                argv += ["--" + name, receipt[name]]
        subprocess.run(argv,
                       capture_output=True, text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        pass


def advise(spec: str) -> str | None:
    try:
        result = subprocess.run(
            [str(RIGHTSIZE), "route", "--task", spec, "--json"],
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
    lines.append(f"  when this worker finishes: rightsize report {pick['provider']} --done"
                 " (or report --from-orca for all of them)")
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
    if not spec:
        print("{}")
        return 0

    if event.get("hook_event_name") == "PostToolUse":
        receipt = launch_receipt(command, segment, event)
        if receipt:
            reserve_for(spec, receipt)
        else:
            print("rightsize: launch not booked; failed or missing provider/model/receipt evidence",
                  file=sys.stderr)
        print("{}")
        return 0

    context = advise(spec)
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

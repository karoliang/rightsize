#!/usr/bin/env python3
"""Self-check for the dispatch hook's matching. Run: python3 hooks/test_hook.py

The hook decides whether a shell command is a worker launch. Getting that wrong
in the permissive direction spends a judgment on a question nobody asked and
puts noise in the transcript, which is exactly what happened twice while it was
being written: a config file full of launcher templates, and an `echo` of a
launch line, both read as launches.
"""

import importlib.util
import json
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("hook", Path(__file__).with_name("claude_pretooluse.py"))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
router_spec = importlib.util.spec_from_file_location("rightsize", ROOT / "rightsize.py")
router = importlib.util.module_from_spec(router_spec)
router_spec.loader.exec_module(router)
CONFIG = json.loads((ROOT / "config.json").read_text())

LAUNCHES = [
    'orca orchestration worker-start --spec "add a rate limit to POST /api/signup" --agent opencode --json',
    'cd /tmp && orca worktree create --name x --agent opencode --prompt "$(cat spec.md)" --json',
    'orca terminal create --worktree current --command "opencode" --json',
    'opencode run -m opencode-go/deepseek-v4.1-flash "add pagination to the invoices endpoint"',
    '/usr/local/bin/codex exec --model gpt-5.6-luna "rename getUser across the repo"',
]

NOT_LAUNCHES = [
    # Text about a launch is not a launch.
    'echo "orca orchestration worker-start --spec \\"x\\" --agent opencode"',
    'grep -n "orca orchestration worker-start --spec" README.md',
    "python3 - <<PY\nc['orca'] = 'orca orchestration worker-start --spec {spec} --agent claude'\nPY",
    'git commit -m "document orca orchestration worker-start"',
    # A heredoc body is a document being written, not a command being run.
    ("cat > rules.md <<'MD'\n"
     "orca orchestration worker-start --spec \"<self-contained task>\" --agent <from rightsize>\n"
     "MD"),
    ("python3 - <<PY\n"
     "orca orchestration worker-start --spec 'add pagination to the invoices endpoint'\n"
     "PY"),
    # The router calling itself would recurse.
    'rightsize route --task "orca orchestration worker-start --spec x" --json',
    # Ordinary work.
    "ls -la && python3 test_rightsize.py",
]


def main():
    for command in LAUNCHES:
        assert hook.launch_segment(command), f"missed a launch: {command}"
    for command in NOT_LAUNCHES:
        assert not hook.launch_segment(command), f"fired on text, not a launch: {command}"

    # A placeholder or a one-word argument is the shape of a task, not a task.
    assert not hook.usable_brief("{spec}")
    assert not hook.usable_brief("<task>")
    assert not hook.usable_brief("fix it")
    assert not hook.usable_brief("<self-contained task>")
    assert hook.usable_brief("add cursor pagination to GET /api/invoices, page size 50")

    # A worker started from a task id still has a brief; it lives in the task.
    assert hook.TASK_ID.search(
        "orca orchestration worker-start --task task_7de8439fec51 --worktree new-child --json")
    assert not hook.TASK_ID.search("orca orchestration worker-start --spec \"do the thing\"")
    # The id is the name of a brief, never the brief itself.
    assert not hook.usable_brief("task_7de8439fec51")

    # A launch with no readable brief stays silent rather than judging nothing.
    assert hook.spec_text('orca orchestration worker-start --spec "{spec}" --agent opencode') is None

    # The shipped Orca launcher binds coding-plan models in OPENCODE_COMMAND,
    # including the empty-options MiniMax path. Post-launch booking must still
    # identify the provider from that rendered command.
    for provider, model, effort, band, has_options in (
            ("zai_coding_plan", "glm-5.3-flash", "max", 1, True),
            ("minimax", "MiniMax-M2.7-highspeed", None, 1, False)):
        decision = {"pick": {"provider": provider, "model": model, "effort": effort},
                    "agent": "opencode", "band": band, "worktree_name": "hook-test"}
        command = router.launch_command(
            decision, CONFIG, "orca", None,
            "Review calculator input validation and preserve boundary behavior")
        assert ("reasoningEffort" in command) is has_options, command
        response = {"stdout": json.dumps({"ok": True, "result": {"worker": {
            "dispatchId": "hook-test-" + provider,
            "resource": {"worktreeId": "repo::/repo/hook-test"}}}})}
        receipt = hook.launch_receipt(command, hook.launch_segment(command),
                                      {"tool_response": response})
        assert receipt and receipt["provider"] == provider, receipt
        assert receipt["model"] == model, receipt

    print("hook checks passed")


if __name__ == "__main__":
    main()

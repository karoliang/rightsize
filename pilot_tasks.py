"""Offline, hash-pinned coding corpus for the paired pilot; never launches models.

Only the reviewed bundled reference/initial sources are executed by self_check.
This is not an acceptance runner for model-generated code.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path

import replay


CORPUS = Path(__file__).resolve().parent / "fixtures/pilot/tasks.json"
CORPUS_SHA256 = "ad301caa309d5ad52ba2b445e37f090860e10be846ff50b4ed1be8a0938276ea"
TIERS = ("mechanical", "implementation", "diagnosis", "high_stakes")
BUILTINS = "abs all any bool dict enumerate filter float int isinstance len list map max min range reversed round set sorted str sum tuple zip Exception ValueError TypeError"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def load():
    raw = CORPUS.read_bytes()
    if digest(raw) != CORPUS_SHA256:
        raise ValueError("pilot corpus differs from reviewed digest")
    return json.loads(raw)["tasks"]


def check_source(source, cases):
    """Internal trusted-fixture check, never call with generated candidate code."""
    namespace = {}
    exec(compile(source, "<reviewed-pilot-fixture>", "exec"), namespace)
    passed = 0
    for case in cases:
        args = copy.deepcopy(case["args"])
        try:
            result = namespace["solve"](*args)
            # JSON representation distinguishes True from 1, unlike Python ==.
            matches = json.dumps(result, sort_keys=True, allow_nan=False) == json.dumps(
                case["expected"], sort_keys=True, allow_nan=False)
            passed += bool(matches and args == case["args"])
        except Exception:
            pass
    return passed


def self_check():
    tasks = load()
    if len(tasks) != 20 or len({row["id"] for row in tasks}) != 20:
        raise ValueError("pilot requires twenty distinct tasks")
    if any(sum(row["tier"] == tier for row in tasks) != 5 for tier in TIERS):
        raise ValueError("pilot requires five tasks per tier")
    checks = 0
    for row in tasks:
        count = len(row["cases"])
        if not count or check_source(row["reference_source"], row["cases"]) != count:
            raise ValueError("reference acceptance failed: " + row["id"])
        if check_source(row["initial_source"], row["cases"]) == count:
            raise ValueError("initial source already passes: " + row["id"])
        checks += count
    return {"tasks": len(tasks), "checks": checks, "corpus_sha256": CORPUS_SHA256,
            "live_attempts": 0}


def prompt(row):
    return ("Edit solution.py to implement the following contract. Keep the solve "
            "function signature. Do not change TASK.md or add files. Use only Python "
            "builtins; no imports, file or network I/O, subprocesses, or external "
            "dependencies. Keep executable code inside function definitions; no "
            "decorators or dunder introspection. Do not mutate arguments. Inputs "
            "follow the stated domain. Available builtins: " + BUILTINS + ".\n\n"
            + row["task"] + "\n")


def prepare(destination):
    """Create equivalent file snapshots; refuse to overwrite any existing path."""
    self_check()
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = {"schema_version": 1, "corpus_sha256": CORPUS_SHA256,
                "baseline_revision": replay.BASELINE,
                "baseline_source_sha256": replay.BASELINE_SOURCE,
                "max_attempts_per_variant_task": 2, "live_ready": False,
                "usage_ceilings": None, "pairs": []}
    tasks = load()
    # Rotate category positions between rounds so AB/BA is not fixed by tier.
    grouped = {tier: [row for row in tasks if row["tier"] == tier] for tier in TIERS}
    ordered = [grouped[TIERS[(offset + index) % 4]][index]
               for index in range(5) for offset in range(4)]
    for index, row in enumerate(ordered):
        task = prompt(row)
        source = row["initial_source"]
        task_hash = digest(task.encode())
        judgment = {"schema_version": 1, "actor": "active-agent",
                    "task_sha256": task_hash,
                    "judgment": {"tier": row["tier"], "size": 0,
                                 "second_opinion": 0, "spec_complete": 1,
                                 "destructive": int(row["tier"] == "high_stakes")}}
        pair = {"id": row["id"], "tier": row["tier"], "task_sha256": task_hash,
                "initial_source_sha256": digest(source.encode()),
                "acceptance_sha256": digest(json.dumps(row["cases"], sort_keys=True).encode()),
                "order": ["baseline", "candidate"] if index % 2 == 0 else ["candidate", "baseline"],
                "judgment": judgment}
        for variant in ("baseline", "candidate"):
            target = destination / row["id"] / variant
            target.mkdir(parents=True, mode=0o700)
            for name, content in (("solution.py", source), ("TASK.md", task)):
                path = target / name
                with path.open("x") as handle:
                    os.chmod(path, 0o600)
                    handle.write(content)
        manifest["pairs"].append(pair)
    path = destination / "manifest.json"
    with path.open("x") as handle:
        os.chmod(path, 0o600)
        json.dump(manifest, handle, indent=2)
        handle.write("\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", type=Path, help="new private snapshot directory")
    args = parser.parse_args()
    result = prepare(args.prepare) if args.prepare else self_check()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

"""Advisory evidence integration; never probes, launches or infers acceptance."""

import hashlib
import json
from pathlib import Path
import uuid


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def store(runtime):
    from outcome_store import OutcomeStore
    return OutcomeStore(runtime.STATE.with_name("outcomes.sqlite3"))


def project_path():
    # Explicit caller cwd, not a guessed project from task prose or branch names.
    return str(Path.cwd().resolve())


def model_identity(choice, config):
    if choice is None:
        return None
    provider, model = choice["provider"], choice["model"]
    profile = (config.get("model_profiles") or {}).get(f"{provider}:{model}", {})
    return {"provider": provider, "model": model, "effort": choice.get("effort"),
            "family": profile.get("family", model)}


def record_decision(runtime, decision, spec, config):
    decision_id = decision.setdefault("decision_id", uuid.uuid4().hex)
    source = hashlib.sha256()
    for name in ("rightsize.py", "task_judgment.py", "outcome_cli.py", "outcome_store.py"):
        path = runtime.ROOT / name
        source.update(name.encode() + b"\0" + path.read_bytes())
    record = {"decision_id": decision_id, "project": project_path(),
              "task_sha256": hashlib.sha256(spec.encode()).hexdigest(),
              "policy_revision": source.hexdigest(), "config_sha256": digest(config),
              "selected": model_identity(decision.get("pick"), config),
              "judgment_source": (decision.get("judgment") or {}).get("source", "unknown"),
              "task_tier": decision["judgment"]["tier"],
              "quality_floor": decision.get("quality_floor", decision["band"]),
              "review_required": bool(decision.get("review_required")), "at": runtime.now()}
    store(runtime).decision(record)
    return record


def record_launch(runtime, config, provider, model, task, dispatch, worktree, effort,
                  decision_id=None, override_reason=None, native_session=None):
    ledger = store(runtime)
    existing = ledger.get_launch(dispatch)
    record = {"dispatch_id": dispatch, "decision_id": decision_id,
              "project": project_path(), "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
              "actual": model_identity({"provider": provider, "model": model, "effort": effort}, config),
              "override_reason": override_reason, "worktree": worktree,
              "native_session_id": native_session, "account_status": "unverified",
              "at": existing["at"] if existing else runtime.now()}
    return ledger.launch(record)


def command(args, config, runtime):
    ledger = store(runtime)
    if args.action == "audit":
        project = str(Path(args.project).resolve()) if args.project else None
        result = ledger.audit(project)
    else:
        with Path(args.data).open("rb") as handle:
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("outcome data exceeds 64 KiB")
        def unique(pairs):
            obj = {}
            for key, value in pairs:
                if key in obj:
                    raise ValueError("duplicate outcome field")
                obj[key] = value
            return obj
        data = json.loads(raw, object_pairs_hook=unique)
        if args.kind == "review" and isinstance(data, dict):
            profile = (config.get("model_profiles") or {}).get(
                f"{data.get('provider')}:{data.get('model')}")
            if not profile or data.get("family") != profile.get("family"):
                raise ValueError("reviewer model/family must match a configured model profile")
            launch = ledger.get_launch(args.dispatch)
            decision = ledger.get_decision(launch["decision_id"]) if launch and launch["decision_id"] else None
            if decision:
                candidates, _ = runtime.qualified_candidates(
                    [f"{data['provider']}:{data['model']}"], config,
                    {"tier": decision["task_tier"]}, decision["quality_floor"], review=True)
                if not candidates:
                    raise ValueError("reviewer does not meet the recorded task quality floor/capabilities")
        result = ledger.event(args.dispatch, args.event_id, args.kind, data)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0

"""Advisory evidence integration; never probes, launches or infers acceptance."""

import hashlib
import json
from pathlib import Path
import uuid


# The closeout manifest, like the single-event payload, is small enough to read
# whole. A larger envelope would push reviewers toward the cap before they
# discover the bound, and an evidence file worth more than 1 MiB is almost
# always raw text or a captured log that should have been a digest.
CLOSEOUT_MANIFEST_MAX_BYTES = 64 * 1024
CLOSEOUT_EVIDENCE_MAX_BYTES = 1024 * 1024
CLOSEOUT_EVENTS_MAX = 64


_CLOSEOUT_REQUIRED = frozenset({"schema_version", "dispatch", "events"})
_CLOSEOUT_OPTIONAL = frozenset({"decision_id", "project"})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def store(runtime):
    from outcome_store import OutcomeStore
    return OutcomeStore(runtime.STATE.with_name("outcomes.sqlite3"))


def project_path():
    # Explicit caller cwd, not a guessed project from task prose or branch names.
    return str(Path.cwd().resolve())


def canonicalize_project(value):
    """Resolve ``--project`` once, reject blank input, never let it silently
    default to cwd when an operator passed ``--project=""``."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("--project must be a nonempty path")
    return str(Path(value).expanduser().resolve())


def resolve_project(explicit, ledger=None, decision_id=None):
    """Coordinator project for a record: explicit arg > decision > cwd.

    An explicit project that disagrees with the recorded decision is a typo
    or a relaunch in another tree, and is the coordinator's problem to notice
    before this becomes a launch receipt no audit can reconcile.
    """
    from outcome_store import OutcomeError
    explicit = canonicalize_project(explicit) if explicit is not None else explicit
    recorded = None
    if ledger is not None and isinstance(decision_id, str) and decision_id:
        recorded = (ledger.get_decision(decision_id) or {}).get("project")
    if explicit and recorded and explicit != recorded:
        raise OutcomeError(
            f"explicit project {explicit!r} does not match decision {decision_id!r} project {recorded!r}"
        )
    if explicit:
        return explicit
    if recorded:
        return recorded
    return project_path()


def model_identity(choice, config):
    if choice is None:
        return None
    provider, model = choice["provider"], choice["model"]
    profile = (config.get("model_profiles") or {}).get(f"{provider}:{model}", {})
    return {"provider": provider, "model": model, "effort": choice.get("effort"),
            "family": profile.get("family", model)}


def reviewer_validator(config, runtime):
    """Return a closure the store calls inside the write transaction.

    Reviewer profile/family lookup and the configured quality floor plus
    capability gates are evaluated against the live locked decision/launch
    the store hands in, so a hook that bound the launch before this check
    cannot weaken acceptance through a stale local view. The same-family
    independence rule stays in the store's accepted-gate, where it belongs,
    because recording a same-family review is legitimate (a coordinator may
    store it for traceability); acceptance is the point that has to refuse it.
    """
    from outcome_store import OutcomeError
    profiles = config.get("model_profiles") or {}

    def validate(db, decision, launch, review_data):
        reviewer_key = f"{review_data.get('provider')}:{review_data.get('model')}"
        profile = profiles.get(reviewer_key)
        if not profile or review_data.get("family") != profile.get("family"):
            raise OutcomeError(
                f"reviewer model/family must match a configured model profile ({reviewer_key})"
            )
        if decision is not None:
            candidates, _ = runtime.qualified_candidates(
                [reviewer_key], config,
                {"tier": decision["task_tier"]}, decision["quality_floor"], review=True)
            if not candidates:
                raise OutcomeError(
                    "reviewer does not meet the recorded task quality floor/capabilities"
                )
    return validate


def record_decision(runtime, decision, spec, config, project=None):
    decision_id = decision.setdefault("decision_id", uuid.uuid4().hex)
    source = hashlib.sha256()
    for name in ("rightsize.py", "task_judgment.py", "outcome_cli.py", "outcome_store.py"):
        path = runtime.ROOT / name
        source.update(name.encode() + b"\0" + path.read_bytes())
    record = {"decision_id": decision_id, "project": resolve_project(project),
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
                  decision_id=None, override_reason=None, native_session=None,
                  project=None):
    ledger = store(runtime)
    resolved = resolve_project(project, ledger, decision_id)
    existing = ledger.get_launch(dispatch)
    # Preserve supplied actual settings; never fabricate an effective effort or
    # model from the decision. Mismatches are the operator's signal that an
    # override-reason must travel with the launch.
    record = {"dispatch_id": dispatch, "decision_id": decision_id,
              "project": resolved, "task_sha256": hashlib.sha256(task.encode()).hexdigest(),
              "actual": model_identity({"provider": provider, "model": model, "effort": effort}, config),
              "override_reason": override_reason, "worktree": worktree,
              "native_session_id": native_session, "account_status": "unverified",
              "at": existing["at"] if existing else runtime.now()}
    return ledger.launch(record)


def _read_manifest(path):
    """Load and parse a closeout manifest, rejecting unknown envelope fields.

    One nonblocking ``os.open`` does the fstat and the bounded read, then
    closes in a ``finally``. Two separate opens would let a manifest path be
    swapped for a FIFO between the stat and the read, and re-opening with
    ``Path.open`` would also race against a late symlink replacement.
    """
    import os
    import stat as stat_module
    from outcome_store import OutcomeError
    flags = os.O_RDONLY | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise OutcomeError(f"closeout manifest not readable: {exc}") from exc
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise OutcomeError(f"closeout manifest not readable: {exc}") from exc
        mode = st.st_mode
        if stat_module.S_ISDIR(mode):
            raise OutcomeError("closeout manifest path is a directory")
        if stat_module.S_ISFIFO(mode):
            raise OutcomeError("closeout manifest path is a fifo")
        if stat_module.S_ISCHR(mode) or stat_module.S_ISBLK(mode):
            raise OutcomeError("closeout manifest path is a device")
        if stat_module.S_ISLNK(mode):
            raise OutcomeError("closeout manifest path is a symlink")
        if not stat_module.S_ISREG(mode):
            raise OutcomeError("closeout manifest path is not a regular file")
        if st.st_size > CLOSEOUT_MANIFEST_MAX_BYTES:
            raise OutcomeError(f"closeout manifest exceeds {CLOSEOUT_MANIFEST_MAX_BYTES} bytes")
        try:
            raw = os.read(fd, CLOSEOUT_MANIFEST_MAX_BYTES + 1)
        except OSError as exc:
            raise OutcomeError(f"closeout manifest not readable: {exc}") from exc
    finally:
        os.close(fd)
    if len(raw) > CLOSEOUT_MANIFEST_MAX_BYTES:
        raise OutcomeError(f"closeout manifest exceeds {CLOSEOUT_MANIFEST_MAX_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise OutcomeError("closeout manifest is not valid UTF-8") from exc
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise OutcomeError("duplicate manifest field")
            obj[key] = value
        return obj
    try:
        manifest = json.loads(text, object_pairs_hook=unique)
    except (ValueError, RecursionError) as exc:
        raise OutcomeError(f"closeout manifest JSON invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise OutcomeError("closeout manifest must be a JSON object")
    missing = _CLOSEOUT_REQUIRED - set(manifest)
    if missing:
        raise OutcomeError(f"closeout manifest missing fields {sorted(missing)}")
    extra = set(manifest) - (_CLOSEOUT_REQUIRED | _CLOSEOUT_OPTIONAL)
    if extra:
        raise OutcomeError(f"closeout manifest has unknown fields {sorted(extra)}")
    schema_version = manifest["schema_version"]
    if type(schema_version) is not int or schema_version != 1:
        raise OutcomeError("closeout manifest schema_version must be integer 1")
    dispatch = manifest["dispatch"]
    if not isinstance(dispatch, str) or not dispatch.strip():
        raise OutcomeError("closeout manifest dispatch must be a nonempty string")
    events = manifest["events"]
    if not isinstance(events, list):
        raise OutcomeError("closeout manifest events must be a list")
    decision_id = manifest.get("decision_id")
    if decision_id is not None and (not isinstance(decision_id, str) or not decision_id.strip()):
        raise OutcomeError("closeout manifest decision_id must be a nonempty string or omitted")
    project = manifest.get("project")
    if project is not None and (not isinstance(project, str) or not project.strip()):
        raise OutcomeError("closeout manifest project must be a nonempty string or omitted")
    return manifest


def command_closeout(args, config, runtime):
    """Atomic batch closeout: validate the whole manifest, then apply in one transaction."""
    from outcome_store import OutcomeError
    manifest = _read_manifest(args.manifest)
    decision_id = manifest.get("decision_id")
    project = manifest.get("project")
    if project is not None:
        project = canonicalize_project(project)
    evidence_root = Path(args.manifest).resolve().parent
    result = store(runtime).closeout(
        manifest["dispatch"], manifest["events"],
        decision_id=decision_id, project=project,
        evidence_root=evidence_root,
        max_events=CLOSEOUT_EVENTS_MAX,
        max_evidence_bytes=CLOSEOUT_EVIDENCE_MAX_BYTES,
        review_validator=reviewer_validator(config, runtime),
    )
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


def command(args, config, runtime):
    ledger = store(runtime)
    if args.action == "audit":
        project = canonicalize_project(args.project) if args.project else None
        result = ledger.audit(project)
    elif args.action == "closeout":
        return command_closeout(args, config, runtime)
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
        result = ledger.event(args.dispatch, args.event_id, args.kind, data,
                              review_validator=reviewer_validator(config, runtime))
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0

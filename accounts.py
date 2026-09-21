"""Native account selection and redacted identity, without exporting auth tokens."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


RUNTIMES = {"codex": "codex", "claude": "claude", "opencode": "opencode",
            "opencode_zen": "opencode", "openrouter": "opencode", "minimax": "opencode"}
HOME_VARIABLES = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR",
                  "opencode": "XDG_DATA_HOME"}
KEY_VARIABLES = {"codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
                 "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                 "opencode": ("OPENCODE_API_KEY",),
                 "opencode_zen": ("OPENCODE_ZEN_API_KEY",),
                 "openrouter": ("OPENROUTER_API_KEY",),
                 "minimax": ("MINIMAX_API_KEY",)}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Binding:
    provider: str
    runtime: str
    account_ref: str
    fingerprint: str
    status: str
    source: str
    home: Path = field(repr=False)
    overrides: dict = field(default_factory=dict, repr=False)

    def public(self):
        return {key: getattr(self, key) for key in
                ("provider", "runtime", "account_ref", "fingerprint", "status", "source")}

    def environment(self):
        env = {**os.environ, **self.overrides}
        if self.runtime == "claude" and "CLAUDE_CONFIG_DIR" not in self.overrides:
            env.pop("CLAUDE_CONFIG_DIR", None)
        return env


def select(provider, config=None, *, home=None, environ=None):
    """Invocation selection (merged by CLI) > config binding > native selection.

    Config contains references and home paths only. Auth content is never read.
    File metadata changes conservatively invalidate observations, including
    native refresh. Environment keys contribute hashes, never persisted values.
    """
    config = config or {}
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    runtime = RUNTIMES.get(provider, "opencode")
    variable = HOME_VARIABLES[runtime]
    default = {"codex": home / ".codex", "claude": home / ".claude",
               "opencode": home / ".local/share"}[runtime]
    selected = (config.get("accounts") or {}).get(provider)
    definitions = config.get("account_bindings") or {}
    source, status, overrides = "native", "unverified", {}
    native = environ.get(variable)
    if runtime == "codex":
        orca = environ.get("ORCA_CODEX_HOME")
        if native and orca and Path(native).expanduser().resolve() != Path(orca).expanduser().resolve():
            status = "ambiguous"
        native = native or orca
    selected_home = native or default
    if selected:
        entry = definitions.get(selected) if isinstance(selected, str) else None
        if (not isinstance(entry, dict) or set(entry) != {"runtime", "home"}
                or entry.get("runtime") != runtime or not isinstance(entry.get("home"), str)
                or not Path(entry["home"]).expanduser().is_absolute()):
            status = "invalid-binding"
        else:
            selected_home, source = entry["home"], "configured-native"
            # An explicit reference overrides native defaults, but cannot change
            # inherited API authentication into a subscription silently.
            if any(environ.get(key) for key in KEY_VARIABLES.get(provider, ())):
                status = "ambiguous"
    elif runtime == "codex" and not native:
        roots = [default] if (default / "auth.json").is_file() else []
        orca_root = home / "Library/Application Support/orca/codex-accounts"
        roots += [p.parent for p in sorted(orca_root.glob("*/home/auth.json"))]
        if len({str(p.resolve()) for p in roots}) > 1:
            status = "ambiguous"
        elif roots:
            selected_home = roots[0]
    path = Path(selected_home).expanduser().resolve()
    overrides[variable] = str(path)
    # Claude's native Keychain namespace distinguishes unset config-dir from
    # an explicit path, even when that path is ~/.claude. Preserve the choice.
    if runtime == "claude" and not selected and not native:
        overrides.pop(variable)
    if runtime == "codex":
        overrides["ORCA_CODEX_HOME"] = str(path)
    auth = {"codex": path / "auth.json", "claude": path / ".credentials.json",
            "opencode": path / "opencode/auth.json"}[runtime]
    try:
        stat = auth.stat()
        stamp = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
    except OSError:
        stamp = None
    # A ref names an account context. A fingerprint detects credential changes
    # within that context, without treating refreshed credentials as new leases.
    context = [provider, runtime, str(path)]
    if runtime == "claude":
        context.append("explicit-home" if variable in overrides else "native-default")
    reference = digest(json.dumps(context))[:24]
    keys = {key: digest(environ[key]) for key in KEY_VARIABLES.get(provider, ()) if environ.get(key)}
    fingerprint = digest(json.dumps([reference, stamp, keys], sort_keys=True))
    if not selected and keys:
        source = "environment"
    return Binding(provider, runtime, reference, fingerprint, status, source, path, overrides)


def cache_identity(config):
    identities = {provider: select(provider, config).public() for provider in RUNTIMES}
    # Keychain login changes need not touch .credentials.json. Consult only
    # native redacted status, never decode tokens or export Keychain contents.
    identities["claude_native"] = claude_identity(select("claude", config))
    return identities


def claude_identity(binding):
    """Native subscription identity, hashed before leaving this boundary."""
    from native_rpc import read_json
    if binding.status != "unverified":
        return {"status": binding.status}
    value = read_json(["claude", "auth", "status", "--json"], env=binding.environment(), returncodes=(0, 1))
    if not value:
        return {"status": "unknown"}
    if value.get("loggedIn") is not True:
        return {"status": "reauth-required"}
    if value.get("authMethod") != "claude.ai" or value.get("apiProvider") != "firstParty":
        return {"status": "unsupported-auth"}
    fields = [value.get(key) for key in ("email", "orgId")]
    if any(not isinstance(item, str) or not item.strip() for item in fields):
        return {"status": "unknown"}
    result = {"status": "ok", "quota_account_ref": digest(json.dumps(["claude", *fields]))}
    # initialize reports the organization display name, not its canonical ID.
    # Keep both proofs distinct; the display signature is not a new quota pool.
    if isinstance(value.get("orgName"), str) and value["orgName"].strip():
        result["session_account_ref"] = digest(json.dumps(["claude", value["email"], value["orgName"]]))
    return result


def runtime_version(runtime):
    """Only a version number can escape diagnostic stdout."""
    if runtime not in HOME_VARIABLES:
        return "unknown"
    try:
        result = subprocess.run([runtime, "--version"], capture_output=True,
                                text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    match = re.search(r"\b\d+\.\d+\.\d+\b", result.stdout[:1024])
    return match.group() if result.returncode == 0 and match else "unknown"

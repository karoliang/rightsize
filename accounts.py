"""Native account selection and redacted identity, without exporting auth tokens."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


RUNTIMES = {"codex": "codex", "claude": "claude", "opencode": "opencode",
            "opencode_zen": "opencode", "openrouter": "opencode"}
HOME_VARIABLES = {"codex": "CODEX_HOME", "claude": "CLAUDE_CONFIG_DIR",
                  "opencode": "XDG_DATA_HOME"}
KEY_VARIABLES = {"codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
                 "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
                 "opencode": ("OPENCODE_API_KEY",),
                 "opencode_zen": ("OPENCODE_ZEN_API_KEY",),
                 "openrouter": ("OPENROUTER_API_KEY",)}


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
        return {**os.environ, **self.overrides}


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
    reference = digest(json.dumps([provider, runtime, str(path)]))[:24]
    keys = {key: digest(environ[key]) for key in KEY_VARIABLES.get(provider, ()) if environ.get(key)}
    fingerprint = digest(json.dumps([reference, stamp, keys], sort_keys=True))
    if not selected and keys:
        source = "environment"
    return Binding(provider, runtime, reference, fingerprint, status, source, path, overrides)


def cache_identity(config):
    return {provider: select(provider, config).public() for provider in RUNTIMES}


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

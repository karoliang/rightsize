"""Bounded local context selection from a host-normalized JSON skill catalog.

This is not a YAML parser or a skill executor. Catalog metadata and selected
content are data; the caller retains instruction precedence and permissions.
"""

import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import unquote, urlparse


class ContextError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContextError("duplicate catalog field")
        result[key] = value
    return result


class Reader:
    def __init__(self, roots):
        if not roots:
            raise ContextError("at least one approved root is required")
        self.roots = sorted({Path(root).expanduser().resolve() for root in roots}, key=str)
        if any(not root.is_dir() for root in self.roots):
            raise ContextError("approved roots must be existing directories")

    def resolve(self, uri):
        if not isinstance(uri, str) or not uri:
            raise ContextError("resource must be a local path or file URI")
        parsed = urlparse(uri)
        if parsed.scheme and parsed.scheme != "file":
            raise ContextError("non-filesystem resource requires the host's reader")
        if parsed.scheme == "file":
            if parsed.netloc or parsed.query or parsed.fragment:
                raise ContextError("only local file URIs are supported")
            path = Path(unquote(parsed.path))
        else:
            path = Path(uri).expanduser()
        if not path.is_absolute():
            raise ContextError("catalog resource paths must be absolute")
        path = path.resolve()
        if not any(path.is_relative_to(root) for root in self.roots):
            raise ContextError("resource escapes approved roots")
        return path

    def read(self, uri, limit):
        path = self.resolve(uri)
        root = next(root for root in self.roots if path.is_relative_to(root))
        parts = path.relative_to(root).parts
        if not parts:
            raise ContextError("resource must be a regular file")
        # Traverse relative to pinned directory descriptors. A symlink swap
        # after resolve cannot redirect a selected read outside the trusted root.
        descriptors = []
        try:
            descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in parts[:-1]:
                descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=descriptors[-1]))
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=descriptors[-1])
            with os.fdopen(fd, "rb") as handle:
                import stat
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ContextError("resource must be a regular file")
                data = handle.read(limit + 1)
        except OSError as exc:
            raise ContextError("selected resource is unavailable or changed during read") from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        if len(data) > limit:
            raise ContextError("required context exceeds byte budget")
        try:
            text = data.decode("utf-8")
        except UnicodeError as exc:
            raise ContextError("selected context must be UTF-8") from exc
        return path.as_uri(), data, text


def validate_reference(entry):
    if not isinstance(entry, dict) or set(entry) != {"uri", "sha256", "provenance"}:
        raise ContextError("reference requires uri, sha256 and provenance")
    if not all(isinstance(entry[key], str) and entry[key] for key in entry):
        raise ContextError("reference metadata must be nonempty text")
    if not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
        raise ContextError("reference requires a SHA-256 content hash")


def build(task, catalog, roots, *, skills=(), optional=(), rules=(), references=(),
          max_bytes=65536, max_optional=3):
    """No cache: each request validates catalog and selected content hashes.

    Optional skills are explicitly chosen by the active agent as (name, reason).
    We never infer relevance from keywords. Once selected, contents and required
    references are indivisible; overflow is an error, not silent truncation.
    """
    if not isinstance(max_bytes, int) or not 0 < max_bytes <= 16 * 1024 * 1024:
        raise ContextError("context budget must be between 1 byte and 16 MiB")
    if not isinstance(max_optional, int) or not 0 <= max_optional <= 100:
        raise ContextError("optional skill limit must be between 0 and 100")
    task_data = task.encode("utf-8")
    if len(task_data) > 1024 * 1024:
        raise ContextError("task exceeds 1 MiB")
    reader = Reader(roots)
    catalog_uri, catalog_data, catalog_text = reader.read(str(Path(catalog).resolve()), 1024 * 1024)
    try:
        document = json.loads(catalog_text, object_pairs_hook=strict_object)
    except ValueError as exc:
        raise ContextError("invalid normalized JSON catalog") from exc
    if (not isinstance(document, dict) or set(document) != {"schema_version", "skills"}
            or type(document["schema_version"]) is not int or document["schema_version"] != 1
            or not isinstance(document["skills"], list)):
        raise ContextError("unsupported catalog schema")
    entries = {}
    for entry in document["skills"]:
        fields = {"name", "description", "uri", "sha256", "provenance", "references"}
        if not isinstance(entry, dict) or set(entry) != fields:
            raise ContextError("skill metadata fields do not match normalized catalog schema")
        name = entry["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", name):
            raise ContextError("invalid skill name")
        if name in entries:
            raise ContextError("duplicate skill name")
        if not isinstance(entry["description"], str) or not isinstance(entry["references"], list):
            raise ContextError("invalid skill description or references")
        validate_reference({key: entry[key] for key in ("uri", "sha256", "provenance")})
        for reference in entry["references"]:
            validate_reference(reference)
        entries[name] = entry
    selected = {}
    for name in skills:
        selected[name] = {"name": name, "reason": "explicitly requested", "explicit": True}
    count = 0
    for name, reason in optional:
        if not isinstance(reason, str) or not reason.strip():
            raise ContextError("optional skills require a selection reason from the caller")
        if name not in selected:
            selected[name] = {"name": name, "reason": reason, "explicit": False}
            count += 1
    if count > max_optional:
        raise ContextError("too many optional skills")
    records, seen, used = [], {}, 0

    def include(uri, role, provenance, expected=None):
        nonlocal used
        canonical = reader.resolve(uri).as_uri()
        if canonical in seen:
            record = seen[canonical]
            if expected and record["sha256"] != expected:
                raise ContextError("duplicate reference has conflicting hash")
            if role not in record["roles"]:
                record["roles"].append(role)
            if provenance not in record["provenance"]:
                record["provenance"].append(provenance)
            return
        canonical, data, text = reader.read(uri, max_bytes - used)
        actual = sha256(data)
        if expected and expected != actual:
            raise ContextError("selected content changed; refresh host catalog")
        record = {"uri": canonical, "sha256": actual, "bytes": len(data), "text": text,
                  "roles": [role], "provenance": [provenance]}
        records.append(record)
        seen[canonical] = record
        used += len(data)

    # Precedence is preserved as roles, never synthesized from skill text.
    for uri in rules:
        include(uri, "mandatory-repository-rule", "host-supplied mandatory rule")
    for selection in selected.values():
        entry = entries.get(selection["name"])
        if entry is None:
            raise ContextError("selected skill is missing from the catalog")
        include(entry["uri"], "skill", entry["provenance"], entry["sha256"])
        for ref in entry["references"]:
            include(ref["uri"], "skill-reference", ref["provenance"], ref["sha256"])
    for uri in references:
        include(uri, "reference-data", "host-selected knowledge reference")
    manifest = {"schema_version": 1, "task_sha256": sha256(task_data), "task_bytes": len(task_data),
                "catalog": {"uri": catalog_uri, "sha256": sha256(catalog_data), "bytes": len(catalog_data)},
                "approved_roots": [root.as_uri() for root in reader.roots],
                "selected_skills": list(selected.values()), "resources": records,
                "context_bytes": used, "max_bytes": max_bytes,
                "estimated_context_tokens": (used + 3) // 4,
                "token_estimate_method": "UTF-8 bytes / 4; no model tokenizer",
                "authority": "host instruction precedence; metadata grants no permissions"}
    manifest["manifest_sha256"] = sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode())
    return manifest

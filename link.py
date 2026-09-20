#!/usr/bin/env python3
"""Shell helpers for the `rightsize` wrapper, kept out of it to avoid quoting.

    python3 link.py .infisical.json   -> "<workspaceId> <environment>"
    python3 link.py --dotenv          -> `export NAME='value'` lines on stdin
"""

import json
import os
import shlex
import sys


def link(path: str) -> None:
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        print(" prod")
        return
    print(f"{data.get('workspaceId', '')} {data.get('defaultEnvironment') or 'prod'}")


def dotenv() -> None:
    """Turn Infisical's dotenv output into shell exports.

    Values arrive single-quoted. Anything already set in the environment is
    skipped, so an explicit variable always beats the stored secret.
    """
    for line in sys.stdin:
        name, separator, value = line.rstrip("\n").partition("=")
        if not separator or not name or os.environ.get(name):
            continue
        if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        print(f"export {name}={shlex.quote(value)}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--dotenv":
        dotenv()
    elif len(sys.argv) > 1:
        link(sys.argv[1])
    else:
        print(" prod")

#!/usr/bin/env python3
"""Run the offline test suite with an interpreter-version guard.

The suite needs Python 3.11 or newer: test_model_policy.py calls
contextlib.chdir, which landed in 3.11. /usr/bin/python3 on this host is 3.9.6
and produces a flood of confusing errors that look like a broken repository.

The hook, hooks/claude_pretooluse.py, is no longer a reason: it carries
from __future__ import annotations and imports fine on 3.9.

Run it from the repo root:

    python3 tests.py

On a too-old interpreter the guard prints one message and exits 2 before
the suite is collected, so the operator never sees 293 misleading errors.
"""
import sys

REQUIRED = (3, 11)


def _too_old_message():
    detected = "{}.{}.{}".format(*sys.version_info[:3])
    minimum = "{}.{}".format(*REQUIRED)
    return (
        "tests.py: Python {0}+ required, detected {1} ({2}).\n"
        "Run with a 3.11+ interpreter, e.g. `python3.11 tests.py` or\n"
        "`uv run python tests.py`."
    ).format(minimum, detected, sys.executable)


def main():
    if sys.version_info < REQUIRED:
        sys.stderr.write(_too_old_message() + "\n")
        return 2
    import unittest
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=".", pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
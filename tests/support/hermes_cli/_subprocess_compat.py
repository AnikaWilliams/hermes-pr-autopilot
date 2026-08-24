"""Minimal process compatibility surface for worker-runtime tests."""

from __future__ import annotations

import os


IS_WINDOWS = os.name == "nt"


def windows_detach_flags_without_breakaway() -> int:
    """Return safe test-process flags without changing parent process policy."""

    return 0

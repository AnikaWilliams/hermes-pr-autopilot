"""Pytest-only support for clean-runner contract tests."""

from __future__ import annotations

import os
from pathlib import Path
import sys


SUPPORT_ROOT = Path(__file__).resolve().parent / "support"
if os.environ.get("HERMES_TEST_SHIMS") == "1" and str(SUPPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPPORT_ROOT))

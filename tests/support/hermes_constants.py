"""Minimal Hermes home helper for clean-runner worker-runtime tests."""

from __future__ import annotations

import os
from pathlib import Path


def get_hermes_home() -> Path:
    """Return the explicit test Hermes home."""

    return Path(os.environ["HERMES_HOME"]).resolve()

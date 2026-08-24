"""Minimal profile helpers for clean-runner worker-runtime tests."""

from __future__ import annotations

import os
from pathlib import Path
import re


_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def normalize_profile_name(profile: str) -> str:
    """Validate and return a test profile name."""

    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise ValueError("profile name is invalid")
    return profile


def get_profile_dir(profile: str) -> Path:
    """Return the Hermes home used by a test profile."""

    normalized = normalize_profile_name(profile)
    home = Path(os.environ["HERMES_HOME"]).resolve()
    return home if normalized == "default" else home / "profiles" / normalized


def profile_exists(profile: str) -> bool:
    """Only the default test profile is available on a clean runner."""

    return normalize_profile_name(profile) == "default"

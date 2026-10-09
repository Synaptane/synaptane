"""Synaptane: a team second brain stored in Git."""

import os
import re
import subprocess
from functools import cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("synaptane")
except PackageNotFoundError:  # running from the sources without installing
    __version__ = "0+unknown"

UNKNOWN_COMMIT = "unknown"


@cache
def git_commit() -> str:
    """Commit the running code comes from, to know afterwards which version did what.

    The version number (`1.0.0.dev0`) is the same for every commit between two releases:
    only the commit tells two deployments apart. In the image, it is written at build time
    (`--build-arg GIT_SHA=…` → `SYNAPTANE_GIT_SHA`); from the sources, it is asked of the
    repository that contains this package. Otherwise: "unknown", never an error.
    """
    sha = os.environ.get("SYNAPTANE_GIT_SHA", "").strip()
    if sha and sha != "unknown":  # "unknown": image built without the GIT_SHA argument
        return sha
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # git missing, unreadable folder…
        return UNKNOWN_COMMIT
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else UNKNOWN_COMMIT


def short_commit() -> str:
    """The first 7 characters of a SHA (like GitHub), or the value as is."""
    sha = git_commit()
    return sha[:7] if re.fullmatch(r"[0-9a-f]{8,64}", sha) else sha


def version_label() -> str:
    """`1.0.0.dev0 (abc1234)`: what `--version`, `doctor` and the administration show."""
    return f"{__version__} ({short_commit()})"

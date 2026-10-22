"""Offline discovery of R packages available to generated task code.

The planner and code generator must know which R packages actually exist in the
configured libraries, otherwise they may emit calls to packages that are not
installed (for example a Bioconductor package on a CRAN-only host). Discovery
only reads ``DESCRIPTION`` files under the configured libraries, so it needs no
running R process, no network and no evaluation of user data.
"""
from __future__ import annotations

import os
from pathlib import Path

# Standard system library locations for the supported platforms. These are the
# base libraries that the task sandbox already authorizes for reading.
SYSTEM_LIBRARIES = (
    "/Library/Frameworks/R.framework/Resources/library",
    "/usr/lib/R/library",
    "/usr/lib/R/site-library",
    "/usr/local/lib/R/site-library",
    "/opt/homebrew/lib/R/site-library",
)


def user_libraries() -> list[str]:
    """Common per-user R libraries, used when runtime.local.yaml is absent."""
    home = Path.home()
    paths = [home / ".local/lib/R/site-library", home / "R/library"]
    for base in (home / "R", home / "Library/R"):
        if base.is_dir():
            paths.extend(sorted(base.glob("*/library")))
            paths.extend(sorted(base.glob("*/*/library")))
            paths.extend(sorted(base.glob("*-library/*")))
    return [str(path) for path in paths if path.is_dir()]


def library_paths(cfg) -> list[str]:
    """Configured project libraries plus user and base/system libraries that exist."""
    runtime = (cfg or {}).get("runtime", {}) or {}
    paths: list[str] = []
    paths.extend(str(p) for p in (runtime.get("r_library_paths") or []))
    home = runtime.get("r_home")
    if home:
        paths.append(str(Path(home) / "library"))
    paths.extend(user_libraries())
    for key in ("R_LIBS", "R_LIBS_USER", "R_LIBS_SITE"):
        paths.extend(part for part in os.environ.get(key, "").split(os.pathsep) if part)
    paths.extend(SYSTEM_LIBRARIES)
    seen: list[str] = []
    for path in paths:
        try:
            resolved = str(Path(path))
        except (TypeError, ValueError):
            continue
        if resolved not in seen and Path(resolved).is_dir():
            seen.append(resolved)
    return seen


def available_packages(cfg) -> list[str]:
    """Names of every installed package visible in the configured libraries."""
    names: set[str] = set()
    for library in library_paths(cfg):
        try:
            descriptions = Path(library).glob("*/DESCRIPTION")
        except OSError:
            continue
        for description in descriptions:
            if description.is_file():
                names.add(description.parent.name)
    return sorted(names)


def availability_note(cfg, limit: int = 400) -> str:
    """Prompt fragment listing the R packages task code may use, or '' if none."""
    packages = available_packages(cfg)
    if not packages:
        return ""
    listed = ", ".join(packages[:limit])
    return ("Available R packages already installed in this project's configured libraries. "
            "Use ONLY these packages or base R; do NOT call library(), require(), requireNamespace() "
            "or pkg::fun for any package outside this list, and never install packages:\n" + listed)

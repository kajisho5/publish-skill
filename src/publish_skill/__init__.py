"""publish-skill: upload finished deliverables to delivery platforms, after QC."""
from __future__ import annotations

SKILL_ID = "publish"
PACKAGE_NAME = "publish-skill"


def _version() -> str:
    """The package version, from one place: this checkout's pyproject.toml when running from source,
    otherwise the installed distribution's metadata. Releases bump only pyproject.toml."""
    import re as _re
    from pathlib import Path as _Path
    pyproject = _Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        text = pyproject.read_text(encoding="utf-8")
        if _re.search(r'^name\s*=\s*"publish-skill"', text, _re.M):
            found = _re.search(r'^version\s*=\s*"([^"]+)"', text, _re.M)
            if found:
                return found.group(1)
    import sys as _sys
    for base in _sys.path:
        for meta in sorted(_Path(base or ".").glob("publish_skill-*.dist-info/METADATA")):
            found = _re.search(r"^Version: *(\S+)", meta.read_text(encoding="utf-8", errors="replace"), _re.M)
            if found:
                return found.group(1)
    return "0.0.0+unknown"


__version__ = _version()
VERSION = __version__

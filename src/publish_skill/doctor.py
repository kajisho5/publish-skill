"""`publish doctor`: what is ready, what is missing and how to get it. It never opens a network connection, never
uploads, and reports credentials as `present` / `absent` only. When ffmpeg-skill is found, its capabilities are
detected by running its contract and `export --list` locally (adapter.py; ffmpeg-skill declares no network use).

status: ok (every platform implemented with complete credentials, ffmpeg-skill found and supported) | degraded (a
platform not implemented or without credentials, or ffmpeg-skill not found or unsupported: the skill itself works,
exit 0) | fail (Python too old, exit 1)."""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import SKILL_ID, __version__
from . import credentials as creds
from .adapter import detect
from .platforms import get_platform, platform_ids

DOCTOR_SCHEMA = f"{SKILL_ID}/doctor@1"
# op -> roadmap step that implements it; plan arrived in step 2 (ADR-002), the uploaders arrive in step 4
IMPLEMENTED_OPS: Dict[str, bool] = {"plan": True, "upload": False}
OP_STEP = {"plan": 2, "upload": 4}
FFMPEG_SKILL_ENV = ("PUBLISH_FFMPEG_SKILL_DIR", "VIDEO_AGENT_FFMPEG_SKILL_DIR")
FFMPEG_SKILL_MARKER = "scripts/_contract.py"


def ffmpeg_skill_candidates(explicit: Optional[str] = None) -> List[str]:
    if explicit:
        return [explicit]
    out = [os.environ[k] for k in FFMPEG_SKILL_ENV if os.environ.get(k)]
    out += [str(Path.home() / ".claude" / "skills" / "ffmpeg-skill"), os.path.join("vendor", "ffmpeg-skill"),
            os.path.join("..", "ffmpeg-skill")]
    return out


def locate_ffmpeg_skill(explicit: Optional[str] = None) -> Dict[str, Any]:
    """Where the ffmpeg-skill checkout is (by its marker file; nothing is run). adapter.detect then checks the tools
    and flags `plan` uses by capability detection."""
    tried = ffmpeg_skill_candidates(explicit)
    for c in tried:
        if (Path(c) / FFMPEG_SKILL_MARKER).is_file():
            return {"status": "OK", "path": str(Path(c).resolve()), "needed_for": ["plan"], "needed_from_step": OP_STEP["plan"]}
    return {"status": "MISSING", "tried": tried, "needed_for": ["plan"], "needed_from_step": OP_STEP["plan"],
            "install": "git clone https://github.com/kajisho5/ffmpeg-skill ~/.claude/skills/ffmpeg-skill "
                       "(or pass --ffmpeg-skill / set PUBLISH_FFMPEG_SKILL_DIR)"}


def platforms_report() -> Dict[str, Any]:
    out = {}
    for pid in platform_ids():
        p = get_platform(pid)
        c = creds.status(pid)
        out[pid] = dict(p.spec(), credentials=c, ready=bool(p.implemented and c["complete"]))
    return out


def doctor(ffmpeg_skill_dir: Optional[str] = None, offline: bool = False) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}
    problems: List[str] = []
    py_ok = sys.version_info >= (3, 9)
    checks["python"] = {"status": "OK" if py_ok else "MISSING", "version": platform.python_version(), "platform": platform.system(),
                        "install": None if py_ok else "Python >= 3.9"}
    if not py_ok:
        problems.append("python: needs >= 3.9")
    checks["ffmpeg_skill"] = locate_ffmpeg_skill(ffmpeg_skill_dir)
    if checks["ffmpeg_skill"]["status"] == "OK":
        det = detect(checks["ffmpeg_skill"]["path"])
        checks["ffmpeg_skill"]["capabilities"] = det
        if det["status"] != "OK":
            checks["ffmpeg_skill"]["status"] = "UNSUPPORTED"
    plats = platforms_report()
    checks["platforms"] = plats
    checks["operations"] = {op: {"implemented": IMPLEMENTED_OPS[op], "roadmap_step": OP_STEP[op]} for op in IMPLEMENTED_OPS}
    warnings: List[str] = []
    for pid, p in plats.items():
        if not p["implemented"]:
            warnings.append(f"platform {pid}: uploader not implemented yet (roadmap step {p['roadmap_step']})")
        absent = sorted(n for n, s in p["credentials"]["variables"].items() if s != "present")
        if absent:
            warnings.append(f"platform {pid}: credentials absent from the environment: {', '.join(absent)}")
    if checks["ffmpeg_skill"]["status"] == "MISSING":
        warnings.append("ffmpeg-skill: not found (`publish plan` reports its delivery spec rows as UNCHECKED)")
    elif checks["ffmpeg_skill"]["status"] != "OK":
        warnings.append("ffmpeg-skill: unsupported (" + "; ".join(checks["ffmpeg_skill"]["capabilities"]["problems"])
                        + "); `publish plan` reports its delivery spec rows as UNCHECKED")
    status = "fail" if problems else ("degraded" if warnings else "ok")
    return {
        "schema": DOCTOR_SCHEMA,
        "ok": status != "fail",
        "status": status,
        "skill": {"id": SKILL_ID, "version": __version__},
        "offline": bool(offline),
        "checks": checks,
        "ready_platforms": sorted(pid for pid, p in plats.items() if p["ready"]),
        "problems": problems,
        "warnings": warnings,
        "network": "doctor and plan never use the network; `upload` uses it only with a matching --confirm token",
    }

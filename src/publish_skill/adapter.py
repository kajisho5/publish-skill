"""ffmpeg-skill adapter: the only module that starts a process (ADR-002).

ffmpeg-skill owns the platform delivery specs (codecs, frame size, aspect, frame rate, loudness). This skill reads them
by running ffmpeg-skill's own tools and never copies their values:

  scripts/_contract.py --json --static   capability detection: the flags of `export` and `check`, the platform names
                                         `check --platform` accepts, and that the contract declares no network use
  scripts/export.py --list               the delivery presets (plan names the one that re-exports a failing file)
  scripts/check.py <file> --platform <p> --json --no-loudness
                                         the deliverable against the platform's delivery spec (rows PASS/WARN/FAIL)

There is no version window: a checkout is usable when the flags this adapter emits are present, whatever its version.
Every call is [sys.executable, <dir>/scripts/<tool>.py, <argv...>], an argv list built here from a resolved absolute
path and names checked against the detected contract; never a shell, stdin closed, a minimal environment, a timeout.
Neither tool uses the network: ffmpeg-skill's contract declares `execution.network: false`, and detection refuses a
checkout that does not."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TOOLS_USED = ("export", "check")
# argparse dests of the ffmpeg-skill tools this adapter emits; every one must be in the located checkout's contract
REQUIRED_FLAGS: Dict[str, Tuple[str, ...]] = {"export": ("list",), "check": ("input", "platform", "json", "no_loudness")}
DETECT_TIMEOUT = 60.0
CHECK_TIMEOUT = 600.0
_ENV_KEEP = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "TERM",
             "SYSTEMROOT", "SYSTEMDRIVE", "PATHEXT", "COMSPEC", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA")


class AdapterError(Exception):
    """ffmpeg-skill could not answer (missing, unsupported, failed, timed out, unparsable). `plan` reports the reason
    as an UNCHECKED row instead of guessing; it is never a PublishError on its own."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


def _clean_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run(directory: Path, tool: str, argv: List[str], timeout: float) -> Tuple[int, str, str]:
    script = directory / "scripts" / f"{tool}.py"
    if not script.is_file():
        raise AdapterError("tool_missing", f"{script} does not exist")
    cmd = [sys.executable, str(script)] + argv
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=_clean_env(), timeout=timeout, shell=False, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        raise AdapterError("timeout", f"ffmpeg-skill/{tool} did not finish in {timeout:g} s")
    except OSError as e:
        raise AdapterError("not_runnable", f"ffmpeg-skill/{tool} could not start: {e}")
    return proc.returncode, proc.stdout, proc.stderr


def _json(text: str, tool: str) -> Dict[str, Any]:
    try:
        doc = json.loads(text)
    except ValueError:
        raise AdapterError("output_unparsed", f"ffmpeg-skill/{tool} printed no JSON document")
    if not isinstance(doc, dict):
        raise AdapterError("output_unparsed", f"ffmpeg-skill/{tool} printed JSON that is not an object")
    return doc


def check_contract(doc: Dict[str, Any], check_platform: Optional[str] = None) -> Tuple[List[str], Dict[str, Any]]:
    """Compare ffmpeg-skill's contract with what this adapter emits. Returns (problems, features); no problems means
    `plan` can use the checkout. Pure: unit-testable against any contract document."""
    problems: List[str] = []
    tools = {t.get("name"): t for t in doc.get("tools") or [] if isinstance(t, dict)}
    features: Dict[str, Any] = {"check_platforms": []}
    for tool, flags in REQUIRED_FLAGS.items():
        spec = tools.get(tool)
        if spec is None:
            problems.append(f"ffmpeg-skill tool {tool!r} is missing")
            continue
        props = (spec.get("input_schema") or {}).get("properties") or {}
        missing = [f for f in flags if f not in props]
        if missing:
            problems.append(f"ffmpeg-skill/{tool} lacks flag(s) {missing}")
        if tool == "check":
            features["check_platforms"] = sorted((props.get("platform") or {}).get("enum") or [])
    if check_platform and features["check_platforms"] and check_platform not in features["check_platforms"]:
        problems.append(f"ffmpeg-skill/check does not accept --platform {check_platform}")
    if (doc.get("execution") or {}).get("network") is not False:
        problems.append("ffmpeg-skill's contract does not declare execution.network: false")
    return problems, features


def parse_export_list(text: str) -> List[str]:
    """Preset names from `export.py --list` (one `<name> <description>` line per preset)."""
    return [line.split()[0] for line in text.splitlines() if line.strip() and not line[0].isspace()]


def detect(directory: str, check_platform: Optional[str] = None) -> Dict[str, Any]:
    """What the checkout at `directory` offers. status: OK (usable) | UNSUPPORTED (problems lists why)."""
    d = Path(directory).resolve()
    out: Dict[str, Any] = {"path": str(d), "tools_used": list(TOOLS_USED)}
    try:
        rc, stdout, _ = _run(d, "_contract", ["--json", "--static"], DETECT_TIMEOUT)
        doc = _json(stdout, "_contract")
    except AdapterError as e:
        return dict(out, status="UNSUPPORTED", problems=[e.message], reason=e.reason)
    problems, features = check_contract(doc, check_platform)
    skill = doc.get("skill") if isinstance(doc.get("skill"), dict) else {}
    out.update(version=skill.get("version"), contract_version=doc.get("contract_version"), **features)
    if not problems:
        try:
            rc, stdout, _ = _run(d, "export", ["--list"], DETECT_TIMEOUT)
            out["export_presets"] = parse_export_list(stdout) if rc == 0 else []
            if rc != 0:
                problems.append(f"ffmpeg-skill/export --list exited {rc}")
        except AdapterError as e:
            problems.append(e.message)
    out["problems"] = problems
    out["status"] = "OK" if not problems else "UNSUPPORTED"
    return out


def check(directory: str, path: str, check_platform: str) -> Dict[str, Any]:
    """ffmpeg-skill's `check` document for one file: {"checks": [{check, status, value, expected, fix, ...}], ...}.
    check.py exits 1 when a row FAILs and still prints its document, so the exit code alone is not an error."""
    rc, stdout, stderr = _run(Path(directory).resolve(), "check",
                              [os.path.abspath(path), "--platform", check_platform, "--json", "--no-loudness"], CHECK_TIMEOUT)
    doc = _json(stdout, "check")
    if not isinstance(doc.get("checks"), list):
        err = doc.get("error") if isinstance(doc.get("error"), dict) else {}
        raise AdapterError("check_failed", f"ffmpeg-skill/check exited {rc}: {err.get('message') or stderr.strip()[-300:]}")
    return doc

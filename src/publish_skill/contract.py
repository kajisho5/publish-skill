"""Machine-readable contract (`publish contract --json`), in the shape the AI Video Production OS registry reads
(skill_id, version, contract_version, provides, dependencies, not_provided)."""
from __future__ import annotations

from typing import Any, Dict

from . import PACKAGE_NAME, SKILL_ID, __version__
from .credentials import REDACTED, SOURCE
from .doctor import DOCTOR_SCHEMA, FFMPEG_SKILL_ENV, IMPLEMENTED_OPS, OP_STEP
from .errors import ERROR_TABLE
from .platforms import get_platform, platform_ids
from .upload import TOKEN_PREFIX

CONTRACT_VERSION = "1.0"
CAPABILITY_IDS = {"plan": "publish.plan", "upload": "publish.upload"}
_OP_TOOLS = {
    "plan": {"role": "measurement", "network": False, "writes_media": False,
             "description": "Check a deliverable and its title / description / tags / privacy / thumbnail / captions against "
                            "the platform's upload limits, offline (structured FAIL, nothing uploaded)",
             "argv": ["plan", "<deliverable>", "--platform <id>", "--json"]},
    "upload": {"role": "execution", "network": True, "writes_media": False,
               "description": "Upload a deliverable to a platform. Dry run unless --confirm carries the token the dry run "
                              "printed for exactly this file and platform",
               "argv": ["upload", "<deliverable>", "--platform <id>", "[--confirm <token>]", "--json"]},
}


def tools() -> list:
    out = []
    for op, spec in _OP_TOOLS.items():
        out.append({"tool_id": f"{SKILL_ID}/{op}", "mutates_input": False, "deterministic": op == "plan",
                    "implemented": IMPLEMENTED_OPS[op], "roadmap_step": OP_STEP[op], **spec})
    out[-1]["implemented_part"] = "the dry run and the confirmation gate; every platform uploader is a NOT_IMPLEMENTED stub"
    out += [
        {"tool_id": f"{SKILL_ID}/doctor", "role": "diagnostic", "network": False, "writes_media": False, "mutates_input": False,
         "deterministic": False, "implemented": True, "schema": DOCTOR_SCHEMA,
         "description": "Python, ffmpeg-skill, and per platform: uploader implemented, credentials present / absent (never values)",
         "argv": ["doctor", "[--offline]", "[--ffmpeg-skill <dir>]", "--json"]},
        {"tool_id": f"{SKILL_ID}/platforms", "role": "diagnostic", "network": False, "writes_media": False, "mutates_input": False,
         "deterministic": False, "implemented": True, "description": "Registered platforms with status and credential variable names",
         "argv": ["platforms", "--json"]},
        {"tool_id": f"{SKILL_ID}/contract", "role": "metadata", "network": False, "writes_media": False, "mutates_input": False,
         "deterministic": True, "implemented": True, "description": "This contract", "argv": ["contract", "--json"]},
    ]
    return out


def contract() -> Dict[str, Any]:
    deps = [
        {"id": "ffmpeg-skill", "skill_id": "ffmpeg-skill", "kind": "skill", "required": False, "for": [CAPABILITY_IDS["plan"]],
         "needed_from_step": OP_STEP["plan"],
         "version_range": "any release whose contract declares `export --list` and `check` (capability detection, step 2)",
         "reads": "the platform delivery specs (ffmpeg-skill owns them; this skill never copies them)",
         "located_by": ["--ffmpeg-skill"] + [f"${k}" for k in FFMPEG_SKILL_ENV]
                       + ["~/.claude/skills/ffmpeg-skill", "./vendor/ffmpeg-skill", "../ffmpeg-skill"]},
    ]
    return {
        "skill_id": SKILL_ID,
        "id": SKILL_ID,
        "skill": {"id": SKILL_ID, "version": __version__, "package": PACKAGE_NAME},
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
        "provides": [{"id": CAPABILITY_IDS[op], "lifecycle": "EXPERIMENTAL" if IMPLEMENTED_OPS[op] else "PROPOSED",
                      "tool_id": f"{SKILL_ID}/{op}"} for op in CAPABILITY_IDS],
        "dependencies": deps,
        "not_provided": ["platform delivery specs: codecs, resolution, bitrate, loudness (ffmpeg-skill owns them)",
                         "encoding or transcoding for a platform (ffmpeg-skill export)",
                         "quality control of the deliverable (qc-skill; uploads come after QC)",
                         "deciding whether to publish (a human confirmation the agent records)",
                         "downloading media from a URL (ingest-skill)",
                         "credentials from files, flags or request fields (environment only; OS keychain in step 3)",
                         "uploading without --confirm"],
        "tools": tools(),
        "platforms": {"registered": {pid: get_platform(pid).spec() for pid in platform_ids()},
                      "per_platform_pr": "roadmap step 5"},
        "confirmation": {"required_for": [f"{SKILL_ID}/upload"], "flag": "--confirm", "token_prefix": TOKEN_PREFIX,
                         "token_binds": ["platform", "deliverable sha256", "deliverable size"],
                         "default": "dry run (no network, nothing uploaded)",
                         "from_environment_or_config": False,
                         "decision": "docs/decisions.md ADR-001"},
        "credentials": {"source": SOURCE, "printed": False, "reported_as": ["present", "absent"], "redaction": REDACTED,
                        "keychain": "roadmap step 3"},
        "execution": {"canonical_invocation": ["publish", "<command>", "...", "--json"], "network": True,
                      "network_tools": [f"{SKILL_ID}/upload"], "network_requires": "--confirm <token>",
                      "network_in_this_release": False, "shell": False, "input_is_read_only": True},
        "errors": {code: {"exit_code": ex, "retryable": rt} for code, (ex, rt) in ERROR_TABLE.items()},
        "error_shape": {"ok": False, "error": {"code": "<errors key>", "message": "str", "retryable": "bool", "details": "object"}},
    }

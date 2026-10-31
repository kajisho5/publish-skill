"""publish CLI.

stdout: with --json exactly one JSON document, on success and failure alike. stderr: diagnostics only. Every
document passes through credentials.redact before it is printed. Exit code: 0 on success; doctor exits 1 when its
status is `fail`; an error exits errors.EXIT_CODES[code].

  publish contract --json                          the contract (alias: skill --json)
  publish doctor --json                            Python, ffmpeg-skill, platforms [--offline] [--ffmpeg-skill DIR]
  publish platforms --json                         registered platforms and their credential variables
  publish upload FILE --platform ID --json         dry run: what would be uploaded, and the confirmation token
  publish upload FILE --platform ID --confirm TOKEN --json
                                                   the upload itself (every uploader is a stub in this release)
  publish plan FILE --platform ID --json           roadmap step 2 (NOT_IMPLEMENTED)"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import PACKAGE_NAME, SKILL_ID, __version__
from .contract import contract
from .credentials import redact
from .doctor import OP_STEP, doctor, platforms_report
from .errors import PublishError
from .platforms import get_platform
from .upload import upload


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="publish", description=f"{PACKAGE_NAME} {__version__}: upload finished deliverables "
                                 "to delivery platforms, after QC (dry run unless --confirm)")
    ap.add_argument("--version", action="version", version=f"{PACKAGE_NAME} {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("contract", "skill"):
        c = sub.add_parser(name, help="print the machine-readable contract")
        c.add_argument("--json", action="store_true", help="JSON on stdout (exactly one document)")
    d = sub.add_parser("doctor", help="what is ready, what is missing, how to get it (never uses the network)")
    d.add_argument("--ffmpeg-skill", help="ffmpeg-skill checkout directory (default: env / ~/.claude/skills/ffmpeg-skill / ./vendor / ..)")
    d.add_argument("--offline", action="store_true", help="accepted for symmetry: doctor never uses the network")
    d.add_argument("--json", action="store_true")
    p = sub.add_parser("platforms", help="registered platforms, uploader status, credential variable names")
    p.add_argument("--json", action="store_true")
    u = sub.add_parser("upload", help="dry run by default; uploads only with the --confirm token the dry run printed")
    u.add_argument("deliverable")
    u.add_argument("--platform", required=True)
    u.add_argument("--confirm", metavar="TOKEN", help="the confirm_token printed by the dry run for this file and platform")
    u.add_argument("--json", action="store_true")
    pl = sub.add_parser("plan", help="check a deliverable against the platform's upload limits (roadmap step 2)")
    pl.add_argument("deliverable")
    pl.add_argument("--platform", required=True)
    pl.add_argument("--json", action="store_true")
    return ap


def _emit(doc: Dict[str, Any], as_json: bool) -> None:
    doc = redact(doc)
    if as_json:
        sys.stdout.write(json.dumps(doc, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    else:
        _human(doc)
    sys.stdout.flush()


def _human(doc: Dict[str, Any]) -> None:
    if doc.get("error"):
        err = doc["error"]
        sys.stderr.write(f"error [{err['code']}]: {err['message']}\n")
    elif "checks" in doc:
        print(f"{PACKAGE_NAME} {__version__}: {doc['status']}")
        print(f"  ffmpeg-skill  {doc['checks']['ffmpeg_skill']['status']}")
        for pid, p in doc["checks"]["platforms"].items():
            vars_ = ", ".join(f"{n}={s}" for n, s in p["credentials"]["variables"].items())
            print(f"  platform {pid:10} implemented={p['implemented']} ready={p['ready']}  {vars_}")
        for w in doc["warnings"] + doc["problems"]:
            print(f"  - {w}")
    elif "dry_run" in doc:
        print(f"dry run: would upload {doc['input']['path']} ({doc['input']['size']} bytes) to {doc['platform']['name']}; nothing sent")
        print(f"  {doc['next_step']}")
    elif "platforms" in doc and "skill_id" not in doc:
        for pid, p in doc["platforms"].items():
            print(f"{pid:10} implemented={p['implemented']} step={p['roadmap_step']} credentials={'complete' if p['credentials']['complete'] else 'incomplete'}")
    else:
        print(f"{doc['skill_id']} {doc['version']}: " + ", ".join(f"{p['id']} ({p['lifecycle']})" for p in doc["provides"]))


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    as_json = bool(getattr(args, "json", False))
    try:
        if args.cmd in ("contract", "skill"):
            _emit(contract(), as_json)
            return 0
        if args.cmd == "doctor":
            doc = doctor(args.ffmpeg_skill, offline=args.offline)
            _emit(doc, as_json)
            return 1 if doc["status"] == "fail" else 0
        if args.cmd == "platforms":
            _emit({"ok": True, "skill": {"id": SKILL_ID, "version": __version__}, "platforms": platforms_report()}, as_json)
            return 0
        if args.cmd == "upload":
            _emit(upload(args.deliverable, args.platform, confirm=args.confirm), as_json)
            return 0
        if args.cmd == "plan":
            get_platform(args.platform)
            raise PublishError("NOT_IMPLEMENTED", f"`publish plan` arrives in roadmap step {OP_STEP['plan']}",
                               {"roadmap_step": OP_STEP["plan"]})
    except PublishError as e:
        _emit({"ok": False, "skill": {"id": SKILL_ID, "version": __version__}, "error": e.to_dict()}, as_json)
        return e.exit_code
    except KeyboardInterrupt:
        err = PublishError("CANCELLED", "interrupted; nothing was uploaded")
        _emit({"ok": False, "skill": {"id": SKILL_ID, "version": __version__}, "error": err.to_dict()}, as_json)
        return err.exit_code
    except Exception as e:  # pragma: no cover - a bug; still one JSON document
        err = PublishError("INTERNAL_ERROR", f"{type(e).__name__}: {e}")
        _emit({"ok": False, "skill": {"id": SKILL_ID, "version": __version__}, "error": err.to_dict()}, as_json)
        return err.exit_code
    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":
    sys.exit(main())

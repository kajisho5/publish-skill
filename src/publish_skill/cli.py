"""publish CLI.

stdout: with --json exactly one JSON document, on success and failure alike. stderr: diagnostics only. Every
document passes through credentials.redact before it is printed. Exit code: 0 on success; doctor exits 1 when its
status is `fail`; plan exits 1 when its verdict is FAIL or INCOMPLETE; an error exits errors.EXIT_CODES[code].

  publish contract --json                          the contract (alias: skill --json)
  publish doctor --json                            Python, ffmpeg-skill, platforms [--offline] [--ffmpeg-skill DIR]
  publish platforms --json                         registered platforms and their credential variables
  publish auth status [--platform ID] --json       credentials present / absent, environment and OS keychain
  publish auth login --platform ID [--variable NAME ...] --json
                                                   store credentials in the OS keychain, read from stdin (one per
                                                   line) or a hidden prompt; never from the command line
  publish auth logout --platform ID [--variable NAME ...] --json
                                                   remove them from the OS keychain
  publish plan FILE --platform ID [METADATA] --json
                                                   the deliverable and its metadata against the platform's limits
                                                   (offline; delivery spec rows from ffmpeg-skill check)
  publish upload FILE --platform ID [METADATA] --json
                                                   dry run: what would be uploaded, and the confirmation token
  publish upload FILE --platform ID [METADATA] --confirm TOKEN --json
                                                   the upload itself, after QC and a human's approval (YouTube,
                                                   Vimeo, TikTok, X, LinkedIn: resumable or chunked uploads;
                                                   prints a receipt)

  METADATA: [--title T] [--description D | --description-file F] [--tag T ...] [--privacy private|unlisted|public]
            [--thumbnail IMAGE] [--caption FILE ... --caption-language LANG ...]
            (the same flags on plan and upload; the token binds them)"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List, Optional

from . import PACKAGE_NAME, SKILL_ID, __version__
from . import auth
from .contract import contract
from .credentials import redact
from .doctor import doctor, platforms_report
from .errors import PublishError
from .metadata import DEFAULT_PRIVACY, build
from .plan import plan
from .platforms import get_platform
from .upload import upload


def _metadata_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("metadata (the same flags on plan and upload; the confirmation token binds every one)")
    g.add_argument("--title")
    g.add_argument("--description")
    g.add_argument("--description-file", metavar="FILE", help="UTF-8 text file holding the description")
    g.add_argument("--tag", action="append", dest="tags", metavar="TAG", help="one tag (repeat for more)")
    g.add_argument("--privacy", help=f"private | unlisted | public (default: {DEFAULT_PRIVACY})")
    g.add_argument("--thumbnail", metavar="IMAGE")
    g.add_argument("--caption", action="append", dest="captions", metavar="FILE", help="a captions file (repeat for more)")
    g.add_argument("--caption-language", action="append", dest="caption_languages", metavar="LANG",
                   help="the BCP-47 language of each --caption, in the same order (e.g. en, ja, es-419)")


def _metadata(args: argparse.Namespace):
    return build(title=args.title, description=args.description, description_file=args.description_file, tags=args.tags,
                 privacy=args.privacy, thumbnail=args.thumbnail, captions=args.captions,
                 caption_languages=args.caption_languages)


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
    a = sub.add_parser("auth", help="credentials in the OS keychain (values are read from stdin or a hidden prompt, "
                       "never from the command line, and never printed)")
    asub = a.add_subparsers(dest="auth_cmd", required=True)
    for name, hlp in (("status", "credentials present / absent in the environment and the OS keychain"),
                      ("login", "store credentials in the OS keychain: one value per line on stdin, in the order of the "
                                "platform's variables (or of --variable), or a hidden prompt on a terminal"),
                      ("logout", "remove credentials from the OS keychain")):
        x = asub.add_parser(name, help=hlp)
        x.add_argument("--platform", required=name != "status")
        if name != "status":
            x.add_argument("--variable", action="append", dest="variables", metavar="NAME",
                           help="only this credential variable (repeat for more; default: all of the platform's)")
        x.add_argument("--json", action="store_true")
    u = sub.add_parser("upload", help="dry run by default; uploads only with the --confirm token the dry run printed")
    u.add_argument("deliverable")
    u.add_argument("--platform", required=True)
    u.add_argument("--confirm", metavar="TOKEN", help="the confirm_token printed by the dry run for this file, metadata and platform")
    u.add_argument("--json", action="store_true")
    _metadata_args(u)
    pl = sub.add_parser("plan", help="check a deliverable and its metadata against the platform's limits (offline)")
    pl.add_argument("deliverable")
    pl.add_argument("--platform", required=True)
    pl.add_argument("--ffmpeg-skill", help="ffmpeg-skill checkout directory (default: env / ~/.claude/skills/ffmpeg-skill / ./vendor / ..)")
    pl.add_argument("--json", action="store_true")
    _metadata_args(pl)
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
    elif "auth" in doc:
        _human_auth(doc)
    elif "verdict" in doc:
        print(f"plan {doc['input']['path']} for {doc['platform']['name']}: {doc['verdict']} (nothing uploaded)")
        for r in doc["checks"]:
            print(f"  {r['status']:9} {r['check']}: {r['value']!r} (limit {r['limit']!r}, {r['owner']})"
                  + (f"  fix: {r['fix']}" if r.get("fix") and r["status"] != "PASS" else ""))
        print(f"  {doc['next_step']}")
    elif "checks" in doc:
        print(f"{PACKAGE_NAME} {__version__}: {doc['status']}")
        print(f"  ffmpeg-skill  {doc['checks']['ffmpeg_skill']['status']}")
        kc = doc["checks"]["keychain"]
        print(f"  keychain      {kc['status']} ({kc['backend'] or kc['reason']})")
        for pid, p in doc["checks"]["platforms"].items():
            vars_ = ", ".join(f"{n}={s}" for n, s in p["credentials"]["variables"].items())
            print(f"  platform {pid:10} implemented={p['implemented']} ready={p['ready']}  {vars_}")
        for w in doc["warnings"] + doc["problems"]:
            print(f"  - {w}")
    elif doc.get("uploaded") and "result" in doc:
        r = doc["result"]
        print(f"uploaded {doc['input']['path']} to {doc['platform']['name']}: video {r['video_id']} (privacy {r['privacy']})"
              + (f" {r['url']}" if r.get("url") else ""))
        if r.get("thumbnail"):
            print("  thumbnail set")
        for c in r.get("captions") or []:
            print(f"  captions {c['language']} added")
        for w in r.get("warnings") or []:
            print(f"  - {w}")
    elif "dry_run" in doc:
        print(f"dry run: would upload {doc['input']['path']} ({doc['input']['size']} bytes) to {doc['platform']['name']}; nothing sent")
        print(f"  {doc['next_step']}")
    elif "platforms" in doc and "skill_id" not in doc:
        for pid, p in doc["platforms"].items():
            print(f"{pid:10} implemented={p['implemented']} step={p['roadmap_step']} credentials={'complete' if p['credentials']['complete'] else 'incomplete'}")
    else:
        print(f"{doc['skill_id']} {doc['version']}: " + ", ".join(f"{p['id']} ({p['lifecycle']})" for p in doc["provides"]))


def _human_auth(doc: Dict[str, Any]) -> None:
    if doc["auth"] == "login":
        print(f"stored {', '.join(doc['stored'])} in the {doc['backend']} (service {doc['service']})")
    elif doc["auth"] == "logout":
        print(f"removed from the {doc['backend']}: {', '.join(doc['removed']) or 'nothing'}"
              + (f"; not found: {', '.join(doc['not_found'])}" if doc["not_found"] else ""))
    else:
        kc = doc["keychain"]
        print(f"keychain: {kc['status']} ({kc['backend'] or kc['reason']})")
    platforms = doc.get("platforms") or {doc["platform"]: doc["credentials"]}
    for pid, c in platforms.items():
        vars_ = ", ".join(f"{n}={s}" for n, s in c["variables"].items())
        print(f"  platform {pid:10} source={c['source']} complete={c['complete']}  {vars_}")
    for w in doc.get("warnings", []):
        print(f"  - {w}")


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
        if args.cmd == "auth":
            if args.auth_cmd == "status":
                _emit(auth.status(args.platform), as_json)
            elif args.auth_cmd == "login":
                _emit(auth.login(args.platform, args.variables), as_json)
            else:
                _emit(auth.logout(args.platform, args.variables), as_json)
            return 0
        if args.cmd in ("upload", "plan"):
            get_platform(args.platform)                       # UNKNOWN_PLATFORM before any file is read
        if args.cmd == "upload":
            _emit(upload(args.deliverable, args.platform, confirm=args.confirm, metadata=_metadata(args)), as_json)
            return 0
        if args.cmd == "plan":
            doc = plan(args.deliverable, args.platform, _metadata(args), ffmpeg_skill_dir=args.ffmpeg_skill)
            _emit(doc, as_json)
            return 0 if doc["ok"] else 1
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

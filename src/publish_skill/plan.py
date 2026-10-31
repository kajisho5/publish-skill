"""`publish plan`: check a deliverable and its metadata against the platform's limits, offline (ADR-002).

Two sources of rows, each owned by one repository:
  owner "publish"       the upload-side limits in limits.py (file size, duration, file format, title, description,
                        tags, privacy, thumbnail, captions), each row citing the page its limit came from
  owner "ffmpeg-skill"  the delivery spec rows of ffmpeg-skill `check --platform <p>` (codec, frame size, aspect, frame
                        rate, ...), passed through as ffmpeg-skill wrote them; their values are never restated here

Row status: PASS | WARN | FAIL | UNCHECKED (could not be checked: ffmpeg-skill missing or failed). The verdict is FAIL
when any row fails, otherwise INCOMPLETE when any row is unchecked, otherwise WARN or PASS. `ok` is true for PASS and
WARN only, and the CLI exits 1 otherwise. Nothing is uploaded, no credential is read and no connection is opened."""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from . import SKILL_ID, __version__
from . import adapter
from . import limits as L
from .doctor import locate_ffmpeg_skill
from .errors import PublishError
from .metadata import DEFAULT_METADATA, Metadata, image_kind
from .platforms import get_platform

PLAN_SCHEMA = f"{SKILL_ID}/plan@1"
STATUSES = ("PASS", "WARN", "FAIL", "UNCHECKED")
# the shape of a BCP-47 tag (RFC 5646): a 2-3 letter (or 4-8 letter) language, then hyphenated 1-8 character subtags
_BCP47 = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$|^[A-Za-z]{4,8}(?:-[A-Za-z0-9]{1,8})*$")


def _row(check: str, status: str, value: Any, limit: Any, platform_id: str, key: Optional[str], fix: str = "",
         owner: str = "publish") -> Dict[str, Any]:
    row: Dict[str, Any] = {"check": check, "status": status, "value": value, "limit": limit, "owner": owner}
    entry = L.limits_of(platform_id).get(key or "")
    if entry:
        row["limit_key"] = key
        if entry.get("source"):
            row["source"], row["accessed"] = entry["source"], entry["accessed"]
    if fix:
        row["fix"] = fix
    return row


def _bytes_row(check: str, size: int, platform_id: str, key: str, fix: str) -> Dict[str, Any]:
    lim = L.value(platform_id, key)
    ok = lim is None or size <= lim
    return _row(check, "PASS" if ok else "FAIL", size, lim, platform_id, key, "" if ok else fix)


def file_checks(platform_id: str, path: str, size: int) -> List[Dict[str, Any]]:
    rows = [_bytes_row("file size", size, platform_id, "video.max_bytes",
                       "re-export smaller: ffmpeg-skill export (a lower quality or resolution)")]
    formats = L.value(platform_id, "video.file_formats")
    if formats:
        ext = os.path.splitext(path)[1].lower()
        ok = ext in formats
        rows.append(_row("file format", "PASS" if ok else "WARN", ext or "(no extension)", formats, platform_id,
                         "video.file_formats", "" if ok else "re-export to .mp4: ffmpeg-skill export --preset <platform>"))
    return rows


def duration_checks(platform_id: str, seconds: Optional[float], why_unchecked: str = "") -> List[Dict[str, Any]]:
    hard, soft = L.value(platform_id, "video.max_duration_s"), L.value(platform_id, "video.unverified_max_duration_s")
    if seconds is None:
        return [_row("duration", "UNCHECKED", None, hard, platform_id, "video.max_duration_s",
                     why_unchecked or "ffmpeg-skill measures the duration; see the delivery spec row")]
    rows = [_row("duration", "PASS" if hard is None or seconds <= hard else "FAIL", seconds, hard, platform_id,
                 "video.max_duration_s", "" if hard is None or seconds <= hard else "shorten it: ffmpeg-skill cut")]
    if soft is not None and seconds > soft and (hard is None or seconds <= hard):
        rows.append(_row("duration (unverified account)", "WARN", seconds, soft, platform_id, "video.unverified_max_duration_s",
                         "longer than an unverified account may upload: verify the account first"))
    return rows


def _forbidden(text: str, chars: Optional[List[str]]) -> List[str]:
    return sorted({c for c in (chars or []) if c in text})


def metadata_checks(platform_id: str, md: Metadata) -> List[Dict[str, Any]]:
    """The metadata rows. Pure: no file is read except the thumbnail's first bytes and caption files' encoding."""
    rows: List[Dict[str, Any]] = []
    max_title = L.value(platform_id, "title.max_chars")
    if md.title is None or not md.title.strip():
        rows.append(_row("title", "WARN", md.title, max_title, platform_id, "title.max_chars",
                         "set --title (the platform names the video otherwise)"))
    else:
        n = len(md.title)
        rows.append(_row("title length", "PASS" if max_title is None or n <= max_title else "FAIL", n, max_title,
                         platform_id, "title.max_chars",
                         "" if max_title is None or n <= max_title else f"shorten the title to {max_title} characters"))
        bad = _forbidden(md.title, L.value(platform_id, "title.forbidden_chars"))
        rows.append(_row("title characters", "FAIL" if bad else "PASS", bad, L.value(platform_id, "title.forbidden_chars"),
                         platform_id, "title.forbidden_chars", f"remove {' '.join(bad)} from the title" if bad else ""))
    if md.description is not None:
        max_desc = L.value(platform_id, "description.max_bytes")
        n = len(md.description.encode("utf-8"))
        rows.append(_row("description length", "PASS" if max_desc is None or n <= max_desc else "FAIL", n, max_desc,
                         platform_id, "description.max_bytes",
                         "" if max_desc is None or n <= max_desc else f"shorten the description to {max_desc} UTF-8 bytes"))
        bad = _forbidden(md.description, L.value(platform_id, "description.forbidden_chars"))
        rows.append(_row("description characters", "FAIL" if bad else "PASS", bad,
                         L.value(platform_id, "description.forbidden_chars"), platform_id, "description.forbidden_chars",
                         f"remove {' '.join(bad)} from the description" if bad else ""))
    if md.tags:
        max_tags = L.value(platform_id, "tags.max_chars")
        n = L.tags_length(md.tags)
        rows.append(_row("tags length", "PASS" if max_tags is None or n <= max_tags else "FAIL", n, max_tags, platform_id,
                         "tags.max_chars", "" if max_tags is None or n <= max_tags else
                         f"drop tags: commas count, and a tag with a space counts two quotation marks ({max_tags} max)"))
    values = L.value(platform_id, "privacy.values")
    ok = values is None or md.privacy in values
    rows.append(_row("privacy", "PASS" if ok else "FAIL", md.privacy, values, platform_id, "privacy.values",
                     "" if ok else f"--privacy one of {', '.join(values or [])}"))
    if md.thumbnail:
        rows.append(_bytes_row("thumbnail size", md.thumbnail["size"], platform_id, "thumbnail.max_bytes",
                               "save the thumbnail smaller (JPEG at a lower quality)"))
        kind = image_kind(md.thumbnail["path"])
        types = L.value(platform_id, "thumbnail.types")
        ok = types is None or kind in types
        rows.append(_row("thumbnail type", "PASS" if ok else "FAIL", kind, types, platform_id, "thumbnail.types",
                         "" if ok else "save the thumbnail as JPEG or PNG"))
    for c in md.captions:
        name = os.path.basename(c["path"])
        rows.append(_bytes_row(f"captions size ({name})", c["size"], platform_id, "captions.max_bytes", "split the captions file"))
        ext = os.path.splitext(name)[1].lower()
        exts = L.value(platform_id, "captions.extensions")
        ok = exts is None or ext in exts
        rows.append(_row(f"captions format ({name})", "PASS" if ok else "WARN", ext or "(no extension)", exts, platform_id,
                         "captions.extensions", "" if ok else "convert to SubRip (.srt): subtitle-skill"))
        lang = c.get("language")
        ok = bool(lang) and bool(_BCP47.match(str(lang)))
        if L.value(platform_id, "captions.language") is not None:
            rows.append(_row(f"captions language ({name})", "PASS" if ok else "FAIL", lang,
                             L.value(platform_id, "captions.language"), platform_id, "captions.language",
                             "" if ok else "give its BCP-47 language tag: --caption-language en (one per --caption, in order)"))
        if ext in (L.value(platform_id, "captions.plain_utf8_extensions") or []):
            with open(c["path"], "rb") as fh:
                raw = fh.read()
            try:
                raw.decode("utf-8")
                utf8 = True
            except UnicodeDecodeError:
                utf8 = False
            rows.append(_row(f"captions encoding ({name})", "PASS" if utf8 else "FAIL", "utf-8" if utf8 else "not utf-8",
                             "plain UTF-8", platform_id, "captions.plain_utf8_extensions",
                             "" if utf8 else "re-save the captions file as UTF-8"))
    return rows


def _seconds(value: Any) -> Optional[float]:
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*s\s*$", str(value)) if value is not None else None
    return float(m.group(1)) if m else None


def delivery_checks(platform_id: str, path: str, ffmpeg_skill_dir: Optional[str]) -> Dict[str, Any]:
    """ffmpeg-skill's rows for this deliverable, and the duration it measured. Never raises for ffmpeg-skill trouble:
    the reason becomes an UNCHECKED row."""
    check_platform = get_platform(platform_id).ffmpeg_skill_platform
    located = locate_ffmpeg_skill(ffmpeg_skill_dir)
    info: Dict[str, Any] = {"status": located["status"], "check_platform": check_platform}
    if located["status"] != "OK":
        info.update(tried=located["tried"], install=located["install"])
        return {"info": info, "rows": [_row("delivery spec", "UNCHECKED", None, None, platform_id, None,
                                            "ffmpeg-skill not found: " + located["install"], owner="ffmpeg-skill")],
                "duration": None, "why": "ffmpeg-skill not found"}
    det = adapter.detect(located["path"], check_platform)
    info.update({k: det.get(k) for k in ("path", "status", "version", "contract_version", "problems")})
    preset = check_platform if check_platform in (det.get("export_presets") or []) else None
    info["export_preset"] = preset
    if det["status"] != "OK":
        return {"info": info, "rows": [_row("delivery spec", "UNCHECKED", None, None, platform_id, None,
                                            "ffmpeg-skill is unsupported: " + "; ".join(det["problems"]), owner="ffmpeg-skill")],
                "duration": None, "why": "ffmpeg-skill unsupported"}
    try:
        doc = adapter.check(located["path"], path, check_platform)
    except adapter.AdapterError as e:
        info["error"] = {"reason": e.reason, "message": e.message}
        return {"info": info, "rows": [_row("delivery spec", "UNCHECKED", None, None, platform_id, None,
                                            f"ffmpeg-skill/check could not run ({e.reason}): {e.message}", owner="ffmpeg-skill")],
                "duration": None, "why": f"ffmpeg-skill/check {e.reason}"}
    rows, duration = [], None
    for r in doc["checks"]:
        if not isinstance(r, dict):
            continue
        status = r.get("status") if r.get("status") in ("PASS", "WARN", "FAIL") else "UNCHECKED"
        row = {"check": str(r.get("check")), "status": status, "value": r.get("value"), "limit": r.get("expected"),
               "owner": "ffmpeg-skill", "source": f"ffmpeg-skill/check --platform {check_platform}"}
        for k in ("fix", "reason", "kind"):
            if r.get(k):
                row[k] = r[k]
        if r.get("check") == "duration":
            duration = _seconds(r.get("value"))
        rows.append(row)
    return {"info": info, "rows": rows, "duration": duration, "why": "ffmpeg-skill reported no parsable duration"}


def verdict(rows: List[Dict[str, Any]]) -> str:
    statuses = {r["status"] for r in rows}
    for v in ("FAIL", "UNCHECKED", "WARN"):
        if v in statuses:
            return "INCOMPLETE" if v == "UNCHECKED" else v
    return "PASS"


def plan(path: str, platform_id: str, metadata: Metadata = DEFAULT_METADATA,
         ffmpeg_skill_dir: Optional[str] = None) -> Dict[str, Any]:
    platform = get_platform(platform_id)                      # UNKNOWN_PLATFORM before the file is read
    if not path or not os.path.isfile(path):
        raise PublishError("INPUT_NOT_FOUND", f"not a regular file: {path}", {"path": path})
    item = {"path": os.path.abspath(path), "size": os.path.getsize(path)}
    delivery = delivery_checks(platform_id, item["path"], ffmpeg_skill_dir)
    rows = (file_checks(platform_id, item["path"], item["size"])
            + duration_checks(platform_id, delivery["duration"], delivery["why"])
            + metadata_checks(platform_id, metadata) + delivery["rows"])
    v = verdict(rows)
    counts = {s.lower(): sum(1 for r in rows if r["status"] == s) for s in STATUSES}
    nxt = {"PASS": "run `publish upload` with the same flags (a dry run) and show the human its confirm_token",
           "WARN": "review the WARN rows with the human, then run `publish upload` with the same flags (a dry run)",
           "FAIL": "fix the FAIL rows (each names its fix) and run plan again; do not upload",
           "INCOMPLETE": "make the UNCHECKED rows checkable (each says why) and run plan again; do not upload"}[v]
    return {"schema": PLAN_SCHEMA, "ok": v in ("PASS", "WARN"), "verdict": v,
            "skill": {"id": SKILL_ID, "version": __version__}, "platform": platform.spec(), "input": item,
            "metadata": metadata.to_dict(), "checks": rows, "counts": counts, "ffmpeg_skill": delivery["info"],
            "limits": dict(L.summary(platform_id), table="publish contract --json: upload_limits"),
            "uploaded": False, "network_used": False, "next_step": nxt}

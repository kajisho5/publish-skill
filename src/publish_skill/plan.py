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
    least = L.value(platform_id, "video.min_bytes")                  # LinkedIn states a minimum too (ADR-008)
    if least is not None:
        ok = size >= least
        rows.append(_row("file size (minimum)", "PASS" if ok else "FAIL", size, least, platform_id, "video.min_bytes",
                         "" if ok else "the file is smaller than the platform accepts: re-export it at a higher "
                                       "quality or longer"))
    formats = L.value(platform_id, "video.file_formats")
    if formats:
        ext = os.path.splitext(path)[1].lower()
        ok = ext in formats
        # WARN on YouTube (the bytes decide); FAIL where the table marks the list enforced (TikTok: the upload's
        # Content-Type must name one of them, ADR-006; X: initialize's media_type, ADR-007; LinkedIn: "File format: MP4.",
        # ADR-008; Facebook: the upload session's file_type, ADR-009; Instagram Reels: MOV or MP4, ADR-010)
        bad = "FAIL" if L.limits_of(platform_id)["video.file_formats"].get("enforced") else "WARN"
        rows.append(_row("file format", "PASS" if ok else bad, ext or "(no extension)", formats, platform_id,
                         "video.file_formats", "" if ok else "re-export to .mp4: ffmpeg-skill export --preset <platform>"))
    return rows


def duration_checks(platform_id: str, seconds: Optional[float], why_unchecked: str = "") -> List[Dict[str, Any]]:
    hard, soft = L.value(platform_id, "video.max_duration_s"), L.value(platform_id, "video.unverified_max_duration_s")
    if seconds is None:
        return [_row("duration", "UNCHECKED", None, hard, platform_id, "video.max_duration_s",
                     why_unchecked or "ffmpeg-skill measures the duration; see the delivery spec row")]
    rows = [_row("duration", "PASS" if hard is None or seconds <= hard else "FAIL", seconds, hard, platform_id,
                 "video.max_duration_s", "" if hard is None or seconds <= hard else "shorten it: ffmpeg-skill cut")]
    least = L.value(platform_id, "video.min_duration_s")             # LinkedIn: at least three seconds (ADR-008)
    if least is not None:
        rows.append(_row("duration (minimum)", "PASS" if seconds >= least else "FAIL", seconds, least, platform_id,
                         "video.min_duration_s", "" if seconds >= least else f"make it at least {least} s long"))
    if soft is not None and seconds > soft and (hard is None or seconds <= hard):
        rows.append(_row("duration (unverified account)", "WARN", seconds, soft, platform_id, "video.unverified_max_duration_s",
                         "longer than an unverified account may upload: verify the account first"))
    return rows


def _forbidden(text: str, chars: Optional[List[str]]) -> List[str]:
    return sorted({c for c in (chars or []) if c in text})


_FIELD_FLAGS = {"title": "--title", "description": "--description", "tags": "--tag", "thumbnail": "--thumbnail",
                "captions": "--caption"}


def unsupported_fields(platform_id: str, md: Metadata) -> List[str]:
    """The metadata fields that are set but that the platform's uploader does not send (Uploader.metadata_fields)."""
    sent = get_platform(platform_id).metadata_fields
    present = {"title": md.title is not None, "description": md.description is not None, "tags": bool(md.tags),
               "thumbnail": md.thumbnail is not None, "captions": bool(md.captions)}
    return [f for f in ("title", "description", "tags", "thumbnail", "captions") if present[f] and f not in sent]


def metadata_checks(platform_id: str, md: Metadata) -> List[Dict[str, Any]]:
    """The metadata rows. Pure: no file is read except the thumbnail's first bytes and caption files' encoding.
    A field the platform's uploader does not send is one FAIL row, so `upload` refuses it (METADATA_INVALID) instead of
    dropping part of what the token bound (ADR-005); its other rows are skipped."""
    rows: List[Dict[str, Any]] = []
    platform = get_platform(platform_id)
    unsupported = unsupported_fields(platform_id, md)
    for f in unsupported:
        rows.append(_row(f"{f} (not sent to {platform.name})", "FAIL", f, list(platform.metadata_fields), platform_id, None,
                         f"drop {_FIELD_FLAGS[f]}: the {platform.name} uploader sends "
                         f"{', '.join(platform.metadata_fields)} only; add it on {platform.name} after the upload"))
    if unsupported:
        md = Metadata(title=None if "title" in unsupported else md.title,
                      description=None if "description" in unsupported else md.description,
                      tags=[] if "tags" in unsupported else md.tags, privacy=md.privacy,
                      thumbnail=None if "thumbnail" in unsupported else md.thumbnail,
                      captions=[] if "captions" in unsupported else md.captions)
    # YouTube and Vimeo count characters, TikTok UTF-16 code units ("runes"), X weighted characters (twitter-text):
    # each table has one of the three keys
    by_utf16 = L.value(platform_id, "title.max_utf16_units") is not None
    by_weight = L.value(platform_id, "title.max_weighted") is not None
    title_key = ("title.max_weighted" if by_weight else "title.max_utf16_units" if by_utf16 else "title.max_chars")
    max_title = L.value(platform_id, title_key)
    if md.title is None or not md.title.strip():
        rows.append(_row("title", "WARN", md.title, max_title, platform_id, title_key,
                         "set --title (the platform names the video otherwise)"))
    elif by_weight:
        w = L.x_weighted_length(md.title)
        over = w["value"] > max_title
        status = "PASS" if not over else ("FAIL" if w["exact"] else "WARN")
        fix = "" if not over else (f"shorten the text to {max_title} weighted characters (CJK and emoji count 2)"
                                   if w["exact"] else "it has URLs or emoji, which X counts itself (a URL 23, an emoji "
                                   f"sequence 2); this upper bound is over {max_title}: check it in X's composer or "
                                   "shorten it")
        rows.append(_row("title length", status, w["value"], max_title, platform_id, title_key, fix))
    else:
        n = len(md.title.encode("utf-16-le")) // 2 if by_utf16 else len(md.title)
        unit = "UTF-16 code units" if by_utf16 else "characters"
        rows.append(_row("title length", "PASS" if max_title is None or n <= max_title else "FAIL", n, max_title,
                         platform_id, title_key,
                         "" if max_title is None or n <= max_title else f"shorten the title to {max_title} {unit}"))
        for key, sign, what in (("title.max_hashtags", "#", "hashtags"), ("title.max_mentions", "@", "@ tags")):
            most = L.value(platform_id, key)                           # Instagram's caption (ADR-010)
            if most is not None:
                n = len(re.findall(r"(?:^|(?<=\s))" + sign + r"\w+", md.title))
                rows.append(_row(f"title {what}", "PASS" if n <= most else "WARN", n, most, platform_id, key,
                                 "" if n <= most else f"keep at most {most} {what} (counted as {sign}word; the "
                                                      "platform's own count decides)"))
        if L.value(platform_id, "title.forbidden_chars") is not None:      # unknown: nothing is checked (Vimeo)
            bad = _forbidden(md.title, L.value(platform_id, "title.forbidden_chars"))
            rows.append(_row("title characters", "FAIL" if bad else "PASS", bad,
                             L.value(platform_id, "title.forbidden_chars"), platform_id, "title.forbidden_chars",
                             f"remove {' '.join(bad)} from the title" if bad else ""))
    if md.description is not None:
        # YouTube states the limit in UTF-8 bytes, Vimeo in characters: each platform's table has one of the two keys
        by_chars = L.value(platform_id, "description.max_chars") is not None
        key = "description.max_chars" if by_chars else "description.max_bytes"
        max_desc = L.value(platform_id, key)
        n = len(md.description) if by_chars else len(md.description.encode("utf-8"))
        unit = "characters" if by_chars else "UTF-8 bytes"
        rows.append(_row("description length", "PASS" if max_desc is None or n <= max_desc else "FAIL", n, max_desc,
                         platform_id, key,
                         "" if max_desc is None or n <= max_desc else f"shorten the description to {max_desc} {unit}"))
        if L.value(platform_id, "description.forbidden_chars") is not None:
            bad = _forbidden(md.description, L.value(platform_id, "description.forbidden_chars"))
            rows.append(_row("description characters", "FAIL" if bad else "PASS", bad,
                             L.value(platform_id, "description.forbidden_chars"), platform_id,
                             "description.forbidden_chars", f"remove {' '.join(bad)} from the description" if bad else ""))
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
    if not check_platform:                      # ffmpeg-skill has no delivery spec for this platform (ADR-005)
        why = f"ffmpeg-skill has no {platform_id} delivery spec (`check --platform`); ffmpeg-skill owns it"
        return {"info": {"status": "NO_SPEC", "check_platform": None, "reason": why},
                "rows": [_row("delivery spec", "UNCHECKED", None, None, platform_id, None,
                              why + "; check the file against the platform's own guidance by hand", owner="ffmpeg-skill")],
                "duration": None, "why": "ffmpeg-skill measures the duration with `check --platform`, which has no "
                                         f"{platform_id} spec"}
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

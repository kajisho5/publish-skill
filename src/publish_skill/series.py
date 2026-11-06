"""`publish series`: clips from one source uploaded in order, each its own CONFIRM, never sent twice (roadmap step 9,
ADR-012).

A manifest (UTF-8 JSON) lists the clips, in upload order, with their own titles and, optionally, the full video they
were cut from:

  {"full_video_url": "https://www.youtube.com/watch?v=...",    optional, https only
   "link_label": "Full video",                                 optional (the default is English)
   "privacy": "public",                                        optional default for every clip
   "clips": [{"file": "clip-01.mp4", "title": "...", "description": "...", "tags": ["..."], "privacy": "...",
              "thumbnail": "...", "captions": [{"file": "clip-01.en.srt", "language": "en"}]}, ...]}

File paths are relative to the manifest's directory. The link back to the full video is one line, `<label>: <url>`,
added after the clip's description where the platform sends a description, otherwise after its title (TikTok, X,
LinkedIn and Instagram Reels send the title as the caption or the post's text). The link is part of the clip's
metadata, so `plan`'s rows and the confirmation token cover it like anything else.

Every clip goes through upload.py's gate unchanged: without --confirm the series is a dry run that prints one
confirm_token per clip; with --confirm (repeatable) only clips whose own token is given are uploaded, in order, and the
run stops at the first clip without one. A token that matches no clip is refused before anything is sent.

A state file (default `<manifest>.publish-state.json`; --state) records each clip as it goes: `sending` before its
upload starts, `uploaded` (with the receipt's id, URL and privacy) after it, removed again when the upload failed
before anything was published. A later run skips uploaded clips and resumes at the first one that is not, so no clip
is sent twice. A clip whose outcome is unknown (a lost answer at the publish step, an interrupted process, a
`sending` left behind) is `uncertain`: the series stops there until a human has checked the platform and said what
happened (`--resolve N=uploaded` or `--resolve N=not-uploaded`). The state holds no credential, session URI or upload
link: the ids and URLs of published clips, the files' sha256 and the tokens, which are not secrets (ADR-001)."""
from __future__ import annotations

import datetime
import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from . import SKILL_ID, __version__
from . import credentials as creds
from .errors import PublishError
from .metadata import build
from .platforms import get_platform
from .upload import upload

STATE_SCHEMA = f"{SKILL_ID}/series-state@1"
SERIES_SCHEMA = f"{SKILL_ID}/series@1"
DEFAULT_LINK_LABEL = "Full video"
STATE_SUFFIX = ".publish-state.json"
_CLIP_KEYS = {"file", "title", "description", "tags", "privacy", "thumbnail", "captions"}
_TOP_KEYS = {"full_video_url", "link_label", "privacy", "clips", "name"}
_URL = re.compile(r"^https://[^\s/$.?#][^\s]*$")


def _bad(message: str, **details: Any) -> PublishError:
    return PublishError("INVALID_ARGUMENT", f"series manifest: {message}", details)


def read_manifest(path: str) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        raise PublishError("INPUT_NOT_FOUND", f"series manifest is not a regular file: {path}",
                           {"path": path, "input": "series manifest"})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except UnicodeDecodeError as e:
        raise _bad(f"not UTF-8: {path}", path=path, position=e.start)
    except ValueError as e:
        raise _bad(f"not JSON: {path} ({e})", path=path)
    if not isinstance(doc, dict) or set(doc) - _TOP_KEYS:
        raise _bad(f"an object with the keys {', '.join(sorted(_TOP_KEYS))} only", path=path,
                   unknown=sorted(set(doc) - _TOP_KEYS) if isinstance(doc, dict) else None)
    clips = doc.get("clips")
    if not isinstance(clips, list) or not clips:
        raise _bad("`clips` must be a non-empty list", path=path)
    url = doc.get("full_video_url")
    if url is not None and (not isinstance(url, str) or not _URL.match(url)):
        raise _bad("`full_video_url` must be an https URL without spaces", path=path)
    label = doc.get("link_label", DEFAULT_LINK_LABEL)
    if not isinstance(label, str) or not label.strip() or "\n" in label:
        raise _bad("`link_label` must be one line of text", path=path)
    for i, c in enumerate(clips, 1):
        if not isinstance(c, dict) or not isinstance(c.get("file"), str) or set(c) - _CLIP_KEYS:
            raise _bad(f"clip {i} must be an object with `file` and only {', '.join(sorted(_CLIP_KEYS))}", clip=i)
        for k in ("title", "description", "privacy", "thumbnail"):
            if c.get(k) is not None and not isinstance(c[k], str):
                raise _bad(f"clip {i}: `{k}` must be a string", clip=i)
        if c.get("tags") is not None and not (isinstance(c["tags"], list) and all(isinstance(t, str) for t in c["tags"])):
            raise _bad(f"clip {i}: `tags` must be a list of strings", clip=i)
        caps = c.get("captions") or []
        if not isinstance(caps, list) or not all(isinstance(x, dict) and isinstance(x.get("file"), str)
                                                 and isinstance(x.get("language"), str) and set(x) == {"file", "language"}
                                                 for x in caps):
            raise _bad(f"clip {i}: `captions` must be a list of {{\"file\", \"language\"}}", clip=i)
    if doc.get("privacy") is not None and not isinstance(doc["privacy"], str):
        raise _bad("`privacy` must be a string", path=path)
    return doc


def _with_link(platform_id: str, title: Optional[str], description: Optional[str], url: Optional[str],
               label: str) -> Dict[str, Optional[str]]:
    if not url:
        return {"title": title, "description": description}
    line = f"{label}: {url}"
    if "description" in get_platform(platform_id).metadata_fields:
        return {"title": title, "description": f"{description.rstrip()}\n\n{line}" if description else line}
    return {"title": f"{title.rstrip()}\n\n{line}" if title else line, "description": description}


def clip_metadata(platform_id: str, manifest: Dict[str, Any], base_dir: str) -> List[Dict[str, Any]]:
    """[{"index", "path", "metadata": Metadata}] for every clip, in order, the link back included."""
    out = []
    for i, c in enumerate(manifest["clips"], 1):
        def rel(p: str) -> str:
            return p if os.path.isabs(p) else os.path.join(base_dir, p)
        text = _with_link(platform_id, c.get("title"), c.get("description"), manifest.get("full_video_url"),
                          manifest.get("link_label", DEFAULT_LINK_LABEL))
        caps = c.get("captions") or []
        try:
            md = build(title=text["title"], description=text["description"], tags=c.get("tags"),
                       privacy=c.get("privacy", manifest.get("privacy")),
                       thumbnail=rel(c["thumbnail"]) if c.get("thumbnail") else None,
                       captions=[rel(x["file"]) for x in caps], caption_languages=[x["language"] for x in caps])
        except PublishError as e:
            raise PublishError(e.code, f"clip {i}: {e.message}", dict(e.details, clip=i)) from None
        out.append({"index": i, "path": rel(c["file"]), "metadata": md})
    return out


def default_state_path(manifest_path: str) -> str:
    return os.path.abspath(manifest_path) + STATE_SUFFIX


def _load_state(path: str, platform_id: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"schema": STATE_SCHEMA, "platform": platform_id, "clips": {}}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as e:
        raise PublishError("INVALID_ARGUMENT", f"series state is unreadable: {path} ({e}); nothing was uploaded. "
                           "Do not delete it unless every clip it lists has been checked on the platform", {"state": path})
    if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA or not isinstance(doc.get("clips"), dict):
        raise PublishError("INVALID_ARGUMENT", f"series state is not a {STATE_SCHEMA} document: {path}; nothing was "
                           "uploaded", {"state": path})
    if doc.get("platform") != platform_id:
        raise PublishError("INVALID_ARGUMENT", f"series state {path} is for platform {doc.get('platform')!r}, not "
                           f"{platform_id!r}; give another --state for another platform. Nothing was uploaded",
                           {"state": path, "state_platform": doc.get("platform"), "platform": platform_id})
    return doc


def _save_state(path: str, state: Dict[str, Any]) -> None:
    state = creds.redact(state)                                   # nothing secret is in it; a second line of defence
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _uncertain(err: PublishError) -> bool:
    """True when the failed upload may have published something: anything but an explicit uploaded=false, and an
    UPLOAD_INTERRUPTED that the uploader marked not retryable (a publish step without an answer, ADR-007..010)."""
    return err.details.get("uploaded") is not False or (err.code == "UPLOAD_INTERRUPTED" and not err.retryable)


def _parse_resolve(values: Sequence[str]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for v in values:
        m = re.match(r"^([0-9]+)=(uploaded|not-uploaded)$", v or "")
        if not m:
            raise PublishError("INVALID_ARGUMENT", f"--resolve takes N=uploaded or N=not-uploaded, not {v!r}")
        out[int(m.group(1))] = m.group(2)
    return out


def series(manifest_path: str, platform_id: str, confirm: Optional[Sequence[str]] = None,
           state_path: Optional[str] = None, resolve: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    uploader = get_platform(platform_id)                                 # UNKNOWN_PLATFORM before any file is read
    manifest = read_manifest(manifest_path)
    base_dir = os.path.dirname(os.path.abspath(manifest_path))
    clips = clip_metadata(platform_id, manifest, base_dir)
    state_path = os.path.abspath(state_path) if state_path else default_state_path(manifest_path)
    state = _load_state(state_path, platform_id)
    entries: Dict[str, Any] = state["clips"]

    # every clip through the gate as a dry run: refusals (missing file, invalid metadata, unsendable format) and the
    # tokens, before anything is sent
    dry: List[Dict[str, Any]] = []
    for c in clips:
        try:
            d = upload(c["path"], platform_id, None, c["metadata"])
        except PublishError as e:
            raise PublishError(e.code, f"clip {c['index']}: {e.message}", dict(e.details, clip=c["index"],
                                                                              uploaded=False)) from None
        dry.append(d)

    resolutions = _parse_resolve(resolve or [])
    for n, outcome in sorted(resolutions.items()):
        e = entries.get(str(n))
        if e is None or e.get("status") not in ("uncertain", "sending"):
            raise PublishError("INVALID_ARGUMENT", f"--resolve {n}: clip {n} is not uncertain in {state_path}; nothing "
                               "was uploaded", {"clip": n, "state": state_path})
        if outcome == "uploaded":
            entries[str(n)] = dict(e, status="uploaded", resolved_by="human", resolved_at=_now())
        else:
            del entries[str(n)]
    if resolutions:
        _save_state(state_path, state)

    # an uploaded clip whose file or metadata changed since is never sent again: the state says it is on the platform
    for c, d in zip(clips, dry):
        e = entries.get(str(c["index"]))
        if e and e.get("status") == "uploaded" and e.get("confirm_token") not in (None, d["confirm_token"]):
            raise PublishError("INVALID_ARGUMENT", f"clip {c['index']} was uploaded ({e.get('url') or e.get('video_id')}) "
                               "with another file or metadata than the manifest gives now; it is not uploaded again. "
                               "Restore the manifest entry, or give another --state for a new series. Nothing was uploaded",
                               {"clip": c["index"], "state": state_path, "uploaded": False})

    report: List[Dict[str, Any]] = []
    for c, d in zip(clips, dry):
        e = entries.get(str(c["index"]))
        report.append({"index": c["index"], "input": d["input"], "metadata": d["metadata"],
                       "confirm_token": d["confirm_token"],
                       "state": (e or {}).get("status", "pending"),
                       "result": {k: e.get(k) for k in ("video_id", "url", "privacy")} if e else None})
    base = {"schema": SERIES_SCHEMA, "skill": {"id": SKILL_ID, "version": __version__}, "platform": uploader.spec(),
            "manifest": os.path.abspath(manifest_path), "state": state_path,
            "full_video_url": manifest.get("full_video_url"), "clips": report}
    blocked = next((r for r in report if r["state"] in ("uncertain", "sending")), None)

    if confirm is None:
        nxt = (f"clip {blocked['index']} is uncertain: check {uploader.name}, then re-run with --resolve "
               f"{blocked['index']}=uploaded or --resolve {blocked['index']}=not-uploaded" if blocked else
               "after QC passes and a human approves each clip, re-run with --confirm <its token> for every approved "
               "clip (repeat --confirm); clips are uploaded in order and the run stops at the first without a token")
        return dict(base, ok=True, dry_run=True, uploaded=False, network_used=False,
                    credentials=creds.status(platform_id), next_step=nxt)

    given = [str(t) for t in confirm]
    known = {r["confirm_token"] for r in report}
    unknown = [t for t in given if t not in known]
    if unknown:
        raise PublishError("CONFIRM_MISMATCH", f"{len(unknown)} --confirm token(s) match no clip of this manifest, "
                           "platform and metadata; run without --confirm to see the current tokens. Nothing was uploaded",
                           {"platform": platform_id, "uploaded": False})
    if blocked:
        raise PublishError("UPLOAD_PARTIAL", f"clip {blocked['index']}'s earlier upload has an unknown outcome; nothing "
                           f"was sent. Check {uploader.name}, then re-run with --resolve {blocked['index']}=uploaded or "
                           f"--resolve {blocked['index']}=not-uploaded",
                           {"platform": platform_id, "clip": blocked["index"], "state": state_path,
                            "series": _progress(report), "uploaded": None})

    uploaded_now: List[int] = []
    for c, r in zip(clips, report):
        if r["state"] == "uploaded":
            r["outcome"] = "skipped (already uploaded)"
            continue
        if r["confirm_token"] not in given:
            r["outcome"] = "awaiting confirmation"
            break
        key = str(c["index"])
        entries[key] = {"status": "sending", "sha256": r["input"]["sha256"], "confirm_token": r["confirm_token"],
                        "started_at": _now()}
        _save_state(state_path, state)
        try:
            doc = upload(c["path"], platform_id, r["confirm_token"], c["metadata"])
        except PublishError as e:
            if _uncertain(e):
                entries[key] = dict(entries[key], status="uncertain", error={"code": e.code, "message": e.message},
                                    **({"video_id": e.details["video_id"], "url": e.details.get("url")}
                                       if e.details.get("video_id") else {}))
                if e.code == "UPLOAD_PARTIAL" and e.details.get("uploaded") is True:
                    entries[key]["status"] = "uploaded"            # the video exists; only its extras failed
                r["state"] = entries[key]["status"]
            else:
                del entries[key]
            _save_state(state_path, state)
            r["outcome"] = "failed"
            raise PublishError(e.code, f"series stopped at clip {c['index']} of {len(clips)}: {e.message}",
                               dict(e.details, clip=c["index"], state=state_path, uploaded_now=uploaded_now,
                                    series=_progress(report)), e.retryable) from None
        res = doc["result"]
        entries[key] = {"status": "uploaded", "sha256": r["input"]["sha256"], "confirm_token": r["confirm_token"],
                        "video_id": res.get("video_id"), "url": res.get("url"), "privacy": res.get("privacy"),
                        "uploaded_at": _now()}
        _save_state(state_path, state)
        r.update(state="uploaded", outcome="uploaded", result={k: res.get(k) for k in ("video_id", "url", "privacy")},
                 receipt=res)
        uploaded_now.append(c["index"])
    for r in report:
        r.setdefault("outcome", "not reached" if r["state"] != "uploaded" else "skipped (already uploaded)")
    complete = all(r["state"] == "uploaded" for r in report)
    nxt = ("every clip is uploaded" if complete else
           f"clip {next(r['index'] for r in report if r['state'] != 'uploaded')} is next: after a human approves it, "
           "re-run with its --confirm token")
    return creds.redact(dict(base, ok=True, dry_run=False, uploaded=bool(uploaded_now), network_used=bool(uploaded_now),
                             complete=complete, uploaded_now=uploaded_now, series=_progress(report), next_step=nxt))


def _progress(report: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"total": len(report), "uploaded": [r["index"] for r in report if r["state"] == "uploaded"],
            "uncertain": [r["index"] for r in report if r["state"] in ("uncertain", "sending")]}


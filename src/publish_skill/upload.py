"""`publish upload`: the confirmation gate in front of every uploader (ADR-001, ADR-002).

Without --confirm it is a dry run: it reads the deliverable (size, sha256), reports what would be uploaded where, with
which metadata, and the confirmation token, and opens no connection. The token is derived from the platform, the
deliverable's bytes and the metadata (title, description, tags, privacy, and the thumbnail's and captions' bytes), so a
confirmation given after one dry run cannot upload a different file, the same file to another platform, or the same
file with edited metadata. Only the exact token goes through; then the credentials are read, from the environment or
the OS keychain (ADR-003), and the platform's uploader runs with exactly the request the token bound (YouTube: the
resumable upload in platforms/youtube.py, ADR-004; Vimeo: the tus upload in platforms/vimeo.py, ADR-005; TikTok:
the Direct Post chunked upload in platforms/tiktok.py, ADR-006; X: the chunked media upload and a post in
platforms/x.py, ADR-007; LinkedIn: the Videos API multipart upload and a post in platforms/linkedin.py, ADR-008).

The receipt of an upload, and the error of a failed one, pass through credentials.redact here, before they reach the
CLI (which redacts again): no credential, access token, session URI or upload link is in either.

Metadata that breaks one of the platform's upload limits (limits.py) is refused as METADATA_INVALID before a token is
printed or checked: `publish plan` shows every row and its fix.

No environment variable, config file or default turns confirmation on: the token must be on the command line."""
from __future__ import annotations

import hashlib
import hmac
from typing import Any, Dict, Optional

from . import SKILL_ID, __version__
from . import credentials as creds
from .errors import PublishError
from .metadata import DEFAULT_METADATA, Metadata, file_facts, sha256_file
from .plan import file_checks, metadata_checks
from .platforms import UploadRequest, get_platform

TOKEN_PREFIX = "confirm-"
TOKEN_BINDS = ["platform", "deliverable sha256", "deliverable size", "title", "description", "tags", "privacy",
               "thumbnail sha256 and size", "captions sha256, size and language, in order"]
__all__ = ["TOKEN_PREFIX", "TOKEN_BINDS", "confirm_token", "sha256_file", "upload"]


def confirm_token(platform_id: str, sha256: str, size: int, metadata: Optional[Metadata] = None) -> str:
    """The token that confirms uploading exactly these bytes, with exactly this metadata, to exactly this platform.
    `metadata` None means the defaults (no title, description, tags, thumbnail or captions; privacy private), the
    metadata of a dry run without metadata flags."""
    md = (metadata or DEFAULT_METADATA).canonical()
    digest = hashlib.sha256(f"{SKILL_ID}/upload\n{platform_id}\n{sha256}\n{size}\n{md}".encode("utf-8")).hexdigest()
    return TOKEN_PREFIX + digest[:16]


def _refuse_invalid_metadata(platform_id: str, metadata: Metadata) -> None:
    failed = [r for r in metadata_checks(platform_id, metadata) if r["status"] == "FAIL"]
    if failed:
        raise PublishError("METADATA_INVALID", "the metadata breaks the platform's upload limits: "
                           + ", ".join(r["check"] for r in failed) + "; run `publish plan` for each fix. Nothing was uploaded",
                           {"platform": platform_id, "failed": failed, "uploaded": False})


def _refuse_unsendable_format(platform_id: str, item: Dict[str, Any]) -> None:
    """A file the platform's upload cannot name a type for (TikTok: MP4, MOV or WebM only, ADR-006) is refused before a
    token is printed, like invalid metadata. Where the format list is advisory (YouTube) plan only warns."""
    failed = [r for r in file_checks(platform_id, item["path"], item["size"])
              if r["status"] == "FAIL" and r.get("limit_key") == "video.file_formats"]
    if failed:
        raise PublishError("INVALID_ARGUMENT", f"{failed[0]['value']} files cannot be uploaded to this platform (it takes "
                           f"{', '.join(failed[0]['limit'])}); {failed[0]['fix']}. Nothing was uploaded",
                           {"platform": platform_id, "failed": failed, "uploaded": False})


def upload(path: str, platform_id: str, confirm: Optional[str] = None, metadata: Optional[Metadata] = None) -> Dict[str, Any]:
    uploader = get_platform(platform_id)                     # UNKNOWN_PLATFORM before the file is read
    item = file_facts(path, "deliverable")
    md = metadata or DEFAULT_METADATA
    _refuse_invalid_metadata(platform_id, md)                # METADATA_INVALID before any token is printed or checked
    _refuse_unsendable_format(platform_id, item)             # INVALID_ARGUMENT, likewise
    token = confirm_token(platform_id, item["sha256"], item["size"], md)
    base = {"skill": {"id": SKILL_ID, "version": __version__}, "platform": uploader.spec(), "input": item,
            "metadata": md.to_dict()}
    if confirm is None:
        return dict(base, ok=True, dry_run=True, uploaded=False, network_used=False, confirm_token=token,
                    credentials=creds.status(platform_id),
                    next_step=f"after QC passes and a human approves this upload, re-run with the same metadata flags and "
                              f"--confirm {token}")
    if not hmac.compare_digest(str(confirm), token):
        raise PublishError("CONFIRM_MISMATCH", "the --confirm token does not match this deliverable, metadata and platform; "
                           "run without --confirm to see the current token. Nothing was uploaded",
                           {"platform": platform_id, "path": item["path"], "uploaded": False})
    empty = [what for what, f in [("deliverable", item)] + [("thumbnail", md.thumbnail)] + [("captions", c) for c in md.captions]
             if f is not None and f["size"] == 0]
    if empty:
        raise PublishError("INVALID_ARGUMENT", f"empty file: {', '.join(empty)}; nothing was uploaded",
                           {"platform": platform_id, "empty": empty, "uploaded": False})
    secrets = creds.load(platform_id)                        # the first credential read; CREDENTIALS_MISSING names variables
    request = UploadRequest(platform=platform_id, path=item["path"], size=item["size"], sha256=item["sha256"],
                            metadata=md.to_dict(), confirm_token=token)
    try:
        result = uploader.upload(request, secrets)           # the only network use in the package (ADR-004..008)
    except PublishError as e:
        raise PublishError(e.code, creds.redact(e.message), creds.redact(e.details), e.retryable) from None
    return creds.redact(dict(base, ok=True, dry_run=False, uploaded=True, network_used=True, confirm_token=token,
                             result=result))

"""`publish upload`: the confirmation gate in front of every uploader (ADR-001, ADR-002).

Without --confirm it is a dry run: it reads the deliverable (size, sha256), reports what would be uploaded where, with
which metadata, and the confirmation token, and opens no connection. The token is derived from the platform, the
deliverable's bytes and the metadata (title, description, tags, privacy, and the thumbnail's and captions' bytes), so a
confirmation given after one dry run cannot upload a different file, the same file to another platform, or the same
file with edited metadata. Only the exact token goes through; then the credentials are read from the environment and
the platform's uploader runs. Every uploader in this release is a stub that raises NOT_IMPLEMENTED, so nothing is ever
uploaded yet.

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
from .plan import metadata_checks
from .platforms import UploadRequest, get_platform

TOKEN_PREFIX = "confirm-"
TOKEN_BINDS = ["platform", "deliverable sha256", "deliverable size", "title", "description", "tags", "privacy",
               "thumbnail sha256 and size", "captions sha256 and size, in order"]
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


def upload(path: str, platform_id: str, confirm: Optional[str] = None, metadata: Optional[Metadata] = None) -> Dict[str, Any]:
    uploader = get_platform(platform_id)                     # UNKNOWN_PLATFORM before the file is read
    item = file_facts(path, "deliverable")
    md = metadata or DEFAULT_METADATA
    _refuse_invalid_metadata(platform_id, md)                # METADATA_INVALID before any token is printed or checked
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
    secrets = creds.load(platform_id)                        # CREDENTIALS_MISSING names the variables, never values
    request = UploadRequest(platform=platform_id, path=item["path"], size=item["size"], sha256=item["sha256"],
                            metadata=md.to_dict())
    result = uploader.upload(request, secrets)               # NOT_IMPLEMENTED for every platform in this release
    return dict(base, ok=True, dry_run=False, uploaded=True, network_used=True, result=result)  # pragma: no cover

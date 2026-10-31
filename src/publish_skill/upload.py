"""`publish upload`: the confirmation gate in front of every uploader (ADR-001).

Without --confirm it is a dry run: it reads the deliverable (size, sha256), reports what would be uploaded where and
the confirmation token, and opens no connection. The token is derived from the platform and the deliverable's bytes,
so a confirmation given after one dry run cannot upload a different file, or the same file to another platform. Only
the exact token goes through; then the credentials are read from the environment and the platform's uploader runs.
Every uploader in this release is a stub that raises NOT_IMPLEMENTED, so nothing is ever uploaded yet.

No environment variable, config file or default turns confirmation on: the token must be on the command line."""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any, Dict, Optional

from . import SKILL_ID, __version__
from . import credentials as creds
from .errors import PublishError
from .platforms import UploadRequest, get_platform

TOKEN_PREFIX = "confirm-"
_CHUNK = 1 << 20


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def confirm_token(platform_id: str, sha256: str, size: int) -> str:
    """The token that confirms uploading exactly these bytes to exactly this platform. Step 2 adds the metadata
    (title, description, privacy, ...) to the hashed fields, so editing it after the dry run also needs a new token."""
    digest = hashlib.sha256(f"{SKILL_ID}/upload\n{platform_id}\n{sha256}\n{size}".encode("utf-8")).hexdigest()
    return TOKEN_PREFIX + digest[:16]


def _deliverable(path: str) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        raise PublishError("INPUT_NOT_FOUND", f"not a regular file: {path}", {"path": path})
    return {"path": os.path.abspath(path), "size": os.path.getsize(path), "sha256": sha256_file(path)}


def upload(path: str, platform_id: str, confirm: Optional[str] = None) -> Dict[str, Any]:
    uploader = get_platform(platform_id)                     # UNKNOWN_PLATFORM before the file is read
    item = _deliverable(path)
    token = confirm_token(platform_id, item["sha256"], item["size"])
    base = {"skill": {"id": SKILL_ID, "version": __version__}, "platform": uploader.spec(), "input": item}
    if confirm is None:
        return dict(base, ok=True, dry_run=True, uploaded=False, network_used=False, confirm_token=token,
                    credentials=creds.status(platform_id),
                    next_step=f"after QC passes and a human approves this upload, re-run with --confirm {token}")
    if not hmac.compare_digest(str(confirm), token):
        raise PublishError("CONFIRM_MISMATCH", "the --confirm token does not match this deliverable and platform; "
                           "run without --confirm to see the current token. Nothing was uploaded",
                           {"platform": platform_id, "path": item["path"], "uploaded": False})
    secrets = creds.load(platform_id)                        # CREDENTIALS_MISSING names the variables, never values
    request = UploadRequest(platform=platform_id, path=item["path"], size=item["size"], sha256=item["sha256"])
    result = uploader.upload(request, secrets)               # NOT_IMPLEMENTED for every platform in this release
    return dict(base, ok=True, dry_run=False, uploaded=True, network_used=True, result=result)  # pragma: no cover

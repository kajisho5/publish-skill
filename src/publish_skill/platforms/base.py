"""Uploader base class. An uploader is registered in platforms/__init__.py; only registered ids can be named on the
command line. The base `upload` is a stub that raises NOT_IMPLEMENTED and opens no connection (ADR-001); a real
uploader replaces it in the roadmap step named by `roadmap_step` (YouTube: step 4, ADR-004; Vimeo: step 5, ADR-005;
TikTok: step 5, ADR-006; X: step 5, ADR-007; LinkedIn: step 5, ADR-008; Facebook: step 5, ADR-009;
Instagram Reels: step 5, ADR-010).
`upload` is called only by upload.py's gate, with the request the confirmation token bound and the credentials
credentials.load returned."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Tuple

from ..errors import PublishError


@dataclass(frozen=True)
class UploadRequest:
    platform: str
    path: str
    size: int
    sha256: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    confirm_token: str = ""


class Uploader:
    id = ""
    name = ""
    roadmap_step = 0
    implemented = False
    # this platform's credential variables: read from the environment, or from the OS keychain under the same names
    # (ADR-001, ADR-003)
    credential_env: Tuple[str, ...] = ()
    docs_url = ""
    # the `check --platform` name of ffmpeg-skill's delivery spec for this platform (ffmpeg-skill owns that spec)
    ffmpeg_skill_platform = ""
    # the metadata fields this uploader sends; `plan` FAILs (and `upload` refuses, METADATA_INVALID) any other field
    # that is set, so a confirmed upload never silently drops part of what the token bound (ADR-005)
    metadata_fields: Tuple[str, ...] = ("title", "description", "tags", "privacy", "thumbnail", "captions")

    def spec(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "implemented": self.implemented, "roadmap_step": self.roadmap_step,
                "credential_env": list(self.credential_env), "docs_url": self.docs_url,
                "ffmpeg_skill_platform": self.ffmpeg_skill_platform, "metadata_fields": list(self.metadata_fields)}

    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        raise PublishError("NOT_IMPLEMENTED", f"the {self.name} uploader is not implemented yet (roadmap step {self.roadmap_step}); "
                           "nothing was uploaded", {"platform": self.id, "roadmap_step": self.roadmap_step, "uploaded": False})

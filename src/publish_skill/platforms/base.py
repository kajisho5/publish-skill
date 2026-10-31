"""Uploader base class. An uploader is registered in platforms/__init__.py; only registered ids can be named on the
command line. In this release every uploader is a stub: `upload` raises NOT_IMPLEMENTED and opens no connection
(ADR-001). A real uploader replaces `upload` in the roadmap step named by `roadmap_step`."""
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


class Uploader:
    id = ""
    name = ""
    roadmap_step = 0
    implemented = False
    # environment variables this platform's credentials are read from (ADR-001: environment only until step 3)
    credential_env: Tuple[str, ...] = ()
    docs_url = ""
    # the `check --platform` name of ffmpeg-skill's delivery spec for this platform (ffmpeg-skill owns that spec)
    ffmpeg_skill_platform = ""

    def spec(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "implemented": self.implemented, "roadmap_step": self.roadmap_step,
                "credential_env": list(self.credential_env), "docs_url": self.docs_url,
                "ffmpeg_skill_platform": self.ffmpeg_skill_platform}

    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        raise PublishError("NOT_IMPLEMENTED", f"the {self.name} uploader is not implemented yet (roadmap step {self.roadmap_step}); "
                           "nothing was uploaded", {"platform": self.id, "roadmap_step": self.roadmap_step, "uploaded": False})

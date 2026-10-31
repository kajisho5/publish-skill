"""Platform registry. Only the ids listed here can be named with --platform; each further platform is one PR
(roadmap step 5)."""
from __future__ import annotations

from typing import Dict, List, Type

from ..errors import PublishError
from .base import Uploader, UploadRequest
from .facebook import FacebookUploader
from .linkedin import LinkedInUploader
from .reels import ReelsUploader
from .tiktok import TikTokUploader
from .vimeo import VimeoUploader
from .x import XUploader
from .youtube import YouTubeUploader

_REGISTRY: Dict[str, Type[Uploader]] = {YouTubeUploader.id: YouTubeUploader, VimeoUploader.id: VimeoUploader,
                                        TikTokUploader.id: TikTokUploader, XUploader.id: XUploader,
                                        LinkedInUploader.id: LinkedInUploader, FacebookUploader.id: FacebookUploader,
                                        ReelsUploader.id: ReelsUploader}

__all__ = ["Uploader", "UploadRequest", "get_platform", "platform_ids"]


def platform_ids() -> List[str]:
    return sorted(_REGISTRY)


def get_platform(platform_id: str) -> Uploader:
    cls = _REGISTRY.get(platform_id)
    if cls is None:
        raise PublishError("UNKNOWN_PLATFORM", f"unknown platform {platform_id!r}", {"known": platform_ids()})
    return cls()

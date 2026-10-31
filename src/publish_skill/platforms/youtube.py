"""YouTube uploader: a stub. Roadmap step 4 implements it (Data API v3 resumable upload of the video, thumbnail and
captions, tested against a mock server). Until then `upload` raises NOT_IMPLEMENTED and nothing leaves the machine."""
from __future__ import annotations

from .base import Uploader


class YouTubeUploader(Uploader):
    id = "youtube"
    name = "YouTube"
    roadmap_step = 4
    implemented = False
    # OAuth 2.0 installed-app credentials. Step 3 moves them to the OS keychain (`publish auth login`).
    credential_env = ("PUBLISH_YOUTUBE_CLIENT_ID", "PUBLISH_YOUTUBE_CLIENT_SECRET", "PUBLISH_YOUTUBE_REFRESH_TOKEN")
    docs_url = "https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol"

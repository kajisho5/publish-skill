"""Upload-side limits per platform (ADR-002). This skill owns them; the platform delivery specs (codecs, frame size,
aspect, frame rate, loudness) belong to ffmpeg-skill, and `publish plan` reads those through `check` (adapter.py).

Every value below was read from the platform's own documentation. Each entry carries the page it came from
(`source`), the day it was read (`accessed`) and the words it rests on (`quote`). Nothing here is inferred: a limit
the documentation does not state is recorded with `value: None` and `status: "unknown"`, and `publish plan` checks
nothing against it. A limit owned by ffmpeg-skill is recorded with `status: "delegated"` and no value, so it is
never restated here.

Where a page writes "GB" or "MB" without saying whether it means 10^9 or 2^30, the smaller (decimal) reading is
used, so a file `plan` passes is never over the limit under either reading; `documented_as` keeps the page's words.

To change a value: re-read the page, update `value`, `quote` and `accessed` together, and the README table."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional

ACCESSED = "2026-10-31"
OWNERS = ("publish", "ffmpeg-skill")

_YT_INSERT = "https://developers.google.com/youtube/v3/docs/videos/insert"
_YT_VIDEOS = "https://developers.google.com/youtube/v3/docs/videos"
_YT_THUMBS = "https://developers.google.com/youtube/v3/docs/thumbnails/set"
_YT_CAPTIONS = "https://developers.google.com/youtube/v3/docs/captions/insert"
_YT_CAPTION_RESOURCE = "https://developers.google.com/youtube/v3/docs/captions"
_YT_HELP_LENGTH = "https://support.google.com/youtube/answer/71673?hl=en"
_YT_HELP_FORMATS = "https://support.google.com/youtube/troubleshooter/2888402?hl=en"
_YT_HELP_CAPTIONS = "https://support.google.com/youtube/answer/2734698?hl=en"
_YT_HELP_THUMBS = "https://support.google.com/youtube/answer/72431?hl=en"
_FFMPEG_CHECK = "ffmpeg-skill `check --platform youtube` (scripts/_platforms.py owns the value)"
# Vimeo (roadmap step 5, ADR-005): the developer site's guides and the API reference (API 3.4, reference revision
# 3.4.9), read on the same day.
_VM_UPLOAD = "https://developer.vimeo.com/api/upload/videos"
_VM_UPLOAD_START = "https://developer.vimeo.com/api/guides/videos/upload"
_VM_REF_UPLOAD = "https://developer.vimeo.com/api/reference/videos#upload_video"
_VM_REF_EDIT = "https://developer.vimeo.com/api/reference/videos#edit_video"
_VM_RATE = "https://developer.vimeo.com/guidelines/rate-limiting"
_VM_NO_SPEC = ("ffmpeg-skill has no `vimeo` delivery spec (scripts/_platforms.py, 2026-10-31); ffmpeg-skill owns it, so "
               "it is not written here (plan reports the delivery spec as UNCHECKED)")
# TikTok (roadmap step 5, ADR-006): the Content Posting API pages on developers.tiktok.com, read on the same day.
_TT_DIRECT = "https://developers.tiktok.com/doc/content-posting-api-reference-direct-post"
_TT_TRANSFER = "https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide"
_TT_CREATOR = "https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info"
_TT_STATUS = "https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status"
_TT_START = "https://developers.tiktok.com/doc/content-posting-api-get-started"
_TT_OAUTH = "https://developers.tiktok.com/doc/oauth-user-access-token-management"
_TT_CHECK = "ffmpeg-skill `check --platform tiktok` (scripts/_platforms.py owns the value)"
# X (roadmap step 5, ADR-007): the X API v2 pages on docs.x.com and X's own twitter-text configuration, read on the
# same day.
_X_CHUNKED = "https://docs.x.com/x-api/media/quickstart/media-upload-chunked"
_X_INIT = "https://docs.x.com/x-api/media/initialize-media-upload"
_X_APPEND = "https://docs.x.com/x-api/media/append-media-upload"
_X_POST = "https://docs.x.com/x-api/posts/create-post"
_X_RATE = "https://docs.x.com/x-api/fundamentals/rate-limits"
_X_COUNT = "https://docs.x.com/fundamentals/counting-characters"
_X_TEXT_CONFIG = "https://github.com/twitter/twitter-text/blob/master/config/v3.json"
_X_OAUTH = "https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code"
_X_CHECK = "ffmpeg-skill `check --platform x` (scripts/_platforms.py owns the value)"
# twitter-text v3: code points in these ranges weigh 1 (100 of scale 100), every other code point 2; a URL counts 23
X_WEIGHT_ONE_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
X_URL_LENGTH = 23
# LinkedIn (roadmap step 5, ADR-008): the Community Management pages on learn.microsoft.com (API version 202610), read
# on the same day.
_LI_VIDEOS = "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api"
_LI_POSTS = "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api"
_LI_SCHEMA = "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/post-api-schema"
_LI_LITTLE = "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format"
_LI_UGC = "https://learn.microsoft.com/en-us/linkedin/compliance/integrations/shares/ugc-post-api"
_LI_OAUTH = "https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow"
_LI_CHECK = "ffmpeg-skill `check --platform linkedin` (scripts/_platforms.py owns the value)"
# Facebook (roadmap step 5, ADR-009): the Video API, Resumable Upload API, Page videos reference and error-handling
# pages on developers.facebook.com (Graph API v26.0), read on the same day.
_FB_PUBLISH = "https://developers.facebook.com/docs/video-api/guides/publishing"
_FB_UPLOAD = "https://developers.facebook.com/docs/graph-api/guides/upload"
_FB_PAGE_VIDEOS = "https://developers.facebook.com/docs/graph-api/reference/page/videos/"
_FB_OVERVIEW = "https://developers.facebook.com/docs/video-api/overview"
_FB_ERRORS = "https://developers.facebook.com/docs/graph-api/guides/error-handling"
_FB_CHECK = "ffmpeg-skill `check --platform facebook` (scripts/_platforms.py owns the value)"


def _known(value: Any, unit: str, source: str, quote: str, documented_as: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    out = {"status": "documented", "value": value, "unit": unit, "source": source, "accessed": ACCESSED, "quote": quote}
    if documented_as is not None:
        out["documented_as"] = documented_as
    out.update(extra)
    return out


def _unknown(note: str, searched: List[str]) -> Dict[str, Any]:
    return {"status": "unknown", "value": None, "note": note, "searched": searched, "accessed": ACCESSED}


def _delegated(what: str, read_from: str = _FFMPEG_CHECK) -> Dict[str, Any]:
    return {"status": "delegated", "value": None, "owner": "ffmpeg-skill", "read_from": read_from, "what": what}


LIMITS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "youtube": {
        # the video file
        "video.max_bytes": _known(256 * 1000 ** 3, "bytes", _YT_INSERT, "Maximum file size: 256GB", documented_as="256GB"),
        "video.max_duration_s": _known(12 * 3600, "seconds", _YT_HELP_LENGTH,
                                       "The maximum file size you can upload is 256 GB or 12 hours, whichever is less."),
        "video.unverified_max_duration_s": _known(15 * 60, "seconds", _YT_HELP_LENGTH,
                                                  "By default, you can upload videos that are up to 15 minutes long. Verified "
                                                  "accounts can upload videos longer than 15 minutes.",
                                                  applies_to="accounts that are not verified (plan cannot tell, so it warns)"),
        "video.mime_types": _known(["video/*", "application/octet-stream"], "MIME types", _YT_INSERT,
                                   "Accepted Media MIME types: video/* , application/octet-stream"),
        # The Help Center lists formats by name, some of them codecs. `value` is the extensions those names are written
        # with (`mapping` says how); it is not a hard rule on the extension (the bytes decide), so an unlisted extension
        # is a WARN, never a FAIL.
        "video.file_formats": _known([".mov", ".mpeg", ".mp4", ".mpg", ".avi", ".wmv", ".mpegps", ".flv", ".3gp", ".3gpp",
                                      ".webm"], "file extensions", _YT_HELP_FORMATS,
                                     "make sure that you're using one of the following formats: .MOV .MPEG-1 .MPEG-2 .MPEG4 "
                                     ".MP4 .MPG .AVI .WMV .MPEGPS .FLV 3GPP WebM DNxHR ProRes CineForm HEVC (H.265)",
                                     listed=[".MOV", ".MPEG-1", ".MPEG-2", ".MPEG4", ".MP4", ".MPG", ".AVI", ".WMV", ".MPEGPS",
                                             ".FLV", "3GPP", "WebM", "DNxHR", "ProRes", "CineForm", "HEVC (H.265)"],
                                     mapping=".MPEG-1/.MPEG-2/.MPEG4 -> .mpeg; 3GPP -> .3gp/.3gpp; WebM -> .webm; DNxHR, "
                                             "ProRes, CineForm and HEVC are codecs (their container is one of the others); "
                                             "the rest as written"),
        "video.codecs": _delegated("video codec"),
        "video.resolution": _delegated("frame size (minimum height)"),
        "video.aspect": _delegated("aspect ratio"),
        "video.fps": _delegated("frame rate"),
        # metadata
        "title.max_chars": _known(100, "characters", _YT_VIDEOS,
                                  "The property value has a maximum length of 100 characters and may contain all valid UTF-8 "
                                  "characters except < and >."),
        "title.forbidden_chars": _known(["<", ">"], "characters", _YT_VIDEOS,
                                        "may contain all valid UTF-8 characters except < and >."),
        "description.max_bytes": _known(5000, "bytes (UTF-8)", _YT_VIDEOS,
                                        "The property value has a maximum length of 5000 bytes and may contain all valid "
                                        "UTF-8 characters except < and >."),
        "description.forbidden_chars": _known(["<", ">"], "characters", _YT_VIDEOS,
                                              "may contain all valid UTF-8 characters except < and >."),
        "tags.max_chars": _known(500, "characters", _YT_VIDEOS,
                                 "The property value has a maximum length of 500 characters. ... commas between items in the "
                                 "list count toward the limit. If a tag contains a space, the API server handles the tag "
                                 "value as though it were wrapped in quotation marks, and the quotation marks count toward "
                                 "the character limit."),
        "tags.max_chars_per_tag": _unknown("no per-tag limit is stated; only the 500-character total is checked",
                                           [_YT_VIDEOS, _YT_INSERT]),
        "tags.max_count": _unknown("no limit on the number of tags is stated; only the 500-character total is checked",
                                   [_YT_VIDEOS, _YT_INSERT]),
        "privacy.values": _known(["private", "public", "unlisted"], "enum", _YT_VIDEOS,
                                 "Valid values for this property are: private public unlisted",
                                 api_default="public (this skill defaults to private: see ADR-002)"),
        # the thumbnail
        "thumbnail.max_bytes": _known(50 * 1000 ** 2, "bytes", _YT_THUMBS, "Maximum file size: 50MB", documented_as="50MB"),
        "thumbnail.types": _known(["image/jpeg", "image/png"], "image types", _YT_THUMBS,
                                  "Accepted Media MIME types: image/jpeg , image/png , application/octet-stream"),
        "thumbnail.min_width_px": _known(640, "pixels", _YT_HELP_THUMBS,
                                         "We recommend your custom thumbnails: Have a resolution of 3840 x 2160 pixels for "
                                         "videos ..., with a minimum width of 640 pixels for videos",
                                         checked=False, why_not_checked="a recommendation, and plan does not decode images"),
        # captions
        "captions.max_bytes": _known(100 * 1000 ** 2, "bytes", _YT_CAPTIONS, "Maximum file size: 100MB",
                                     documented_as="100MB"),
        "captions.extensions": _known([".srt", ".sbv", ".sub", ".mpsub", ".lrc", ".cap", ".smi", ".sami", ".rt", ".vtt",
                                       ".ttml", ".dfxp", ".scc", ".stl", ".tds", ".cin", ".asc"], "file extensions",
                                      _YT_HELP_CAPTIONS,
                                      "Basic file formats ... SubRip .srt, SubViewer .sbv or .sub, MPsub .mpsub, LRC .lrc, "
                                      "Videotron Lambda .cap; Advanced ... SAMI .smi or .sami, RealText .rt, WebVTT .vtt, "
                                      "TTML .ttml, DFXP .ttml or .dfxp; Broadcast ... .scc, EBU-STL .stl, .tds, .cin, .asc, .cap"),
        "captions.plain_utf8_extensions": _known([".srt", ".sbv", ".sub"], "file extensions", _YT_HELP_CAPTIONS,
                                                 "Only basic versions of these files are supported. No style info (markup) is "
                                                 "recognized. The file must be in plain UTF-8."),
        "captions.language": _known("BCP-47 language tag", "format", _YT_CAPTION_RESOURCE,
                                    "snippet.language ... The property value is a BCP-47 language tag.",
                                    required_by=_YT_CAPTIONS + " (snippet.language is a property you must specify)",
                                    checked_as="present, and shaped like a BCP-47 tag (letters, digits and hyphens: "
                                               "en, en-US, zh-Hant, es-419); the platform validates the registry"),
        # API quota: what one upload costs. Reported, not checked (plan cannot see the project's remaining quota).
        "quota.videos_insert": _known("100 calls per day; 1 unit each in the Video Uploads quota bucket", "quota", _YT_INSERT,
                                      "Quota impact: 100 calls per day. A call to this method has a quota cost of 1 unit in "
                                      "the Video Uploads quota bucket.", checked=False),
        "quota.thumbnails_set_units": _known(50, "quota units (approximate)", _YT_THUMBS,
                                             "A call to this method has a quota cost of approximately 50 units.", checked=False),
        "quota.captions_insert_units": _known(400, "quota units", _YT_CAPTIONS,
                                              "A call to this method has a quota cost of 400 units.", checked=False),
        "uploads.per_user": _unknown("the API returns uploadLimitExceeded when \"the user has exceeded the number of videos "
                                     "they may upload\" but states no number", [_YT_INSERT]),
    },
    "tiktok": {
        # the video file. "4GB" is read as decimal, the smaller reading (ffmpeg-skill's delivery spec has its own value).
        "video.max_bytes": _known(4 * 1000 ** 3, "bytes", _TT_TRANSFER, "Size restrictions: Maximum of 4GB",
                                  documented_as="4GB"),
        "video.max_duration_s": _known(10 * 60, "seconds", _TT_TRANSFER,
                                       "The longest video a developer can send via the initialize Upload Video endpoint "
                                       "is 10 minutes."),
        "video.creator_max_duration_s": _unknown("per creator: \"All TikTok creators can post 3-minute videos, while some "
                                                 "have access to post 5-minute or 10-minute videos\"; creator_info answers "
                                                 "max_video_post_duration_sec, which plan cannot read offline (the upload "
                                                 "receipt reports it)", [_TT_TRANSFER, _TT_CREATOR]),
        # MP4, WebM and MOV only: the upload's Content-Type must be one of video/mp4, video/quicktime, video/webm, so
        # an unlisted extension is a FAIL here (enforced), not a WARN as on YouTube.
        "video.file_formats": _known([".mp4", ".webm", ".mov"], "file extensions", _TT_TRANSFER,
                                     "Supported media formats: MP4 (recommended), WebM, MOV", enforced=True,
                                     content_types={".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"},
                                     also=_TT_DIRECT + " (the PUT Content-Type: video/mp4, video/quicktime, video/webm)"),
        "video.codecs": _delegated("video codec", _TT_CHECK),
        "video.resolution": _delegated("frame size", _TT_CHECK),
        "video.aspect": _delegated("aspect ratio", _TT_CHECK),
        "video.fps": _delegated("frame rate", _TT_CHECK),
        "upload.chunk_bytes": _known("5-64 MB per chunk; the final chunk up to 128 MB; a file under 5 MB whole",
                                     "bytes per PUT", _TT_TRANSFER,
                                     "Each chunk must be at least 5 MB but no greater than 64 MB, except for the final "
                                     "chunk, which can be greater than chunk_size (up to 128 MB) to accommodate any "
                                     "trailing bytes. ... Videos with a total size less than 5 MB must be uploaded as a "
                                     "whole", checked=False,
                                     used="64 000 000 bytes (at most 64 MB under either reading); a file of at most "
                                          "that size is sent whole"),
        "upload.max_chunks": _known(1000, "chunks", _TT_TRANSFER, "Minimum of 1 chunk and maximum of 1,000 chunks.",
                                    checked=False),
        "upload.url_validity_s": _known(3600, "seconds", _TT_DIRECT, "The upload_url is valid for one hour after "
                                                                       "issuance.", checked=False),
        # metadata: TikTok's `title` is the post's caption; there is no separate description field.
        "title.max_utf16_units": _known(2200, "UTF-16 code units", _TT_DIRECT,
                                        "title: The video caption. ... The maximum length is 2200 in UTF-16 runes."),
        "title.forbidden_chars": _unknown("no forbidden characters are stated", [_TT_DIRECT]),
        "privacy.values": _known(["private", "public"], "enum (this skill's names, sent as privacy_level)", _TT_DIRECT,
                                 "privacy_level ... PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR, "
                                 "SELF_ONLY. The provided value must match one of the privacy_level_options returned in "
                                 "the /creator_info/query/ API.",
                                 maps_to={"private": "SELF_ONLY", "public": "PUBLIC_TO_EVERYONE"},
                                 api_values=["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR",
                                             "SELF_ONLY"],
                                 not_offered="unlisted (TikTok has no link-only level), MUTUAL_FOLLOW_FRIENDS and "
                                             "FOLLOWER_OF_CREATOR (no name for them in this skill's three)",
                                 api_default="none: privacy_level is required; this skill sends SELF_ONLY unless "
                                             "--privacy public"),
        # account and app limits
        "quota.daily_posts": _unknown("per creator: \"spam_risk_too_many_posts: the daily post cap is reached for the "
                                      "user\". No number is stated", [_TT_DIRECT, _TT_START]),
        "quota.active_users": _unknown("per app: \"reached_active_user_cap\", the client's daily quota of active "
                                       "publishing users. No number is stated", [_TT_DIRECT]),
        "rate_limit.requests_per_minute": _known({"video/init": 6, "creator_info/query": 20, "status/fetch": 30},
                                                 "requests per minute per user access token", _TT_DIRECT,
                                                 "Each user access_token is limited to 6 requests per minute.",
                                                 checked=False,
                                                 also={"creator_info/query": _TT_CREATOR + " (20 per minute)",
                                                       "status/fetch": _TT_STATUS + " (30 per minute)"}),
        "upload.audit": _known("an unaudited app's posts are private (SELF_ONLY viewing) until TikTok audits it",
                               "app requirement", _TT_START,
                               "All content posted by unaudited clients will be restricted to private viewing mode.",
                               checked=False),
        "token.scopes": _known(["video.publish"], "OAuth scopes", _TT_DIRECT,
                               "Scope: video.publish", checked=False,
                               note="not checked offline; TikTok answers 401 scope_not_authorized (AUTH_FAILED)"),
        "token.lifetime_s": _known(24 * 3600, "seconds (access token)", _TT_OAUTH,
                                   "It is valid for 24 hours after initial issuance.", checked=False,
                                   note="this skill reads an access token only; refreshing it is not implemented "
                                        "(ADR-006)"),
    },
    "x": {
        # the video file: tweet_video, an account without Premium (Premium allows more; plan cannot tell, so the
        # smaller values are checked). "8 GB" is read as decimal, the smaller reading.
        "video.max_bytes": _known(8 * 1000 ** 3, "bytes", _X_CHUNKED,
                                  "tweet_video / amplify_video | 20 min / 8 GB · Premium: 125 min / 16 GB",
                                  documented_as="8 GB"),
        "video.max_duration_s": _known(20 * 60, "seconds", _X_CHUNKED,
                                       "tweet_video / amplify_video | 20 min / 8 GB · Premium: 125 min / 16 GB",
                                       applies_to="accounts without Premium (Premium: 125 minutes)"),
        # initialize's media_type must name one of these, so the list is enforced (as on TikTok)
        "video.file_formats": _known([".mp4", ".mov", ".webm", ".ts"], "file extensions", _X_INIT,
                                     "media_type: video/mp4, video/webm, video/mp2t, video/quicktime", enforced=True,
                                     content_types={".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
                                                    ".ts": "video/mp2t"}),
        "video.codecs": _delegated("video codec", _X_CHECK),
        "video.resolution": _delegated("frame size", _X_CHECK),
        "video.aspect": _delegated("aspect ratio", _X_CHECK),
        "video.fps": _delegated("frame rate", _X_CHECK),
        "upload.chunk_bytes": _known("at or below 5 MB per segment (the server maximum is 8 MB)", "bytes per append",
                                     _X_CHUNKED, "keep each chunk at or below 5 MB", checked=False,
                                     used="4 MiB (4 194 304 bytes): below 5 MB under either reading"),
        "upload.max_segments": _known(10000, "segments", _X_APPEND, "segment_index: integer, 0 to 9999", checked=False),
        "upload.session_validity_s": _known(86400, "seconds", _X_CHUNKED,
                                            "expires_after_secs (86400 in the example)", checked=False),
        # metadata: the post's text is the bound title; X weighs characters (twitter-text), NFC-normalised
        "title.max_weighted": _known(280, "weighted characters (twitter-text v3)", _X_COUNT,
                                     "Posts can be up to 280 characters. Some characters count more than others. ... "
                                     "Emojis, CJK (Chinese, Japanese, Korean) characters, and other Unicode characters "
                                     "count as 2 by default. ... Any URL counts as 23 characters",
                                     weights=_X_TEXT_CONFIG + " (maxWeightedTweetLength 280; ranges 0-4351, 8192-8205, "
                                                              "8208-8223, 8242-8247 weigh 1; defaultWeight 2; "
                                                              "transformedURLLength 23)",
                                     note="exact for text with no URL and no emoji; otherwise plan computes an upper "
                                          "bound and WARNs (not FAILs) when only the bound is over"),
        "title.forbidden_chars": _unknown("no forbidden characters are stated", [_X_POST, _X_COUNT]),
        "privacy.values": _known(["public"], "enum (this skill's names)", _X_POST,
                                 "the create-post request has no field that makes a post private or unlisted",
                                 not_offered="private and unlisted: X posts are public (for_super_followers_only and "
                                             "nullcast are not privacy levels and are not sent)",
                                 api_default="public; this skill's default (`private`) is a FAIL here, so --privacy "
                                             "public must be given"),
        "rate_limit.requests": _known({"POST /2/tweets": "100 per 15 minutes per user",
                                       "media upload initialize / append / finalize": "1,875 per 15 minutes per user",
                                       "GET /2/media/upload": "1,000 per 15 minutes per user"},
                                      "requests per window per user token", _X_RATE,
                                      "POST /2/tweets | 10,000/24hrs | 100/15min", checked=False),
        "quota.daily_posts": _unknown("no per-account daily post cap is stated for the API beyond the rate limits",
                                      [_X_POST, _X_RATE]),
        "token.scopes": _known(["media.write", "tweet.write", "tweet.read", "users.read"], "OAuth 2.0 scopes", _X_POST,
                               "POST /2/tweets: tweet.read, users.read, tweet.write", checked=False,
                               also=_X_INIT + " (media.write)",
                               note="not checked offline; X answers 401 or 403 (AUTH_FAILED)"),
        "token.lifetime_s": _known(2 * 3600, "seconds (access token)", _X_OAUTH,
                                   "will only stay valid for two hours", checked=False,
                                   note="this skill reads an access token only; refreshing it (offline.access) is not "
                                        "implemented (ADR-007)"),
    },
    "facebook": {
        # the video file: no size or duration limit is stated on the pages read (the Video API overview, the publishing
        # guide, the Resumable Upload API, the Page videos reference); error 382 "The video file is too small" is named
        # without a number
        "video.max_bytes": _unknown("no maximum file size is stated", [_FB_PUBLISH, _FB_UPLOAD, _FB_PAGE_VIDEOS,
                                                                        _FB_OVERVIEW]),
        "video.min_bytes": _unknown("error 382 \"The video file is too small\" is named, with no size",
                                    [_FB_PAGE_VIDEOS]),
        "video.max_duration_s": _unknown("no maximum duration is stated", [_FB_PUBLISH, _FB_PAGE_VIDEOS, _FB_OVERVIEW]),
        # the upload session's file_type must name the type, and video/mp4 is the only video type listed
        "video.file_formats": _known([".mp4"], "file extensions", _FB_UPLOAD,
                                     "file_type ... Valid values are: application/pdf, image/jpeg, image/jpg, image/png, "
                                     "and video/mp4", enforced=True, content_types={".mp4": "video/mp4"}),
        "video.codecs": _delegated("video codec", _FB_CHECK),
        "video.resolution": _delegated("frame size", _FB_CHECK),
        "video.aspect": _delegated("aspect ratio", _FB_CHECK),
        "video.fps": _delegated("frame rate", _FB_CHECK),
        "upload.chunk_bytes": _known("one body from file_offset to the end (no chunk size is documented)", "bytes",
                                     _FB_UPLOAD, "Send another POST ... setting file_offset to the value returned by "
                                     "the GET", checked=False, used="the whole remaining file, streamed from disk"),
        # metadata
        "title.max_chars": _unknown("no maximum title length is stated (\"title: The title of the video. Supports "
                                    "emoji\")", [_FB_PAGE_VIDEOS, _FB_PUBLISH]),
        "title.forbidden_chars": _unknown("no forbidden characters are stated", [_FB_PAGE_VIDEOS]),
        "description.max_chars": _unknown("no maximum description length is stated", [_FB_PAGE_VIDEOS, _FB_PUBLISH]),
        "privacy.values": _known(["public", "unlisted"], "enum (this skill's names, sent as published / secret)",
                                 _FB_PAGE_VIDEOS,
                                 "published: Whether a post about the video is published (default true). ... secret: "
                                 "If true, the video doesn't appear on Facebook or in search, but can be viewed via "
                                 "permalink and embeds.",
                                 maps_to={"public": {"published": True, "secret": False},
                                          "unlisted": {"published": True, "secret": True}},
                                 not_offered="private: an unpublished Page video (published=false) has no documented "
                                             "audience; scheduled, backdated and targeted posts are not offered",
                                 api_default="published, not secret; this skill always sends both, and its default "
                                             "(`private`) is a FAIL here"),
        "rate_limit.requests": _unknown("no number is stated on the pages read; throttling is typed (error codes 4, "
                                        "17 and 341: \"Wait and retry the operation\")", [_FB_ERRORS, _FB_PAGE_VIDEOS]),
        "quota.daily_posts": _unknown("no daily post cap is stated", [_FB_PAGE_VIDEOS, _FB_PUBLISH]),
        "token.scopes": _known(["pages_manage_posts", "pages_read_engagement", "pages_show_list"],
                               "Facebook Login permissions", _FB_PUBLISH,
                               "A Page access token requested by a person who can perform the CREATE_CONTENT task on "
                               "the Page ... pages_show_list, pages_read_engagement, pages_manage_posts",
                               checked=False, upload=_FB_UPLOAD + " (the upload takes a User access token)",
                               note="not checked offline: Facebook answers with code 190, 10 or 200-299 (AUTH_FAILED)"),
        "token.lifetime_s": _unknown("the token lifetimes are not stated on the pages read", [_FB_PUBLISH, _FB_UPLOAD]),
    },
    "linkedin": {
        # the video file (non-connected-television video). "75kb" is read as 75 KiB (the larger reading of a minimum)
        # and "500MB" as decimal (the smaller reading of a maximum), so a file plan passes is in range either way.
        "video.min_bytes": _known(75 * 1024, "bytes", _LI_VIDEOS, "File size: Between 75kb and 500MB.",
                                  documented_as="75kb"),
        "video.max_bytes": _known(500 * 1000 ** 2, "bytes", _LI_VIDEOS, "File size: Between 75kb and 500MB.",
                                  documented_as="500MB",
                                  also="initializeUploadRequest.fileSizeBytes: \"Maximum allowed Videos size is 5GB\" "
                                       "(the API's own cap; the stated specification, 500MB, is the one checked)"),
        "video.min_duration_s": _known(3, "seconds", _LI_VIDEOS, "Length: Three seconds to 30 minutes."),
        "video.max_duration_s": _known(30 * 60, "seconds", _LI_VIDEOS, "Length: Three seconds to 30 minutes."),
        # the parts are PUT as application/octet-stream, but the specification names one format
        "video.file_formats": _known([".mp4"], "file extensions", _LI_VIDEOS, "File format: MP4.", enforced=True),
        "video.codecs": _delegated("video codec", _LI_CHECK),
        "video.resolution": _delegated("frame size", _LI_CHECK),
        "video.aspect": _delegated("aspect ratio", _LI_CHECK),
        "video.fps": _delegated("frame rate", _LI_CHECK),
        "upload.chunk_bytes": _known("given by LinkedIn per part (uploadInstructions: 0 - 4194303, 4194304 - 8388607, "
                                     "...)", "bytes per part", _LI_VIDEOS,
                                     "Parts are sorted by non-overlapping byte-range. The first entry is byte range: 0 - "
                                     "4194303, the next one with 4194304 - 8388607 and so on.", checked=False,
                                     used="exactly the byte ranges of uploadInstructions"),
        "upload.session_validity_s": _known("typically 30 days", "upload URL lifetime", _LI_VIDEOS,
                                            "Typically URLs expire 30 days from the time an upload is initialized.",
                                            checked=False),
        # metadata: the post's commentary is the bound title, as `little` text with every reserved character escaped
        "title.max_chars": _unknown("the Posts API states no maximum length for `commentary` (it names the error "
                                    "FIELD_LENGTH_TOO_LONG only); the 3000-character limit is stated for the older UGC "
                                    "Post API's shareCommentary.text, not for the Posts API, so nothing is checked",
                                    [_LI_SCHEMA, _LI_POSTS, _LI_UGC]),
        "title.forbidden_chars": _unknown("none: the reserved `little` characters (| { } @ [ ] ( ) < > # \\ * _ ~) are "
                                          "escaped with a backslash, so any text is sent as typed",
                                          [_LI_LITTLE, _LI_SCHEMA]),
        "privacy.values": _known(["public"], "enum (this skill's names, sent as visibility)", _LI_SCHEMA,
                                 "visibility ... CONNECTIONS - Represents 1st degree network of owner. PUBLIC - Anyone "
                                 "can view this. LOGGED_IN - Viewable by logged in members only. CONTAINER - Visibility is "
                                 "delegated to the owner of the container entity.",
                                 maps_to={"public": "PUBLIC"},
                                 not_offered="CONNECTIONS and LOGGED_IN (neither is private nor unlisted, so neither "
                                             "maps to this skill's names) and CONTAINER (no container is used)",
                                 api_default="none (visibility is required); this skill's default (`private`) is a "
                                             "FAIL here, so --privacy public must be given"),
        "rate_limit.requests": _unknown("no fixed rate limit is stated on the Videos or Posts API pages (429 "
                                        "TOO_MANY_REQUESTS is named)", [_LI_VIDEOS, _LI_POSTS]),
        "quota.daily_posts": _unknown("no daily post cap is stated", [_LI_POSTS]),
        "token.scopes": _known(["w_member_social", "w_organization_social"], "OAuth 2.0 permissions", _LI_POSTS,
                               "w_member_social | Post, comment, and like posts on behalf of an authenticated member. "
                               "... w_organization_social | Post, comment, and like posts on behalf of an organization.",
                               checked=False,
                               note="w_member_social for a person author, w_organization_social (and an ADMINISTRATOR, "
                                    "DIRECT_SPONSORED_CONTENT_POSTER or CONTENT_ADMIN page role) for an organization "
                                    "author; not checked offline: LinkedIn answers 401 or 403 (AUTH_FAILED)"),
        "token.lifetime_s": _known(60 * 86400, "seconds (access token)", _LI_OAUTH,
                                   "Currently, all access tokens are issued with a 60-day lifespan.", checked=False,
                                   note="this skill reads an access token only; refreshing it is not implemented "
                                        "(ADR-008)"),
    },
    "vimeo": {
        # the video file. upload.size is stated in bytes, so there is no decimal/binary question here.
        "video.max_bytes": _known(322122547200, "bytes", _VM_REF_UPLOAD,
                                  "upload.size: The size in bytes of the video to upload. The maximum value of this field "
                                  "is `322122547200`, which corresponds to 300 GB.", documented_as="300 GB",
                                  also=_VM_UPLOAD + " (\"The maximum file size is 300 GB\")"),
        "video.max_duration_s": _known(24 * 3600, "seconds", _VM_UPLOAD,
                                       "The maximum file size is 300 GB, and the maximum duration is 24 hours."),
        "video.file_formats": _unknown("no list of accepted containers is stated: \"our uploader accepts a wide range of "
                                       "video codecs and file types\"; nothing is checked",
                                       [_VM_UPLOAD, _VM_UPLOAD_START, _VM_REF_UPLOAD]),
        "video.codecs": _delegated("video codec", _VM_NO_SPEC),
        "video.resolution": _delegated("frame size", _VM_NO_SPEC),
        "video.aspect": _delegated("aspect ratio", _VM_NO_SPEC),
        "video.fps": _delegated("frame rate", _VM_NO_SPEC),
        "upload.content_type": _known("application/offset+octet-stream", "MIME type", _VM_UPLOAD,
                                      "Headers for PATCH to {upload.upload_link}: Tus-Resumable 1.0.0; Upload-Offset 0 for "
                                      "the initial request; Content-Type application/offset+octet-stream", checked=False),
        "upload.chunk_bytes": _known("128-512 MB recommended", "bytes per PATCH", _VM_UPLOAD,
                                     "if you're using a tus library that breaks the video file into separate chunks based "
                                     "on a maximum size value, we recommend setting the chunk size in the range of "
                                     "128–512 MB", checked=False, used="128 MiB (134 217 728 bytes): within the range "
                                                                       "under either reading of MB"),
        # metadata. The limits are stated on "Edit a video" (PATCH /videos/{video_id}) for the same fields the upload
        # request takes (name, description); the upload method itself states none.
        "title.max_chars": _known(128, "characters", _VM_REF_EDIT,
                                  "name: The title of the video. This field can hold a maximum of 128 characters."),
        "title.forbidden_chars": _unknown("no forbidden characters are stated", [_VM_REF_UPLOAD, _VM_REF_EDIT]),
        "description.max_chars": _known(5000, "characters", _VM_REF_EDIT,
                                        "description: The description of the video. This field can hold a maximum of 5000 "
                                        "characters."),
        "description.forbidden_chars": _unknown("no forbidden characters are stated", [_VM_REF_UPLOAD, _VM_REF_EDIT]),
        # The skill's privacy names, each sent as one privacy.view value (maps_to). `private` is `nobody`, the most
        # private option: "No one except the owner can access the video."
        "privacy.values": _known(["private", "unlisted", "public"], "enum (this skill's names, sent as privacy.view)",
                                 _VM_REF_UPLOAD,
                                 "privacy.view ... `nobody` - No one except the owner can access the video. This privacy "
                                 "setting appears as `Private` on the Vimeo front end. ... `unlisted` - Only those with the "
                                 "private link can access the video. ... `anybody` - Anyone can access the video. This "
                                 "privacy setting appears as `Public` on the Vimeo front end.",
                                 maps_to={"private": "nobody", "unlisted": "unlisted", "public": "anybody"},
                                 api_values=["anybody", "contacts", "disable", "nobody", "password", "team", "unlisted",
                                             "users"],
                                 not_offered="disable, password, team, contacts and users: password needs a secret in the "
                                             "request, team a team plan, contacts and users are deprecated",
                                 api_default="not stated (the account's own default would apply); this skill always "
                                             "sends privacy.view, `nobody` unless --privacy says otherwise"),
        # account limits: they exist (typed errors) but their values depend on the membership and are not stated
        "storage.max_bytes": _unknown("per account; \"Error code 4101: The authenticated user's maximum disk space has "
                                      "been reached.\" No number is stated", [_VM_REF_UPLOAD, _VM_UPLOAD_START]),
        "quota.weekly_uploads": _unknown("per account; \"Error code 4102: The authenticated user's allotted weekly quota "
                                         "has been reached.\" No number is stated", [_VM_REF_UPLOAD]),
        "quota.daily_uploads": _unknown("per account; \"Error code 4104: The authenticated user's allotted daily quota has "
                                        "been reached.\" No number is stated", [_VM_REF_UPLOAD]),
        "rate_limit.requests_per_minute": _known({"Vimeo Free": 25, "Vimeo Starter": 125, "Vimeo Standard": 250,
                                                  "Vimeo Advanced": 750, "Vimeo Studio": 1500, "Vimeo Enterprise": 2500},
                                                 "API requests per minute per user (authenticated token)", _VM_RATE,
                                                 "Your end user's Vimeo membership level determines how many API requests "
                                                 "they can make from your app per minute. ... any request from your app "
                                                 "for this end user results in HTTP status code 429 and error code 9000.",
                                                 checked=False),
        "upload.access": _known("the API app needs upload access (Vimeo Free: an approved app, by support ticket)",
                                "account requirement", _VM_UPLOAD_START,
                                "for your app to be able to upload videos to Vimeo, you need to turn on upload access. "
                                "This capability isn't enabled by default.", checked=False),
        "token.scopes": _known(["upload", "edit"], "OAuth scopes", _VM_UPLOAD,
                               "To use this guide, you need: ... An access token with the upload and edit scopes.",
                               checked=False, method_security=_VM_REF_UPLOAD + " (Upload a video: oauth2 scope `upload`)",
                               note="not checked: a personal access token's scopes are not in any answer this uploader "
                                    "reads; the API refuses a token without them (401: AUTH_FAILED; another refusal: "
                                    "UPLOAD_REJECTED)"),
    },
}


def limits_of(platform_id: str) -> Dict[str, Dict[str, Any]]:
    return LIMITS.get(platform_id, {})


def value(platform_id: str, key: str) -> Any:
    """A documented limit's value, or None when it is unknown, delegated or absent (nothing is checked against None)."""
    entry = limits_of(platform_id).get(key)
    return entry["value"] if entry and entry["status"] == "documented" else None


def tags_length(tags: List[str]) -> int:
    """The length YouTube counts for a tag list: commas between tags count, and a tag with a space counts as if it were
    wrapped in quotation marks (snippet.tags[] in the Videos resource)."""
    if not tags:
        return 0
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + len(tags) - 1


def x_weighted_length(text: str) -> Dict[str, Any]:
    """The length X counts for a post's text (twitter-text v3), NFC-normalised: code points in X_WEIGHT_ONE_RANGES
    weigh 1, every other 2, a URL 23. `exact` is true when the text has no URL-like token and no emoji, where this is
    the count X makes; otherwise `value` is an upper bound (a URL-like token counts max(its weight, 23) and each code
    point of an emoji sequence 2, where X counts the whole sequence as 2)."""
    text = unicodedata.normalize("NFC", text)
    exact = True
    n = 0
    for token in re.split(r"(\s+)", text):
        weight = 0
        for ch in token:
            cp = ord(ch)
            if cp >= 0x1F000 or cp in (0x200D, 0xFE0F) or 0x2600 <= cp <= 0x27BF:
                exact = False
            weight += 1 if any(a <= cp <= b for a, b in X_WEIGHT_ONE_RANGES) else 2
        if re.search(r"://|^www\.|\.[A-Za-z]{2,}(/|$)", token):
            exact = False
            weight = max(weight, X_URL_LENGTH)
        n += weight
    return {"value": n, "exact": exact}


def summary(platform_id: str) -> Dict[str, List[str]]:
    """Which keys are documented, unknown and delegated (for plan's report and the contract)."""
    out: Dict[str, List[str]] = {"documented": [], "unknown": [], "delegated": []}
    for k, v in sorted(limits_of(platform_id).items()):
        out[v["status"]].append(k)
    return out

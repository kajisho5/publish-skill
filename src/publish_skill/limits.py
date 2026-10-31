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


def summary(platform_id: str) -> Dict[str, List[str]]:
    """Which keys are documented, unknown and delegated (for plan's report and the contract)."""
    out: Dict[str, List[str]] = {"documented": [], "unknown": [], "delegated": []}
    for k, v in sorted(limits_of(platform_id).items()):
        out[v["status"]].append(k)
    return out

"""Machine-readable contract (`publish contract --json`), in the shape the AI Video Production OS registry reads
(skill_id, version, contract_version, provides, dependencies, not_provided)."""
from __future__ import annotations

from typing import Any, Dict

from . import PACKAGE_NAME, SKILL_ID, __version__
from . import adapter
from . import keychain
from .credentials import PRECEDENCE, REDACTED, SOURCES
from .doctor import DOCTOR_SCHEMA, FFMPEG_SKILL_ENV, IMPLEMENTED_OPS, OP_STEP
from .errors import ERROR_TABLE
from .limits import LIMITS
from .plan import PLAN_SCHEMA, STATUSES
from .platforms import get_platform, platform_ids
from .upload import TOKEN_BINDS, TOKEN_PREFIX

_METADATA_ARGV = ["[--title <t>]", "[--description <d> | --description-file <f>]", "[--tag <t> ...]",
                  "[--privacy private|unlisted|public]", "[--thumbnail <image>]",
                  "[--caption <file> ... --caption-language <bcp47> ...]"]
# the only modules with network code: one per implemented uploader (ADR-004 .. ADR-009)
NETWORK_MODULES = ["src/publish_skill/platforms/facebook.py", "src/publish_skill/platforms/linkedin.py", "src/publish_skill/platforms/tiktok.py",
                   "src/publish_skill/platforms/vimeo.py",
                   "src/publish_skill/platforms/x.py", "src/publish_skill/platforms/youtube.py"]

CONTRACT_VERSION = "1.0"
CAPABILITY_IDS = {"plan": "publish.plan", "upload": "publish.upload"}
_OP_TOOLS = {
    "plan": {"role": "measurement", "network": False, "writes_media": False,
             "description": "Check a deliverable and its title / description / tags / privacy / thumbnail / captions against "
                            "the platform's upload limits, and the deliverable against ffmpeg-skill's delivery spec "
                            "(`check`), offline (structured FAIL with a fix per row, nothing uploaded; exit 1 unless the "
                            "verdict is PASS or WARN)",
             "schema": PLAN_SCHEMA, "row_statuses": list(STATUSES), "verdicts": ["PASS", "WARN", "FAIL", "INCOMPLETE"],
             "argv": ["plan", "<deliverable>", "--platform <id>"] + _METADATA_ARGV + ["[--ffmpeg-skill <dir>]", "--json"]},
    "upload": {"role": "execution", "network": True, "writes_media": False,
               "description": "Upload a deliverable, its thumbnail and captions to a platform (YouTube: the Data API v3 "
                              "resumable upload; Vimeo: the tus resumable upload, title, description and privacy "
                              "only; TikTok: the Content Posting API Direct Post, title and privacy only; X: the chunked media upload "
                              "and one public post, its text the title; LinkedIn: the Videos API multipart upload and one public post, "
                              "its commentary the title; Facebook: the Resumable Upload API and one Page video, title, description and "
                              "privacy (public or unlisted) only). Dry run unless --confirm carries the token the dry run printed for "
                              "exactly this file, metadata and platform; then a receipt, redacted",
               "argv": ["upload", "<deliverable>", "--platform <id>"] + _METADATA_ARGV + ["[--confirm <token>]", "--json"]},
}


def tools() -> list:
    out = []
    for op, spec in _OP_TOOLS.items():
        out.append({"tool_id": f"{SKILL_ID}/{op}", "mutates_input": False, "deterministic": op == "plan",
                    "implemented": IMPLEMENTED_OPS[op], "roadmap_step": OP_STEP[op], **spec})
    out[-1]["uploaders"] = {pid: get_platform(pid).implemented for pid in platform_ids()}
    out += [
        {"tool_id": f"{SKILL_ID}/doctor", "role": "diagnostic", "network": False, "writes_media": False, "mutates_input": False,
         "deterministic": False, "implemented": True, "schema": DOCTOR_SCHEMA,
         "description": "Python, ffmpeg-skill, and per platform: uploader implemented, credentials present / absent (never values)",
         "argv": ["doctor", "[--offline]", "[--ffmpeg-skill <dir>]", "--json"]},
        {"tool_id": f"{SKILL_ID}/auth", "role": "configuration", "network": False, "writes_media": False, "mutates_input": False,
         "deterministic": False, "implemented": True, "roadmap_step": 3,
         "description": "Credentials in the OS keychain: status (present / absent, never values), login (values from stdin "
                        "or a hidden prompt, never from argv), logout",
         "argv": ["auth", "status|login|logout", "[--platform <id>]", "[--variable <name> ...]", "--json"],
         "secret_input": "stdin (one value per line) or a prompt that does not echo; no flag takes a value"},
        {"tool_id": f"{SKILL_ID}/platforms", "role": "diagnostic", "network": False, "writes_media": False, "mutates_input": False,
         "deterministic": False, "implemented": True, "description": "Registered platforms with status and credential variable names",
         "argv": ["platforms", "--json"]},
        {"tool_id": f"{SKILL_ID}/contract", "role": "metadata", "network": False, "writes_media": False, "mutates_input": False,
         "deterministic": True, "implemented": True, "description": "This contract", "argv": ["contract", "--json"]},
    ]
    return out


def contract() -> Dict[str, Any]:
    deps = [
        {"id": "ffmpeg-skill", "skill_id": "ffmpeg-skill", "kind": "skill", "required": False, "for": [CAPABILITY_IDS["plan"]],
         "needed_from_step": OP_STEP["plan"],
         "version_range": "any release whose contract (`scripts/_contract.py --json --static`) declares "
                          + "; ".join(f"{t} with {', '.join('--' + f.replace('_', '-') for f in fl)}"
                                      for t, fl in ((t, [f for f in fl if f != "input"]) for t, fl in adapter.REQUIRED_FLAGS.items()))
                          + ", and execution.network: false (capability detection, no version window)",
         "tools_used": list(adapter.TOOLS_USED),
         "reads": "the platform delivery specs through `check --platform` (ffmpeg-skill owns them; this skill never copies them)",
         "without_it": "plan reports the delivery spec and duration rows as UNCHECKED (verdict INCOMPLETE)",
         "located_by": ["--ffmpeg-skill"] + [f"${k}" for k in FFMPEG_SKILL_ENV]
                       + ["~/.claude/skills/ffmpeg-skill", "./vendor/ffmpeg-skill", "../ffmpeg-skill"]},
    ]
    return {
        "skill_id": SKILL_ID,
        "id": SKILL_ID,
        "skill": {"id": SKILL_ID, "version": __version__, "package": PACKAGE_NAME},
        "version": __version__,
        "contract_version": CONTRACT_VERSION,
        "provides": [{"id": CAPABILITY_IDS[op], "lifecycle": "EXPERIMENTAL" if IMPLEMENTED_OPS[op] else "PROPOSED",
                      "tool_id": f"{SKILL_ID}/{op}"} for op in CAPABILITY_IDS],
        "dependencies": deps,
        "not_provided": ["platform delivery specs: codecs, resolution, bitrate, loudness (ffmpeg-skill owns them)",
                         "encoding or transcoding for a platform (ffmpeg-skill export)",
                         "quality control of the deliverable (qc-skill; uploads come after QC)",
                         "deciding whether to publish (a human confirmation the agent records)",
                         "downloading media from a URL (ingest-skill)",
                         "credentials from files, flags or request fields (the environment or the OS keychain only)",
                         "keyring backends other than the three OS keychains (plain files, plug-ins, network stores)",
                         "obtaining an OAuth refresh token (the consent screen needs the network; `auth login` stores one)",
                         "uploading without --confirm",
                         "resuming an upload in a later run (an interrupted run uploads nothing; run it again)",
                         "an HTTP(S) proxy for the upload (direct connections only in this release)",
                         "Vimeo: tags, thumbnails and text tracks (refused before a token is printed; add them on Vimeo)",
                         "Vimeo: the OAuth authorization-code flow (`auth login` stores a personal access token)",
                         "TikTok: a description, tags, a cover image, captions, the unlisted privacy, the duet / stitch "
                         "/ comment switches and the branded-content or AI-generated labels (refused or not sent; set "
                         "them in the TikTok app)",
                         "TikTok: the OAuth flow and refreshing the 24-hour access token (`auth login` stores an access "
                         "token)",
                         "TikTok: PULL_FROM_URL, photo posts and drafts sent to the creator's inbox (video.upload)",
                         "X: private or unlisted posts (X has none), a description, tags, a thumbnail, captions, "
                         "reply settings, polls and labels (refused or not sent)",
                         "X: the OAuth 2.0 flow and refreshing the two-hour access token (`auth login` stores an "
                         "access token); OAuth 1.0a",
                         "LinkedIn: CONNECTIONS, LOGGED_IN and CONTAINER visibility, a description, tags, a thumbnail, "
                         "captions, mentions and hashtag markup (the commentary is escaped `little` text), a media "
                         "title, targeting, calls to action and sponsored content (refused or not sent)",
                         "LinkedIn: the OAuth flow, looking up the author's URN, and refreshing the 60-day access "
                         "token (`auth login` stores an access token and the author URN)",
                         "Facebook: private (unpublished) videos, tags, a thumbnail, captions, scheduling, backdating, "
                         "targeting, crossposting and Reels (refused or not sent)",
                         "Facebook: Facebook Login and exchanging tokens (`auth login` stores the app id, a user access "
                         "token, the Page id and a Page access token)"],
        "tools": tools(),
        "platforms": {"registered": {pid: get_platform(pid).spec() for pid in platform_ids()},
                      "per_platform_pr": "roadmap step 5"},
        "upload_limits": {"owner": SKILL_ID, "delivery_spec_owner": "ffmpeg-skill", "decision": "docs/decisions.md ADR-002",
                          "platforms": LIMITS},
        "confirmation": {"required_for": [f"{SKILL_ID}/upload"], "flag": "--confirm", "token_prefix": TOKEN_PREFIX,
                         "token_binds": list(TOKEN_BINDS),
                         "default": "dry run (no network, nothing uploaded)",
                         "from_environment_or_config": False,
                         "decision": "docs/decisions.md ADR-001"},
        "credentials": {"sources": list(SOURCES), "precedence": PRECEDENCE, "printed": False,
                        "reported_as": ["present", "absent"], "redaction": REDACTED, "from_files_or_flags": False,
                        "keychain": {"service": keychain.SERVICE, "account": "the environment variable's name",
                                     "backends": list(keychain.BACKEND_LABELS), "optional_extra": keychain.EXTRA,
                                     "requirement": keychain.REQUIREMENT, "install": keychain.INSTALL,
                                     "without_it": "credentials come from the environment only; doctor says so",
                                     "turn_off": f"{keychain.OFF_ENV}=off",
                                     "write_input": "stdin or a prompt that does not echo, never argv"},
                        "decision": "docs/decisions.md ADR-001, ADR-003"},
        "execution": {"canonical_invocation": ["publish", "<command>", "...", "--json"], "network": True,
                      "network_tools": [f"{SKILL_ID}/upload"], "network_requires": "--confirm <token>",
                      "network_in_this_release": True, "network_code": list(NETWORK_MODULES),
                      "network_hosts": ["oauth2.googleapis.com", "www.googleapis.com", "api.vimeo.com",
                                        "*.vimeo.com (the tus upload link Vimeo returns; https only)",
                                        "open.tiktokapis.com",
                                        "*.tiktokapis.com (the upload_url TikTok returns; https only)",
                                        "api.x.com", "api.linkedin.com",
                                        "*.linkedin.com (the signed uploadUrl LinkedIn returns; https only)",
                                        "graph.facebook.com", "graph-video.facebook.com"],
                      "upload_protocol": {"youtube": {
                          "oauth": "refresh token -> access token (https://oauth2.googleapis.com/token)",
                          "resumable": "https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol",
                          "methods": ["videos.insert", "thumbnails.set", "captions.insert"],
                          "chunk_bytes": "a multiple of 256 KiB (8 MiB)",
                          "retries": "5xx, 429, rate-limit reasons, connection errors and timeouts: exponential backoff "
                                     "(or Retry-After), then the session's status and a resume",
                          "decision": "docs/decisions.md ADR-004"},
                          "vimeo": {
                          "auth": "personal access token (Authorization: bearer, to api.vimeo.com only)",
                          "resumable": "https://developer.vimeo.com/api/upload/videos (tus 1.0.0, https://tus.io/protocols/resumable-upload)",
                          "methods": ["POST /me/videos (upload.approach tus)", "PATCH upload_link", "HEAD upload_link"],
                          "chunk_bytes": "128 MiB (the guide recommends 128-512 MB)",
                          "metadata_sent": ["name", "description", "privacy.view"],
                          "retries": "5xx, 429, connection errors and timeouts on PATCH/HEAD: exponential backoff (or "
                                     "Retry-After), then HEAD and a resume from Upload-Offset; the create POST is retried "
                                     "only on 429/5xx answers",
                          "decision": "docs/decisions.md ADR-005"},
                          "tiktok": {
                          "auth": "user access token with the video.publish scope (Authorization: Bearer, to "
                                  "open.tiktokapis.com only)",
                          "resumable": "https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide",
                          "methods": ["POST /v2/post/publish/creator_info/query/", "POST /v2/post/publish/video/init/ "
                                      "(source FILE_UPLOAD)", "PUT upload_url (Content-Range)",
                                      "POST /v2/post/publish/status/fetch/"],
                          "chunk_bytes": "64 000 000 (the guide: 5-64 MB, the final chunk up to 128 MB); a file of at "
                                         "most that size whole",
                          "metadata_sent": ["title", "privacy_level"],
                          "retries": "5xx, 429, connection errors and timeouts on a chunk: exponential backoff (or "
                                     "Retry-After), then the same chunk again; 416: the post's uploaded_bytes and a "
                                     "resume; the init POST is retried only on 429/5xx answers",
                          "decision": "docs/decisions.md ADR-006"},
                          "x": {
                          "auth": "OAuth 2.0 user access token with media.write, tweet.write, tweet.read and users.read "
                                  "(Authorization: Bearer, to api.x.com only)",
                          "resumable": "https://docs.x.com/x-api/media/quickstart/media-upload-chunked",
                          "methods": ["POST /2/media/upload/initialize (tweet_video)",
                                      "POST /2/media/upload/{id}/append (multipart, segment_index)",
                                      "POST /2/media/upload/{id}/finalize",
                                      "GET /2/media/upload?command=STATUS&media_id={id}", "POST /2/tweets"],
                          "chunk_bytes": "4 MiB (the guide: at or below 5 MB)",
                          "metadata_sent": ["text (the title)", "media.media_ids"],
                          "retries": "5xx, 429, connection errors and timeouts on an append: exponential backoff (or "
                                     "Retry-After / x-rate-limit-reset), then the same segment_index again, and "
                                     "finalize's size must equal the file's; initialize and finalize are retried only on "
                                     "429/5xx answers; the post only on 429 (never twice)",
                          "decision": "docs/decisions.md ADR-007"},
                          "linkedin": {
                          "auth": "OAuth 2.0 member access token with w_member_social or w_organization_social "
                                  "(Authorization: Bearer, to api.linkedin.com only), Linkedin-Version 202610, "
                                  "X-Restli-Protocol-Version 2.0.0",
                          "resumable": "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/"
                                       "videos-api",
                          "methods": ["POST /rest/videos?action=initializeUpload",
                                      "PUT <uploadInstructions[].uploadUrl> (each byte range; ETag)",
                                      "POST /rest/videos?action=finalizeUpload (uploadedPartIds)",
                                      "GET /rest/videos/{urn} (until AVAILABLE)", "POST /rest/posts (201, x-restli-id)"],
                          "chunk_bytes": "the byte ranges LinkedIn gives (4 MiB parts)",
                          "metadata_sent": ["commentary (the title, little-escaped)", "visibility PUBLIC",
                                            "content.media.id"],
                          "retries": "5xx, 429, connection errors and timeouts on a part: exponential backoff (or "
                                     "Retry-After), then the same part again; initialize and finalize are retried only "
                                     "on 429/5xx answers; the post only on 429 (never twice)",
                          "decision": "docs/decisions.md ADR-008"},
                          "facebook": {
                          "auth": "a user access token for the upload (Authorization: OAuth) and a Page access token "
                                  "for the publish (CREATE_CONTENT; pages_manage_posts, pages_read_engagement, "
                                  "pages_show_list), to graph.facebook.com and graph-video.facebook.com only; Graph API "
                                  "v26.0",
                          "resumable": "https://developers.facebook.com/docs/graph-api/guides/upload",
                          "methods": ["POST /v26.0/<APP_ID>/uploads (file_name, file_length, file_type=video/mp4)",
                                      "POST /v26.0/upload:<UPLOAD_SESSION_ID> (file_offset; the file; -> h)",
                                      "GET /v26.0/upload:<UPLOAD_SESSION_ID> (file_offset, to resume)",
                                      "POST graph-video /v26.0/<PAGE_ID>/videos (fbuploader_video_file_chunk)"],
                          "chunk_bytes": "the whole remaining file from file_offset, streamed",
                          "metadata_sent": ["title", "description", "published", "secret"],
                          "retries": "5xx, 429, error codes 1, 2, 4, 17, 341, connection errors and timeouts on the "
                                     "upload: exponential backoff (or Retry-After), then file_offset and the rest of the "
                                     "file; the session request only on answers; the publish only on 429 or a "
                                     "throttling code (never twice)",
                          "decision": "docs/decisions.md ADR-009"}},
                      "shell": False, "input_is_read_only": True,
                      "child_processes": "adapter.py only: [python, <ffmpeg-skill>/scripts/<tool>.py, ...] as an argv list "
                                    "(tools: _contract, export --list, check), never a shell; ffmpeg-skill declares no "
                                    "network use"},
        "errors": {code: {"exit_code": ex, "retryable": rt} for code, (ex, rt) in ERROR_TABLE.items()},
        "error_shape": {"ok": False, "error": {"code": "<errors key>", "message": "str", "retryable": "bool", "details": "object"}},
    }

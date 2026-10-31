<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/app-icon-dark.png">
    <img src="brand/app-icon.png" width="128" alt="publish-skill app icon">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/logo/publish-skill-wordmark-white.svg">
    <img src="brand/logo/publish-skill-wordmark-black.svg" height="48" alt="publish-skill">
  </picture>
</p>

<p align="center"><b>Upload to delivery platforms, after QC</b></p>



Upload finished deliverables to delivery platforms (YouTube, Vimeo, TikTok, X and LinkedIn) for the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS) (department 納品 / Delivery), after QC and a human's confirmation. Platform delivery specs (codecs, resolution, loudness) belong to [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill); this skill reads them and owns only the upload-side limits.

**Status: 0.1.0, unreleased.** `contract`, `doctor`, `platforms`, `plan`, `auth` and `upload` work. `upload` is a dry run unless it is given the exact confirmation token; then the YouTube uploader ([ADR-004](docs/decisions.md), [roadmap](ECOSYSTEM_ROADMAP.md) step 4) sends the video, its thumbnail and its captions with the YouTube Data API v3 resumable upload, the Vimeo uploader ([ADR-005](docs/decisions.md), step 5) sends the video with its title, description and privacy with Vimeo's tus resumable upload, and the TikTok uploader ([ADR-006](docs/decisions.md), step 5) posts the video with its title and privacy through the Content Posting API's Direct Post, and the X uploader ([ADR-007](docs/decisions.md), step 5) uploads the video with the X API v2 chunked media upload and makes one public post with the title as its text, and the LinkedIn uploader ([ADR-008](docs/decisions.md), step 5) uploads the video with the Videos API and makes one public post with the title as its commentary. All five have been tested only against local mocks of the APIs, never against the platforms themselves: the first real upload to each is the owner's, with their credentials and their confirmation.

```bash
pip install -e .                                          # stdlib only, Python >= 3.9
pip install -e '.[keychain]'                              # optional: the OS keychain (keyring>=25.0)
publish doctor                                            # platforms, credentials present/absent, keychain, ffmpeg-skill
publish plan final.mp4 --platform youtube --title "Launch" --privacy unlisted --thumbnail thumb.jpg --json
                                                          # offline: every limit, PASS/WARN/FAIL with a fix per row
publish upload final.mp4 --platform youtube --title "Launch" --privacy unlisted --thumbnail thumb.jpg --json
                                                          # dry run: what would be sent, and the confirm_token
publish upload final.mp4 --platform youtube --title "Launch" --privacy unlisted --thumbnail thumb.jpg --confirm <confirm_token> --json
                                                          # the upload, after QC and a human's approval: a receipt
publish auth login --platform youtube                     # store credentials in the OS keychain (stdin or a hidden prompt)
publish auth status --json                                # present/absent in the environment and the keychain
publish auth logout --platform youtube                    # remove them from the keychain
publish upload talk.mp4 --platform vimeo --title "Keynote" --json
                                                          # Vimeo: the same gate; title, description and privacy only
publish upload short.mp4 --platform tiktok --title "Launch #film" --json
                                                          # TikTok: the same gate; title (the caption) and privacy only
publish upload clip.mp4 --platform x --title "Launch day" --privacy public --json
                                                          # X: the same gate; title (the post's text), public only
publish upload clip.mp4 --platform linkedin --title "Launch day" --privacy public --json
                                                          # LinkedIn: the same gate; title (the commentary), public only
publish contract --json                                   # machine-readable contract (alias: skill --json)
```

Metadata flags, the same on `plan` and `upload`: `--title`, `--description` or `--description-file` (UTF-8), `--tag` (repeat), `--privacy private|unlisted|public` (default `private`; the YouTube API would default to `public`; on Vimeo `private` is `privacy.view: nobody`, "no one except the owner"), `--thumbnail`, `--caption` (repeat) with `--caption-language` (its BCP-47 tag, one per `--caption`, in order).

## Safety ([ADR-001](docs/decisions.md))

- **Dry run by default.** Without `--confirm`, `upload` reads the file and reports; it opens no connection.
- **A confirmation token per deliverable.** The dry run prints `confirm_token`, derived from the platform, the file's sha256 and size, and the metadata (title, description, tags, privacy, the thumbnail's bytes, and each caption file's bytes and language; [ADR-002](docs/decisions.md), [ADR-004](docs/decisions.md)). Only that exact token on the command line goes further, so an approval cannot be reused for a re-exported file, another platform or edited metadata. No environment variable or config file confirms for you. Metadata that breaks an upload limit is refused (`METADATA_INVALID`) before a token is printed.
- **Credentials from the environment or the OS keychain** ([ADR-003](docs/decisions.md)), never from files or flags, and never printed: reports say `present` / `absent`, and every output passes through a redactor. YouTube reads `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET` and `PUBLISH_YOUTUBE_REFRESH_TOKEN`; Vimeo reads `PUBLISH_VIMEO_ACCESS_TOKEN` (a personal access token); TikTok reads `PUBLISH_TIKTOK_ACCESS_TOKEN` (a user access token with the `video.publish` scope); X reads `PUBLISH_X_ACCESS_TOKEN` (an OAuth 2.0 user access token with `media.write`, `tweet.write`, `tweet.read` and `users.read`); LinkedIn reads `PUBLISH_LINKEDIN_ACCESS_TOKEN` (a member access token with `w_member_social`, or `w_organization_social` for a page) and `PUBLISH_LINKEDIN_AUTHOR` (the author's URN, `urn:li:person:<id>` or `urn:li:organization:<id>`). See below.
- **Exactly what was confirmed is sent** ([ADR-004](docs/decisions.md), [ADR-005](docs/decisions.md)). The video resource is the bound title, description, tags and privacy, nothing added; the thumbnail and captions are the bound files. Every file is hashed as it is sent, and the last chunk is held back unless the bytes match the token, so a file edited mid-upload is never published (`CONFIRM_MISMATCH`). Privacy is `private` unless `--privacy` says otherwise. A field a platform's uploader does not send (on Vimeo: tags, thumbnail, captions; on TikTok: description, tags, thumbnail, captions, and `unlisted`; on X and LinkedIn: the same, and `private` too, as their posts are public; LinkedIn takes .mp4 only) is refused (`METADATA_INVALID`) before a token is printed, never silently dropped.
- **Network code lives in the uploader modules only**, `src/publish_skill/platforms/youtube.py`, `src/publish_skill/platforms/vimeo.py`, `src/publish_skill/platforms/tiktok.py`, `src/publish_skill/platforms/x.py` and `src/publish_skill/platforms/linkedin.py` (stdlib `http.client`, verified TLS), and runs only after the token matched and the credentials were read. The access token, the upload session URI, Vimeo's upload link, TikTok's upload URL and LinkedIn's signed upload URLs are redacted like the credentials. The only process the package starts is ffmpeg-skill (below), from an argv list, never a shell.

## `publish upload` to YouTube ([ADR-004](docs/decisions.md))

1. The refresh token is exchanged for an access token at `https://oauth2.googleapis.com/token`. If the token response lists its scopes and none that an upload step accepts is granted (for example `youtube.upload` for the video and thumbnail, `youtube.force-ssl` for captions), the upload stops before any byte is sent (`AUTH_FAILED`).
2. `videos.insert`: a resumable session (`POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=...`), then the file in 8 MiB chunks (`Content-Range`, 308 + `Range` until the last).
3. An interrupted chunk (no answer, a reset, a 60 s timeout, 5xx, 429 or a rate-limit reason) waits with exponential backoff (`random × 2^n` s, at most 64 s, or `Retry-After` up to 5 min), asks the session how far it got (`Content-Range: bytes */TOTAL`) and resumes from the next byte; at most 10 retries per request. An expired session (404) starts again from the beginning, at most twice.
4. `thumbnails.set` and `captions.insert` (one per caption file, named by its language) use the same protocol, with the new video's id.
5. The receipt (`uploaded: true`, `result.video_id`, `url`, `privacy`, `sent`, `transfer`) passes through the redactor. YouTube processes the video afterwards.

| Error | Exit | Retryable | Meaning |
|---|---|---|---|
| `AUTH_FAILED` | 3 | no | the refresh token was refused (`invalid_grant`...) or lacks a scope; nothing uploaded |
| `QUOTA_EXCEEDED` | 4 | yes, after the quota resets | `quotaExceeded`; nothing uploaded |
| `UPLOAD_LIMIT_EXCEEDED` | 4 | yes, later | the channel's daily upload limit (`uploadLimitExceeded`); nothing uploaded |
| `UPLOAD_INTERRUPTED` | 4 | yes | still failing after every retry; nothing published (an unfinished resumable upload creates no video) |
| `UPLOAD_REJECTED` | 4 | no | another 4xx, or a protocol error; nothing published |
| `UPLOAD_PARTIAL` | 4 | no | the video is uploaded (`details.video_id`, `url`), its thumbnail or captions are not; finish in YouTube Studio, do not run `upload` again |

An interrupted run is not resumed by a later run (each run starts a new session); no proxy is used (direct HTTPS only).

## `publish upload` to Vimeo ([ADR-005](docs/decisions.md))

Protocol read from Vimeo's developer documentation on 2026-10-31: [Working with Video Uploads](https://developer.vimeo.com/api/upload/videos) (the resumable "tus" approach), [Getting Started: The Basics](https://developer.vimeo.com/api/guides/start) (personal access tokens), the [API reference](https://developer.vimeo.com/api/reference/videos#upload_video) (API 3.4, reference 3.4.9) and the [tus 1.0.0 protocol](https://tus.io/protocols/resumable-upload).

1. `POST https://api.vimeo.com/me/videos` with `Authorization: bearer <PUBLISH_VIMEO_ACCESS_TOKEN>`, `Accept: application/vnd.vimeo.*+json;version=3.4` and the body `{"upload": {"approach": "tus", "size": "<bytes>"}, "name", "description", "privacy": {"view"}}`: exactly the bound title, description and privacy (`private` -> `nobody`, `unlisted` -> `unlisted`, `public` -> `anybody`). The answer carries `upload.upload_link`, which must be https on a vimeo.com host.
2. `PATCH <upload_link>` in 128 MiB chunks (the guide recommends 128-512 MB) with `Tus-Resumable: 1.0.0`, `Upload-Offset` and `Content-Type: application/offset+octet-stream`; `204` and the new `Upload-Offset` after each. These requests carry no `Authorization` header: the token goes to `api.vimeo.com` only.
3. An interrupted chunk (no answer, a reset, a 60 s timeout, 5xx, 429) or a `409` (offset mismatch) waits with exponential backoff (`random × 2^n` s, at most 64 s, or `Retry-After` up to 5 min), asks `HEAD <upload_link>` for `Upload-Offset` and resumes from it; at most 10 retries per request. The create `POST` is retried on 429/5xx answers only: without an answer a placeholder may already exist, and a second `POST` would add another.
4. The receipt (`uploaded: true`, `result.video_id`, `uri`, `url`, `privacy`, `privacy_view`, `sent`, `transfer`) passes through the redactor. Vimeo transcodes the video afterwards.

| Error | Exit | Retryable | Meaning |
|---|---|---|---|
| `AUTH_FAILED` | 3 | no | 401: the access token was refused (for example error 8003); nothing uploaded |
| `UPLOAD_LIMIT_EXCEEDED` | 4 | yes, after it resets | error 4102 (weekly quota) or 4104 (daily quota); nothing uploaded |
| `UPLOAD_REJECTED` | 4 | no | error 4101 (storage full), another 4xx, or a protocol error |
| `UPLOAD_INTERRUPTED` | 4 | yes | still failing after every retry, no answer to the create request, or the upload link expired (404/410/403); the video was not completed, and Vimeo keeps an empty placeholder (`details.video_uri`, privacy as sent) |

Tags, thumbnails and text tracks are not sent to Vimeo in this release: `plan` FAILs them and `upload` refuses them before a token is printed. ffmpeg-skill has no Vimeo delivery spec, so `plan --platform vimeo` reports the delivery spec and the duration as `UNCHECKED` (verdict `INCOMPLETE`).

## `publish upload` to TikTok ([ADR-006](docs/decisions.md))

Protocol read from TikTok's developer documentation on 2026-10-31: [Content Posting API: Get Started](https://developers.tiktok.com/doc/content-posting-api-get-started) (Direct Post), [Query Creator Info](https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info), [Direct Post reference](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post), [Media Transfer Guide](https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide), [Get Post Status](https://developers.tiktok.com/doc/content-posting-api-reference-get-video-status) and [Manage User Access Tokens](https://developers.tiktok.com/doc/oauth-user-access-token-management).

1. `POST https://open.tiktokapis.com/v2/post/publish/creator_info/query/` with `Authorization: Bearer <PUBLISH_TIKTOK_ACCESS_TOKEN>`: the creator's `privacy_level_options` must offer the bound level (`private` -> `SELF_ONLY`, `public` -> `PUBLIC_TO_EVERYONE`), or the upload stops before any byte is sent.
2. `POST /v2/post/publish/video/init/` with `{"post_info": {"title", "privacy_level"}, "source_info": {"source": "FILE_UPLOAD", "video_size", "chunk_size", "total_chunk_count"}}`: exactly the bound title (TikTok's caption) and privacy level, nothing else. The answer carries `publish_id` and `upload_url`, which must be https on a `tiktokapis.com` host.
3. `PUT <upload_url>` chunk by chunk with `Content-Type` (`video/mp4`, `video/quicktime` or `video/webm`, by the extension) and `Content-Range: bytes FIRST-LAST/TOTAL`; `206` after each, `201` after the last. Chunks are 64 000 000 bytes (the guide: 5-64 MB; the last takes the trailing bytes, below 128 MB); a file of at most that size is sent whole. These requests carry no `Authorization` header.
4. An interrupted chunk (no answer, a reset, a 60 s timeout, 5xx, 429) waits with exponential backoff (`random × 2^n` s, at most 64 s, or `Retry-After` up to 5 min) and is sent again; a `416` (the chunk's range is not where TikTok is) asks `POST /v2/post/publish/status/fetch/` for `uploaded_bytes` and resumes from there. At most 10 retries per request. The init `POST` is retried on 429/5xx answers only.
5. `POST /v2/post/publish/status/fetch/` every 6 s, at most 20 times, until `PUBLISH_COMPLETE` or `FAILED`. The receipt (`uploaded: true`, `result.publish_id`, `status`, `post_ids`, `url`, `privacy`, `privacy_level`, `creator`, `sent`, `transfer`) passes through the redactor. A post still processing when the polls run out is a receipt with a warning, not an error.

| Error | Exit | Retryable | Meaning |
|---|---|---|---|
| `AUTH_FAILED` | 3 | no | `access_token_invalid` (expired: access tokens last 24 hours) or `scope_not_authorized` (no `video.publish`); nothing uploaded |
| `UPLOAD_LIMIT_EXCEEDED` | 4 | yes, later | `spam_risk_too_many_posts`: the creator's daily post cap; nothing posted |
| `QUOTA_EXCEEDED` | 4 | yes, later | `reached_active_user_cap`: the app's daily quota of publishing users; nothing uploaded |
| `UPLOAD_REJECTED` | 4 | no | the creator does not offer the privacy level, `unaudited_client_can_only_post_to_private_accounts`, another 4xx, a `FAILED` post (`fail_reason`, e.g. `duration_check_failed`), or a protocol error |
| `UPLOAD_INTERRUPTED` | 4 | yes | still failing after every retry, no answer to the init request, the upload URL expired (403/404), or a `FAILED` post with `fail_reason: internal`; nothing posted |

A description, tags, a thumbnail, captions and `--privacy unlisted` (TikTok has no link-only level) are refused before a token is printed, and so is a file that is not `.mp4`, `.mov` or `.webm` (`plan` FAILs it). The duet, stitch and comment switches, the cover frame and the branded-content and AI-generated labels are not sent (TikTok's and the creator's own settings apply). An app TikTok has not audited can post only privately: "All content posted by unaudited clients will be restricted to private viewing mode." TikTok may moderate a public post before it is visible.

## `publish upload` to X ([ADR-007](docs/decisions.md))

Protocol read from X's developer documentation on 2026-10-31: [Chunked media upload](https://docs.x.com/x-api/media/quickstart/media-upload-chunked), [Initialize](https://docs.x.com/x-api/media/initialize-media-upload), [Append](https://docs.x.com/x-api/media/append-media-upload), [Finalize](https://docs.x.com/x-api/media/finalize-media-upload), [Get upload status](https://docs.x.com/x-api/media/get-media-upload-status), [Create Post](https://docs.x.com/x-api/posts/create-post), [Rate limits](https://docs.x.com/x-api/fundamentals/rate-limits), [Counting characters](https://docs.x.com/fundamentals/counting-characters) and [OAuth 2.0 authorization code](https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code).

1. `POST https://api.x.com/2/media/upload/initialize` with `Authorization: Bearer <PUBLISH_X_ACCESS_TOKEN>` and `{"media_type", "total_bytes", "media_category": "tweet_video"}` (`video/mp4`, `video/quicktime`, `video/webm` or `video/mp2t`, by the extension); the answer's `data.id` is the media id.
2. `POST /2/media/upload/{id}/append`, `multipart/form-data` with `segment_index` and `media`, 4 MiB per segment (the guide: "at or below 5 MB"), in order.
3. `POST /2/media/upload/{id}/finalize`: `data.size` must be the file's size. While `processing_info.state` is `pending` or `in_progress`, `GET /2/media/upload?command=STATUS&media_id={id}` after `check_after_secs` (at most 30 s), at most 60 times.
4. `POST /2/tweets` with `{"text": <the bound title>, "media": {"media_ids": [id]}}` (no `text` when no title is bound): one post, nothing else. The receipt (`uploaded: true`, `result.post_id`, `media_id`, `privacy`, `sent`, `transfer`) passes through the redactor; X's API documentation states no URL for a post, so `url` is `null`.
5. An interrupted segment (no answer, a reset, a 60 s timeout, 5xx, 429) waits with exponential backoff (`random × 2^n` s, at most 64 s; `Retry-After`, or on a 429 `x-rate-limit-reset`, up to 5 min) and is sent again with the same `segment_index`; finalize's size then shows the media is whole. Initialize and finalize are retried on 429/5xx answers only; the create-post request on 429 only, so a post is never made twice.

| Error | Exit | Retryable | Meaning |
|---|---|---|---|
| `AUTH_FAILED` | 3 | no | 401 (an expired token: access tokens last two hours) or a 403 naming a missing scope; nothing posted |
| `UPLOAD_REJECTED` | 4 | no | another 4xx (e.g. 403 "not allowed to post a video longer than 20 minutes"), failed processing, a size mismatch at finalize, or a protocol error; nothing posted |
| `UPLOAD_INTERRUPTED` | 4 | yes | still failing after every retry, no answer to initialize or finalize, or still processing after the last status query; nothing posted |
| `UPLOAD_INTERRUPTED` | 4 | **no** | the create-post request got no answer or a 5xx: the media is uploaded, and whether the post exists is unknown; check the profile before running `upload` again |

X has no private or unlisted post, so `--privacy public` must be given (`plan` FAILs the default `private`). A description, tags, a thumbnail and captions are refused before a token is printed, and so is a file that is not `.mp4`, `.mov`, `.webm` or `.ts`. Reply settings, polls, `for_super_followers_only`, `nullcast` and the AI or paid-partnership labels are not sent. The access token is not refreshed (`offline.access` is not used).

## `publish upload` to LinkedIn ([ADR-008](docs/decisions.md))

Protocol read from LinkedIn's documentation (API version 202610) on 2026-10-31: [Videos API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api), [Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api), [Post API schema](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/post-api-schema), [`little` text format](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format) and [Authorization Code Flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow). Every API request carries `Authorization: Bearer <PUBLISH_LINKEDIN_ACCESS_TOKEN>`, `Linkedin-Version: 202610` and `X-Restli-Protocol-Version: 2.0.0`.

1. `POST https://api.linkedin.com/rest/videos?action=initializeUpload` with `{"initializeUploadRequest": {"owner": <PUBLISH_LINKEDIN_AUTHOR>, "fileSizeBytes", "uploadCaptions": false, "uploadThumbnail": false}}`; the answer's `value` has the video URN, `uploadToken` and `uploadInstructions` (each `firstByte`, `lastByte`, `uploadUrl`), which must cover the file in order. Each upload URL must be https on a linkedin.com host; it is redacted from every output.
2. `PUT` each byte range to its `uploadUrl` (`application/octet-stream`, no token: the URL is signed); the answer's `ETag` is the part's id.
3. `POST /rest/videos?action=finalizeUpload` with `{"finalizeUploadRequest": {"video", "uploadToken", "uploadedPartIds"}}`, the ETags in order.
4. `GET /rest/videos/{urn}` until `status` is `AVAILABLE` (every 5 s, at most 120 times; the interval is not documented). `PROCESSING_FAILED` stops with `processingFailureReason`.
5. `POST /rest/posts` with `{"author", "commentary": <the bound title, little-escaped>, "visibility": "PUBLIC", "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []}, "content": {"media": {"id": <video URN>}}, "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": false}`: one post, nothing else. 201; the receipt's `result.post_id` is `x-restli-id`. `url` is `https://www.linkedin.com/feed/update/<urn>/` for a `ugcPost` URN (the only form the documentation gives) and `null` otherwise.
6. An interrupted part (no answer, a reset, a 60 s timeout, 5xx, 429) waits with exponential backoff (`random × 2^n` s, at most 64 s; `Retry-After` up to 5 min) and is `PUT` again; finalize then names its newer ETag. Initialize and finalize are retried on 429/5xx answers only; the create-post request on 429 only, so a post is never made twice.

| Error | Exit | Retryable | Meaning |
|---|---|---|---|
| `AUTH_FAILED` | 3 | no | 401 (an expired or invalid token: tokens last 60 days) or 403 (a missing permission, or no page role for an organization author); nothing posted |
| `UPLOAD_REJECTED` | 4 | no | another 4xx, `PROCESSING_FAILED`, a part without an ETag, an upload URL off linkedin.com, or a protocol error; nothing posted |
| `UPLOAD_INTERRUPTED` | 4 | yes | still failing after every retry, no answer to initialize or finalize, or still processing after the last status query; nothing posted |
| `UPLOAD_INTERRUPTED` | 4 | **no** | the create-post request got no answer or a 5xx: the video is uploaded, and whether the post exists is unknown; check the author's page before running `upload` again |

The commentary is the title as typed: every `little` reserved character (`| { } @ [ ] ( ) < > # \ * _ ~`) is escaped with a backslash, so a `#word` is text, not a hashtag, and no mention is made. LinkedIn's other visibilities, CONNECTIONS and LOGGED_IN, are neither private nor unlisted, so `--privacy public` must be given (`plan` FAILs the default `private`). A description, tags, a thumbnail and captions are refused before a token is printed, and so is a file that is not `.mp4`. A media title, alt text, calls to action, targeting and sponsored-content fields are not sent. The author's URN is not looked up (it is stored with the token), and the access token is not refreshed.

## Credentials ([ADR-003](docs/decisions.md))

- **Two sources, the environment first.** If any of a platform's variables is set in the environment, the environment is that platform's only source and the keychain is not consulted; otherwise the OS keychain is. One platform's values never mix sources (a refresh token belongs to the OAuth client that issued it).
- **The OS keychain** is the macOS Keychain, the Windows Credential Manager or the Secret Service on Linux, reached through [keyring](https://pypi.org/project/keyring/) (MIT), an optional extra: `pip install 'publish-skill[keychain]'` installs `keyring>=25.0`. Without it, credentials come from the environment only and `doctor` says so. Only those three OS keychains are used: keyring's own backend choice (`PYTHON_KEYRING_BACKEND`, plug-in or file backends) is ignored. `PUBLISH_KEYCHAIN=off` turns the keychain off (headless machines, CI).
- **Items** are stored under the service `publish-skill`, with the variable's name as the account.
- **`publish auth login --platform youtube`** (or `--platform vimeo` / `--platform tiktok` / `--platform x`: one value, the personal or user access token; `--platform linkedin`: two, the access token and the author's URN) reads the values from stdin, one per line in the order of the platform's variables (or of `--variable NAME`, repeatable), or from a prompt that does not echo when stdin is a terminal. No flag takes a value, so none reaches shell history. Every value is read before the first is stored. `login` stores values you already have; it does not run Google's OAuth consent screen or Vimeo's authorization flow (a Vimeo personal access token is generated on the app's page at https://developer.vimeo.com/apps, with the `upload` scope; a TikTok user access token comes from TikTok's Login Kit authorization with the `video.publish` scope, lasts 24 hours, and is not refreshed by this skill; an X OAuth 2.0 user access token comes from X's authorization code flow with PKCE, lasts two hours, and is not refreshed either; a LinkedIn member access token comes from LinkedIn's Authorization Code Flow with `w_member_social` or `w_organization_social`, lasts 60 days, and is not refreshed).
- **`publish auth status`** reports, per platform, the chosen `source`, whether each variable is `present` / `absent` in the environment and in the keychain, and the keychain's availability. **`publish auth logout`** removes the items.
- Refusals (unknown platform or variable, keychain unavailable, a wrong `--confirm`) all come before any credential is read. `KEYCHAIN_UNAVAILABLE` and `KEYCHAIN_ERROR` exit 3, like `CREDENTIALS_MISSING`.

## `publish plan` ([ADR-002](docs/decisions.md))

`plan` checks the deliverable and its metadata against the platform's limits, offline, and prints one row per limit: `PASS`, `WARN`, `FAIL` (with a `fix`) or `UNCHECKED` (with the reason). The verdict is `FAIL` if any row fails, `INCOMPLETE` if any row could not be checked, otherwise `WARN` or `PASS`; it exits 1 unless the verdict is `PASS` or `WARN`. Nothing is uploaded and no credential is read.

- Rows with owner `publish` come from the upload-limit table below, and each row carries its limit's `source` and `accessed` date.
- Rows with owner `ffmpeg-skill` are the output of ffmpeg-skill `check --platform youtube --json --no-loudness` (codec, frame size, aspect, frame rate, colour...), passed through unchanged. ffmpeg-skill owns those specs; this skill never copies them.
- ffmpeg-skill is found as `doctor` finds it (`--ffmpeg-skill`, `PUBLISH_FFMPEG_SKILL_DIR`, ...) and used only when its contract (`scripts/_contract.py --json --static`) declares `export --list`, `check --platform/--json/--no-loudness` and `execution.network: false`. Without a usable checkout, its rows and the duration row are `UNCHECKED` and the verdict is `INCOMPLETE`, never `PASS`.

### Upload limits: YouTube

Read from YouTube's own documentation on 2026-10-31 (`publish contract --json` → `upload_limits` has every value with the quoted sentence). A size written as "GB" or "MB" is read as decimal, the smaller reading. A limit the documentation does not state is `unknown` and nothing is checked against it.

| Key | Limit | plan | Source |
|---|---|---|---|
| `video.max_bytes` | 256 GB (256 000 000 000 bytes) | FAIL above | https://developers.google.com/youtube/v3/docs/videos/insert |
| `video.max_duration_s` | 12 hours (43 200 s) | FAIL above | https://support.google.com/youtube/answer/71673?hl=en |
| `video.unverified_max_duration_s` | 15 minutes unless the account is verified | WARN above | https://support.google.com/youtube/answer/71673?hl=en |
| `video.mime_types` | `video/*`, `application/octet-stream` | reported | https://developers.google.com/youtube/v3/docs/videos/insert |
| `video.file_formats` | .MOV .MPEG-1 .MPEG-2 .MPEG4 .MP4 .MPG .AVI .WMV .MPEGPS .FLV 3GPP WebM (plus the codecs DNxHR, ProRes, CineForm, HEVC) | WARN on another extension | https://support.google.com/youtube/troubleshooter/2888402?hl=en |
| `video.codecs`, `video.resolution`, `video.aspect`, `video.fps` | delegated: ffmpeg-skill `check --platform youtube` | ffmpeg-skill's rows | ffmpeg-skill `scripts/_platforms.py` |
| `title.max_chars` | 100 characters | FAIL above | https://developers.google.com/youtube/v3/docs/videos |
| `title.forbidden_chars` | `<` and `>` | FAIL | https://developers.google.com/youtube/v3/docs/videos |
| `description.max_bytes` | 5000 bytes (UTF-8) | FAIL above | https://developers.google.com/youtube/v3/docs/videos |
| `description.forbidden_chars` | `<` and `>` | FAIL | https://developers.google.com/youtube/v3/docs/videos |
| `tags.max_chars` | 500 characters for the list; commas count, a tag with a space counts its quotation marks | FAIL above | https://developers.google.com/youtube/v3/docs/videos |
| `tags.max_chars_per_tag`, `tags.max_count` | unknown (not stated) | not checked | searched: the two API pages above |
| `privacy.values` | `private`, `public`, `unlisted` | FAIL otherwise | https://developers.google.com/youtube/v3/docs/videos |
| `thumbnail.max_bytes` | 50 MB | FAIL above | https://developers.google.com/youtube/v3/docs/thumbnails/set |
| `thumbnail.types` | JPEG, PNG (by the file's first bytes) | FAIL otherwise | https://developers.google.com/youtube/v3/docs/thumbnails/set |
| `thumbnail.min_width_px` | 640 px (a recommendation) | not checked | https://support.google.com/youtube/answer/72431?hl=en |
| `captions.max_bytes` | 100 MB | FAIL above | https://developers.google.com/youtube/v3/docs/captions/insert |
| `captions.extensions` | .srt .sbv .sub .mpsub .lrc .cap .smi .sami .rt .vtt .ttml .dfxp .scc .stl .tds .cin .asc | WARN otherwise | https://support.google.com/youtube/answer/2734698?hl=en |
| `captions.plain_utf8_extensions` | .srt, .sbv and .sub must be plain UTF-8 | FAIL otherwise | https://support.google.com/youtube/answer/2734698?hl=en |
| `captions.language` | a BCP-47 language tag, required by captions.insert (`--caption-language`) | FAIL when missing or not tag-shaped | https://developers.google.com/youtube/v3/docs/captions |
| `quota.videos_insert` | 100 calls per day, 1 unit each in the Video Uploads bucket | reported | https://developers.google.com/youtube/v3/docs/videos/insert |
| `quota.thumbnails_set_units` | about 50 units per call | reported | https://developers.google.com/youtube/v3/docs/thumbnails/set |
| `quota.captions_insert_units` | 400 units per call | reported | https://developers.google.com/youtube/v3/docs/captions/insert |
| `uploads.per_user` | unknown (`uploadLimitExceeded` exists, no number is stated) | not checked | searched: https://developers.google.com/youtube/v3/docs/videos/insert |

### Upload limits: Vimeo

Read from Vimeo's developer documentation on 2026-10-31 (API 3.4; `publish contract --json` → `upload_limits.platforms.vimeo` has every value with the quoted sentence). A limit the documentation does not state is `unknown` and nothing is checked against it.

| Key | Limit | plan | Source |
|---|---|---|---|
| `video.max_bytes` | 322 122 547 200 bytes ("300 GB", stated in bytes) | FAIL above | https://developer.vimeo.com/api/reference/videos#upload_video |
| `video.max_duration_s` | 24 hours (86 400 s) | FAIL above (needs ffmpeg-skill's measured duration) | https://developer.vimeo.com/api/upload/videos |
| `video.file_formats` | unknown ("a wide range of video codecs and file types") | not checked | searched: https://developer.vimeo.com/api/upload/videos, https://developer.vimeo.com/api/guides/videos/upload, the reference |
| `video.codecs`, `video.resolution`, `video.aspect`, `video.fps` | delegated: ffmpeg-skill, which has no `vimeo` spec today | `UNCHECKED` | ffmpeg-skill `scripts/_platforms.py` |
| `upload.content_type` | `application/offset+octet-stream` (tus PATCH) | protocol, not a plan row | https://developer.vimeo.com/api/upload/videos |
| `upload.chunk_bytes` | 128-512 MB recommended (this uploader: 128 MiB) | protocol, not a plan row | https://developer.vimeo.com/api/upload/videos |
| `title.max_chars` | 128 characters | FAIL above | https://developer.vimeo.com/api/reference/videos#edit_video |
| `title.forbidden_chars`, `description.forbidden_chars` | unknown (not stated) | not checked | searched: the upload and edit reference |
| `description.max_chars` | 5000 characters | FAIL above | https://developer.vimeo.com/api/reference/videos#edit_video |
| `privacy.values` | `private` (`nobody`), `unlisted` (`unlisted`), `public` (`anybody`) | FAIL otherwise | https://developer.vimeo.com/api/reference/videos#upload_video |
| `storage.max_bytes`, `quota.weekly_uploads`, `quota.daily_uploads` | unknown, per account (errors 4101, 4102, 4104 exist; no number is stated) | not checked; typed errors at upload | searched: https://developer.vimeo.com/api/reference/videos#upload_video |
| `rate_limit.requests_per_minute` | 25 (Free), 125 (Starter), 250 (Standard), 750 (Advanced), 1500 (Studio), 2500 (Enterprise); 429 and error 9000 above | reported | https://developer.vimeo.com/guidelines/rate-limiting |
| `upload.access` | the API app needs upload access (Vimeo Free: approval by support ticket) | reported | https://developer.vimeo.com/api/guides/videos/upload |
| `token.scopes` | `upload` and `edit` | reported | https://developer.vimeo.com/api/upload/videos |

### Upload limits: TikTok

Read from TikTok's developer documentation on 2026-10-31 (`publish contract --json` → `upload_limits.platforms.tiktok` has every value with the quoted sentence). A size written as "GB" or "MB" is read as decimal, the smaller reading. A limit the documentation does not state is `unknown` and nothing is checked against it.

| Key | Limit | plan | Source |
|---|---|---|---|
| `video.max_bytes` | 4 GB (4 000 000 000 bytes) | FAIL above | https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide |
| `video.max_duration_s` | 10 minutes (600 s) through the API | FAIL above (needs ffmpeg-skill's measured duration) | https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide |
| `video.creator_max_duration_s` | unknown offline: per creator (3, 5 or 10 minutes), `max_video_post_duration_sec` in creator info | not checked; reported in the receipt | searched: the transfer guide, https://developers.tiktok.com/doc/content-posting-api-reference-query-creator-info |
| `video.file_formats` | .mp4 .webm .mov | FAIL otherwise (the upload's `Content-Type` must name one) | https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide |
| `video.codecs`, `video.resolution`, `video.aspect`, `video.fps` | delegated: ffmpeg-skill `check --platform tiktok` | ffmpeg-skill's rows | ffmpeg-skill `scripts/_platforms.py` |
| `upload.chunk_bytes` | 5-64 MB per chunk, the last up to 128 MB, a file under 5 MB whole (this uploader: 64 000 000 bytes) | protocol, not a plan row | https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide |
| `upload.max_chunks` | 1000 | protocol, not a plan row | https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide |
| `upload.url_validity_s` | 1 hour | protocol, not a plan row | https://developers.tiktok.com/doc/content-posting-api-reference-direct-post |
| `title.max_utf16_units` | 2200 UTF-16 code units (the caption) | FAIL above | https://developers.tiktok.com/doc/content-posting-api-reference-direct-post |
| `title.forbidden_chars` | unknown (not stated) | not checked | searched: the Direct Post reference |
| `privacy.values` | `private` (`SELF_ONLY`), `public` (`PUBLIC_TO_EVERYONE`) | FAIL otherwise (`unlisted` included) | https://developers.tiktok.com/doc/content-posting-api-reference-direct-post |
| `quota.daily_posts`, `quota.active_users` | unknown (`spam_risk_too_many_posts`, `reached_active_user_cap` exist; no number is stated) | not checked; typed errors at upload | searched: the Direct Post reference, Get Started |
| `rate_limit.requests_per_minute` | 6 (init), 20 (creator info), 30 (status) per user access token | reported | https://developers.tiktok.com/doc/content-posting-api-reference-direct-post |
| `upload.audit` | an unaudited app's posts are private | reported | https://developers.tiktok.com/doc/content-posting-api-get-started |
| `token.scopes` | `video.publish` | reported | https://developers.tiktok.com/doc/content-posting-api-reference-direct-post |
| `token.lifetime_s` | 24 hours (the access token) | reported | https://developers.tiktok.com/doc/oauth-user-access-token-management |

### Upload limits: X

Read from X's developer documentation on 2026-10-31 (`publish contract --json` → `upload_limits.platforms.x` has every value with the quoted sentence). The values for an account without Premium are checked (Premium allows 125 minutes and 16 GB; `plan` cannot tell). A limit the documentation does not state is `unknown` and nothing is checked against it.

| Key | Limit | plan | Source |
|---|---|---|---|
| `video.max_bytes` | 8 GB (8 000 000 000 bytes), `tweet_video` | FAIL above | https://docs.x.com/x-api/media/quickstart/media-upload-chunked |
| `video.max_duration_s` | 20 minutes (1200 s) | FAIL above (needs ffmpeg-skill's measured duration) | https://docs.x.com/x-api/media/quickstart/media-upload-chunked |
| `video.file_formats` | .mp4 .mov .webm .ts | FAIL otherwise (initialize's `media_type` must name one) | https://docs.x.com/x-api/media/initialize-media-upload |
| `video.codecs`, `video.resolution`, `video.aspect`, `video.fps` | delegated: ffmpeg-skill `check --platform x` | ffmpeg-skill's rows | ffmpeg-skill `scripts/_platforms.py` |
| `upload.chunk_bytes` | at or below 5 MB per segment (this uploader: 4 MiB) | protocol, not a plan row | https://docs.x.com/x-api/media/quickstart/media-upload-chunked |
| `upload.max_segments` | 10000 (`segment_index` 0-9999) | protocol, not a plan row | https://docs.x.com/x-api/media/append-media-upload |
| `upload.session_validity_s` | 86400 s | protocol, not a plan row | https://docs.x.com/x-api/media/quickstart/media-upload-chunked |
| `title.max_weighted` | 280 weighted characters (CJK and emoji 2, a URL 23; twitter-text v3 ranges) | FAIL above when exact; with URLs or emoji an upper bound, WARN above | https://docs.x.com/fundamentals/counting-characters |
| `title.forbidden_chars` | unknown (not stated) | not checked | searched: Create Post, Counting characters |
| `privacy.values` | `public` only | FAIL otherwise (the default `private` included) | https://docs.x.com/x-api/posts/create-post |
| `rate_limit.requests` | 100 posts / 15 min per user; 1,875 upload requests / 15 min per user | reported | https://docs.x.com/x-api/fundamentals/rate-limits |
| `quota.daily_posts` | unknown (not stated beyond the rate limits) | not checked | searched: Create Post, Rate limits |
| `token.scopes` | `media.write`, `tweet.write`, `tweet.read`, `users.read` | reported | https://docs.x.com/x-api/posts/create-post |
| `token.lifetime_s` | two hours (the access token) | reported | https://docs.x.com/fundamentals/authentication/oauth-2-0/authorization-code |

### Upload limits: LinkedIn

Read from LinkedIn's documentation on 2026-10-31 (`publish contract --json` → `upload_limits.platforms.linkedin` has every value with the quoted sentence). A limit the documentation does not state is `unknown` and nothing is checked against it.

| Key | Limit | plan | Source |
|---|---|---|---|
| `video.min_bytes` | 75 KB (read as 76 800 bytes, the larger reading) | FAIL below | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api |
| `video.max_bytes` | 500 MB (500 000 000 bytes); the API caps `fileSizeBytes` at 5 GB | FAIL above | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api |
| `video.min_duration_s` | 3 s | FAIL below (needs ffmpeg-skill's measured duration) | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api |
| `video.max_duration_s` | 30 minutes (1800 s) | FAIL above (needs ffmpeg-skill's measured duration) | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api |
| `video.file_formats` | .mp4 | FAIL otherwise | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api |
| `video.codecs`, `video.resolution`, `video.aspect`, `video.fps` | delegated: ffmpeg-skill `check --platform linkedin` | ffmpeg-skill's rows | ffmpeg-skill `scripts/_platforms.py` |
| `upload.chunk_bytes` | the byte ranges LinkedIn gives (4 MiB) | protocol, not a plan row | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api |
| `upload.session_validity_s` | typically 30 days (the upload URLs) | protocol, not a plan row | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api |
| `title.max_chars` | unknown: the Posts API states no `commentary` maximum (3000 is stated for the older UGC Post API only) | not checked | searched: Post API schema, Posts API, https://learn.microsoft.com/en-us/linkedin/compliance/integrations/shares/ugc-post-api |
| `title.forbidden_chars` | none (reserved `little` characters are escaped) | not checked | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/little-text-format |
| `privacy.values` | `public` only (sent as `PUBLIC`) | FAIL otherwise (the default `private` included) | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/post-api-schema |
| `rate_limit.requests` | unknown (not stated; 429 is named) | not checked | searched: Videos API, Posts API |
| `quota.daily_posts` | unknown (not stated) | not checked | searched: Posts API |
| `token.scopes` | `w_member_social` (a person) or `w_organization_social` (an organization, with a page role) | reported | https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api |
| `token.lifetime_s` | 60 days (the access token) | reported | https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow |

## JSON and errors

With `--json`, stdout holds exactly one JSON document. Failures are `{"ok": false, "error": {"code", "message", "retryable", "details"}}`; the code fixes the exit code (`contract --json` lists them under `errors`).

## Tests

```bash
python -m unittest discover -s tests -v
```

Every unit test runs with network access patched to fail, except to `127.0.0.1` on the port of a mock server the test itself started: the YouTube uploader's tests run against `tests/mock_youtube.py`, an in-process mock of the token endpoint and the resumable protocol (chunks, an interrupted chunk and its resume, timeouts, 5xx/429 backoff, quota and upload-limit errors, expired sessions, thumbnail, captions), and the Vimeo uploader's against `tests/mock_vimeo.py`, an in-process mock of `POST /me/videos` and tus 1.0.0 (PATCH/HEAD, 409 offset conflicts, an interrupted chunk and its resume, timeouts, 5xx/429 backoff, the 4101/4102/4104 account errors, an expired upload link), and the TikTok uploader's against `tests/mock_tiktok.py`, an in-process mock of creator info, the Direct Post init and the chunked PUT (206/201, a resent chunk, a lost answer resolved by 416 and `uploaded_bytes`, timeouts, 5xx/429 backoff, the post cap and the app quota, an expired upload URL, the post's status), and the X uploader's against `tests/mock_x.py`, an in-process mock of the chunked media upload and create-post endpoints (segments, a resent segment after a lost answer or a drop, timeouts, 5xx/429 backoff with `x-rate-limit-reset`, processing status, failed processing, a post never sent twice), and the LinkedIn uploader's against `tests/mock_linkedin.py`, an in-process mock of the Videos API and the Posts API (signed part uploads and their ETags, a resent part after a lost answer or a drop, timeouts, 5xx/429 backoff, processing status, failed processing, a post never sent twice). No test contacts Google, Vimeo, TikTok, X or LinkedIn, and no workflow performs a real upload; CI fails unless `test_the_guard_allows_only_the_mock_this_test_started`, `test_the_guard_blocks_every_non_loopback_host_for_vimeo`, `test_the_guard_blocks_every_non_loopback_host_for_tiktok`, `test_the_guard_blocks_every_non_loopback_host_for_x`, `test_the_guard_blocks_every_non_loopback_host_for_linkedin` and the resume and quota (or never-twice) tests of the five uploaders passed. `plan`'s tests run fake ffmpeg-skill checkouts. No test reaches a real OS keychain: the package's only import of keyring is replaced by a guard for the whole test run, every test gets an in-memory fake keychain, and CI fails unless `test_the_fake_keychain_is_what_runs` passed. The OS registry conformance checks and the JSON Schema validation of the contract run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present and `jsonschema` is installed; the integration tests against a real ffmpeg-skill run when `PUBLISH_FFMPEG_SKILL_DIR` (or `../ffmpeg-skill`), FFmpeg and Python >= 3.10 are present. CI's `conformance` job provides all of them and fails if any test is skipped.

## Licence

MIT.

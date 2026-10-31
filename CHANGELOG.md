# Changelog

## 0.1.0 - unreleased

The Instagram Reels uploader (roadmap step 5, the sixth and last further platform; step 5 is done).

- ADR-010: `publish upload --platform reels --confirm <token>` creates a REELS container with `upload_type=resumable` (`POST graph.facebook.com/v26.0/<IG_ID>/media`), uploads the file to `rupload.facebook.com/ig-api-upload/v26.0/<container>` (`Authorization: OAuth`, `offset: 0`, `file_size`, streamed), queries `status_code` once a minute for at most five minutes until `FINISHED`, then makes one `media_publish`. The account id and an access token from Facebook Login for Business (`PUBLISH_INSTAGRAM_USER_ID`, `PUBLISH_INSTAGRAM_ACCESS_TOKEN`), environment or keychain. Standard library only, in `platforms/reels.py`, the seventh module with network code.
- Exactly the bound request: the title as the caption, nothing added. A reel published through the API is public, so `public` is the only privacy (`plan` FAILs the default `private`); a description, tags, a thumbnail, captions files, or a file other than .mp4/.mov are refused before a token is printed.
- No byte-offset query is documented, so an interrupted upload (no answer, 5xx, 429, `debug_info.retriable`) is sent again whole from offset 0 with exponential backoff; the last block is held back unless the file still has the bound sha256. The publish is retried only when throttled: no answer, a 5xx or code 1/2 is `UPLOAD_INTERRUPTED` with `retryable: false`, so a reel is never published twice. `ERROR` and `EXPIRED` are `UPLOAD_REJECTED`.
- The Instagram Reels upload-limit table (cited, 2026-10-31): 300 MB, 3 s to 15 minutes, MOV or MP4, a caption of 2200 characters, 30 hashtags and 20 @ tags (`plan` FAILs the length; the hashtag and @ counts are heuristic and only WARN), 100 API-published posts per 24 hours (reported). `plan` reads ffmpeg-skill's `reels` delivery spec.
- Tested only against an in-process mock of the API on 127.0.0.1; the socket guard lets tests reach nothing else. No real upload has been made.

The Facebook uploader (roadmap step 5, the fifth further platform).

- ADR-009: `publish upload --platform facebook --confirm <token>` uploads the video with the Resumable Upload API (`POST /<APP_ID>/uploads` for a session, `POST /upload:<id>` with `file_offset` and the file streamed in one body, `GET /upload:<id>` for the offset to resume from), then publishes one video on the Page with `POST graph-video.facebook.com/v26.0/<PAGE_ID>/videos` and the file handle. Four values (`PUBLISH_FACEBOOK_APP_ID`, `PUBLISH_FACEBOOK_USER_ACCESS_TOKEN`, `PUBLISH_FACEBOOK_PAGE_ID`, `PUBLISH_FACEBOOK_PAGE_ACCESS_TOKEN`), environment or keychain. Standard library only, in `platforms/facebook.py`, the sixth module with network code.
- Exactly the bound request: title and description when bound, and the privacy as documented flags (`public`: published; `unlisted`: published and `secret`, viewable by permalink only). An unpublished video's audience is not documented, so `private` is not offered (`plan` FAILs the default); tags, a thumbnail, captions, or a file other than .mp4 are refused before a token is printed. The user token goes to the upload only, the Page token to the publish only; the file handle is redacted.
- An interrupted upload asks for `file_offset` and sends the rest, with exponential backoff (`Retry-After`) after no answer, 5xx, 429 or Graph codes 1, 2, 4, 17, 341; the last block is held back unless the file still has the bound sha256. The publish is retried only when throttled: no answer, a 5xx or code 1/2 is `UPLOAD_INTERRUPTED` with `retryable: false`, so a video is never published twice. Codes 190, 102, 10 and 200-299 are `AUTH_FAILED`.
- The Facebook upload-limit table (cited, 2026-10-31): no size, duration, title or description limit is stated on the pages read, so those are `unknown` and nothing is checked against them; .mp4 is enforced (the upload session's `file_type`). `plan` reads ffmpeg-skill's `facebook` delivery spec.
- Tested only against an in-process mock of the API on 127.0.0.1; the socket guard lets tests reach nothing else. No real upload has been made.

The LinkedIn uploader (roadmap step 5, the fourth further platform).

- ADR-008: `publish upload --platform linkedin --confirm <token>` uploads the video with the Videos API (`initializeUpload`, a `PUT` of each byte range LinkedIn gives to its signed upload URL, the ETags to `finalizeUpload`, the video's status until `AVAILABLE`), then makes one post with `POST /rest/posts` (201, the post's URN in `x-restli-id`). An OAuth 2.0 member access token (`PUBLISH_LINKEDIN_ACCESS_TOKEN`, `w_member_social` or `w_organization_social`) and the author's URN (`PUBLISH_LINKEDIN_AUTHOR`, a person or organization), environment or keychain; `Linkedin-Version: 202610`. Standard library only, in `platforms/linkedin.py`, the fifth module with network code.
- Exactly the bound request: the title as the commentary, every `little` reserved character escaped so the text appears as typed, `PUBLIC`, the main feed and the video, nothing added. LinkedIn's CONNECTIONS and LOGGED_IN are neither private nor unlisted, so `public` is the only privacy (`plan` FAILs the default `private`); a description, tags, a thumbnail, captions, or a file other than .mp4 are refused before a token is printed. The token goes to api.linkedin.com only; the signed upload URLs (https on linkedin.com only) get no token and are redacted.
- An interrupted part is `PUT` again with exponential backoff (`Retry-After`); finalize names every part's latest ETag. The post is retried only after a 429: no answer or a 5xx is `UPLOAD_INTERRUPTED` with `retryable: false`, so a post is never made twice. 401 and 403 are `AUTH_FAILED`; `PROCESSING_FAILED` and other 4xx are `UPLOAD_REJECTED`.
- The LinkedIn upload-limit table (cited, 2026-10-31; unknowns marked: the Posts API's commentary maximum is not stated, so nothing is checked against it); `plan` gains minimum rows, used by LinkedIn's 75 KB and 3 s, and reads ffmpeg-skill's `linkedin` delivery spec.
- Tested only against an in-process mock of the API on 127.0.0.1; the socket guard lets tests reach nothing else. No real upload has been made.

The X uploader (roadmap step 5, the third further platform).

- ADR-007: `publish upload --platform x --confirm <token>` uploads the video with the X API v2 chunked media upload (`initialize` with `tweet_video`, `append` in 4 MiB multipart segments, `finalize`, the processing `STATUS`), then makes one post with `POST /2/tweets`. An OAuth 2.0 user access token (`PUBLISH_X_ACCESS_TOKEN`, environment or keychain) with `media.write`, `tweet.write`, `tweet.read` and `users.read`. Standard library only, in `platforms/x.py`, the fourth module with network code.
- Exactly the bound request: the title as the post's text (a media-only post without one) and the media id, nothing added; every request goes to api.x.com only. X has no private or unlisted post, so `public` is the only privacy (`plan` FAILs the default `private`); a description, tags, a thumbnail, captions, or a file other than .mp4/.mov/.webm/.ts are refused before a token is printed.
- An interrupted segment is sent again with the same `segment_index` with exponential backoff (`Retry-After`, or `x-rate-limit-reset` on a 429); finalize's `size` must equal the file's. The post is retried only after a 429: no answer or a 5xx is `UPLOAD_INTERRUPTED` with `retryable: false`, so a post is never made twice. 401 or a 403 naming a scope is `AUTH_FAILED`; failed processing and other 4xx are `UPLOAD_REJECTED`.
- The X upload-limit table (cited, 2026-10-31; unknowns marked); `plan --platform x` reads ffmpeg-skill's `x` delivery spec and counts the text the way X does (twitter-text v3 weights, NFC; exact without URLs and emoji, else an upper bound that only WARNs).
- Tested only against an in-process mock of the API on 127.0.0.1; the socket guard lets tests reach nothing else. No real upload has been made.

The TikTok uploader (roadmap step 5, the second further platform).

- ADR-006: `publish upload --platform tiktok --confirm <token>` posts the video with the Content Posting API's Direct Post: creator info (the bound privacy level must be offered), `video/init` with `FILE_UPLOAD`, the chunked `PUT` (64 000 000-byte chunks, the last taking the trailing bytes, a small file whole; 206/201), then the post's status. A user access token (`PUBLISH_TIKTOK_ACCESS_TOKEN`, environment or keychain). Standard library only, in `platforms/tiktok.py`, the third module with network code.
- Exactly the bound request: the title (TikTok's caption) and `privacy_level` (`private` -> `SELF_ONLY`, the default; `public` -> `PUBLIC_TO_EVERYONE`), nothing added; the token goes to open.tiktokapis.com only, and the upload URL must be https on a tiktokapis.com host and is redacted. A description, tags, a thumbnail, captions, `unlisted`, or a file other than .mp4/.mov/.webm are refused before a token is printed.
- An interrupted chunk is sent again with exponential backoff; a lost answer (416) is resolved with the post's `uploaded_bytes`. `spam_risk_too_many_posts` is `UPLOAD_LIMIT_EXCEEDED`, `reached_active_user_cap` `QUOTA_EXCEEDED`, `access_token_invalid` / `scope_not_authorized` `AUTH_FAILED`, a `FAILED` post `UPLOAD_REJECTED` (or `UPLOAD_INTERRUPTED` for `internal`).
- The TikTok upload-limit table (cited, 2026-10-31; unknowns marked); `plan --platform tiktok` reads ffmpeg-skill's `tiktok` delivery spec, counts the title in UTF-16 code units, and FAILs a container TikTok does not take.
- Tested only against an in-process mock of the API on 127.0.0.1; the socket guard lets tests reach nothing else. No real upload has been made.

The Vimeo uploader (roadmap step 5, the first further platform).

- ADR-005: `publish upload --platform vimeo --confirm <token>` uploads the video with the Vimeo API 3.4 resumable (tus 1.0.0) upload: `POST /me/videos` (approach tus) with a personal access token (`PUBLISH_VIMEO_ACCESS_TOKEN`, environment or keychain), then `PATCH` in 128 MiB chunks, with `HEAD` and a resume after an interrupted chunk or a 409, and exponential backoff on 5xx/429 and timeouts. Standard library only, in `platforms/vimeo.py`, the second (and only other) module with network code.
- Exactly the bound request: the title, description and `privacy.view` (`private` -> `nobody`, the default; `unlisted`; `public` -> `anybody`), nothing added; the token goes to api.vimeo.com only, and the upload link must be https on a vimeo.com host and is redacted. Tags, a thumbnail or captions for Vimeo are refused (`METADATA_INVALID`) before a token is printed (`Uploader.metadata_fields`).
- Vimeo's errors 4102 / 4104 (weekly / daily quota) are `UPLOAD_LIMIT_EXCEEDED`, 4101 (storage full) `UPLOAD_REJECTED`, 401 `AUTH_FAILED`; failures after the create name the empty placeholder Vimeo keeps.
- The Vimeo upload-limit table (cited, 2026-10-31; unknowns marked); `plan` counts Vimeo's description in characters and reports the delivery spec as UNCHECKED, because ffmpeg-skill has no Vimeo spec.
- Tested only against an in-process mock of the API and tus on 127.0.0.1; the socket guard lets tests reach nothing else. No real upload has been made.

The YouTube uploader (roadmap step 4).

- ADR-004: `publish upload --confirm <token>` uploads the video, its thumbnail and its captions with the YouTube Data API v3 resumable upload protocol (OAuth refresh token -> access token, 8 MiB chunks, status query and resume after an interrupted chunk, exponential backoff on 5xx/429 and timeouts, a new session after a 404). Standard library only (`http.client`), in one module, the only one with network code.
- Exactly the bound request is sent: the video resource is the bound title / description / tags / privacy (default `private`), and the last chunk is held back unless the bytes match the token. `--caption-language` (BCP-47, one per `--caption`) is new and bound by the token; `plan` checks it.
- New typed errors: `AUTH_FAILED` (exit 3); `QUOTA_EXCEEDED`, `UPLOAD_LIMIT_EXCEEDED`, `UPLOAD_INTERRUPTED`, `UPLOAD_REJECTED`, `UPLOAD_PARTIAL` (exit 4). Receipts and errors pass through the redactor, which now also covers the access token and the session URIs.
- Tested only against an in-process mock of the API on 127.0.0.1; the socket guard lets tests reach nothing else. No real upload has been made.

Credentials in the OS keychain (roadmap step 3).

- ADR-003: `publish auth status|login|logout`. Credentials come from the environment or the OS keychain (macOS Keychain, Windows Credential Manager, Secret Service), the environment first and per platform as a whole, so one platform's values never mix sources. `login` reads values from stdin or a prompt that does not echo, never from argv.
- The keychain is reached through `keyring>=25.0` (MIT), an optional extra: `pip install 'publish-skill[keychain]'`. Without it, credentials come from the environment only and `doctor` says so. Only the three OS keychains are used; `PUBLISH_KEYCHAIN=off` turns it off.
- Reports still say present / absent only; the redactor also covers every keychain value the process holds; every refusal still comes before any credential is read. New errors `KEYCHAIN_UNAVAILABLE` and `KEYCHAIN_ERROR` (exit 3).
- Tests run against an in-memory fake keychain, with the real one guarded off; CI checks that the fake is what runs, and installs the extra on Python 3.12.

`publish plan` (roadmap step 2).

- ADR-002: `publish plan FILE --platform ID` checks the deliverable and its title / description / tags / privacy / thumbnail / captions against the platform's upload limits, offline, with a PASS / WARN / FAIL / UNCHECKED row per limit and a fix for each; verdict PASS / WARN / FAIL / INCOMPLETE, exit 1 unless PASS or WARN.
- The YouTube upload-limit table (`limits.py`, the README, `contract --json` → `upload_limits`): every value cites the official page and the date it was read (2026-10-31); limits the documentation does not state are `unknown`, and the delivery spec stays with ffmpeg-skill (`delegated`).
- The ffmpeg-skill adapter: capability detection from its contract (`export --list`, `check --platform/--json/--no-loudness`, no network) and its `check` rows (codec, frame size, aspect, ...) passed through; the only module that starts a process.
- The confirmation token now binds the metadata, too, and `upload` takes the same metadata flags; metadata that breaks a limit is refused as `METADATA_INVALID` before a token is printed. Privacy defaults to `private`.

Scaffold (roadmap step 1).

- ADR-001: `publish contract` / `skill`, `doctor` (`--offline`, `--ffmpeg-skill`), `platforms`; `publish upload` is a dry run unless `--confirm` carries the token the dry run printed for exactly that file and platform; credentials from the environment only, reported as present / absent and redacted from every output; typed errors with exit codes and `retryable`.
- The YouTube uploader is registered as a stub (`NOT_IMPLEMENTED`, roadmap step 4) and `publish plan` arrives in step 2; both capabilities are declared `PROPOSED`. Nothing is uploaded in this release.

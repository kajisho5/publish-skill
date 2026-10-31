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



Upload finished deliverables to delivery platforms (YouTube and Vimeo) for the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS) (department 納品 / Delivery), after QC and a human's confirmation. Platform delivery specs (codecs, resolution, loudness) belong to [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill); this skill reads them and owns only the upload-side limits.

**Status: 0.1.0, unreleased.** `contract`, `doctor`, `platforms`, `plan`, `auth` and `upload` work. `upload` is a dry run unless it is given the exact confirmation token; then the YouTube uploader ([ADR-004](docs/decisions.md), [roadmap](ECOSYSTEM_ROADMAP.md) step 4) sends the video, its thumbnail and its captions with the YouTube Data API v3 resumable upload, and the Vimeo uploader ([ADR-005](docs/decisions.md), step 5) sends the video with its title, description and privacy with Vimeo's tus resumable upload. Both have been tested only against local mocks of the APIs, never against YouTube or Vimeo themselves: the first real upload to each is the owner's, with their credentials and their confirmation.

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
publish contract --json                                   # machine-readable contract (alias: skill --json)
```

Metadata flags, the same on `plan` and `upload`: `--title`, `--description` or `--description-file` (UTF-8), `--tag` (repeat), `--privacy private|unlisted|public` (default `private`; the YouTube API would default to `public`; on Vimeo `private` is `privacy.view: nobody`, "no one except the owner"), `--thumbnail`, `--caption` (repeat) with `--caption-language` (its BCP-47 tag, one per `--caption`, in order).

## Safety ([ADR-001](docs/decisions.md))

- **Dry run by default.** Without `--confirm`, `upload` reads the file and reports; it opens no connection.
- **A confirmation token per deliverable.** The dry run prints `confirm_token`, derived from the platform, the file's sha256 and size, and the metadata (title, description, tags, privacy, the thumbnail's bytes, and each caption file's bytes and language; [ADR-002](docs/decisions.md), [ADR-004](docs/decisions.md)). Only that exact token on the command line goes further, so an approval cannot be reused for a re-exported file, another platform or edited metadata. No environment variable or config file confirms for you. Metadata that breaks an upload limit is refused (`METADATA_INVALID`) before a token is printed.
- **Credentials from the environment or the OS keychain** ([ADR-003](docs/decisions.md)), never from files or flags, and never printed: reports say `present` / `absent`, and every output passes through a redactor. YouTube reads `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET` and `PUBLISH_YOUTUBE_REFRESH_TOKEN`; Vimeo reads `PUBLISH_VIMEO_ACCESS_TOKEN` (a personal access token). See below.
- **Exactly what was confirmed is sent** ([ADR-004](docs/decisions.md), [ADR-005](docs/decisions.md)). The video resource is the bound title, description, tags and privacy, nothing added; the thumbnail and captions are the bound files. Every file is hashed as it is sent, and the last chunk is held back unless the bytes match the token, so a file edited mid-upload is never published (`CONFIRM_MISMATCH`). Privacy is `private` unless `--privacy` says otherwise. A field a platform's uploader does not send (on Vimeo: tags, thumbnail, captions) is refused (`METADATA_INVALID`) before a token is printed, never silently dropped.
- **Network code lives in the uploader modules only**, `src/publish_skill/platforms/youtube.py` and `src/publish_skill/platforms/vimeo.py` (stdlib `http.client`, verified TLS), and runs only after the token matched and the credentials were read. The access token, the upload session URI and Vimeo's upload link are redacted like the credentials. The only process the package starts is ffmpeg-skill (below), from an argv list, never a shell.

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

## Credentials ([ADR-003](docs/decisions.md))

- **Two sources, the environment first.** If any of a platform's variables is set in the environment, the environment is that platform's only source and the keychain is not consulted; otherwise the OS keychain is. One platform's values never mix sources (a refresh token belongs to the OAuth client that issued it).
- **The OS keychain** is the macOS Keychain, the Windows Credential Manager or the Secret Service on Linux, reached through [keyring](https://pypi.org/project/keyring/) (MIT), an optional extra: `pip install 'publish-skill[keychain]'` installs `keyring>=25.0`. Without it, credentials come from the environment only and `doctor` says so. Only those three OS keychains are used: keyring's own backend choice (`PYTHON_KEYRING_BACKEND`, plug-in or file backends) is ignored. `PUBLISH_KEYCHAIN=off` turns the keychain off (headless machines, CI).
- **Items** are stored under the service `publish-skill`, with the variable's name as the account.
- **`publish auth login --platform youtube`** (or `--platform vimeo`: one value, the personal access token) reads the values from stdin, one per line in the order of the platform's variables (or of `--variable NAME`, repeatable), or from a prompt that does not echo when stdin is a terminal. No flag takes a value, so none reaches shell history. Every value is read before the first is stored. `login` stores values you already have; it does not run Google's OAuth consent screen or Vimeo's authorization flow (a Vimeo personal access token is generated on the app's page at https://developer.vimeo.com/apps, with the `upload` scope).
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

## JSON and errors

With `--json`, stdout holds exactly one JSON document. Failures are `{"ok": false, "error": {"code", "message", "retryable", "details"}}`; the code fixes the exit code (`contract --json` lists them under `errors`).

## Tests

```bash
python -m unittest discover -s tests -v
```

Every unit test runs with network access patched to fail, except to `127.0.0.1` on the port of a mock server the test itself started: the YouTube uploader's tests run against `tests/mock_youtube.py`, an in-process mock of the token endpoint and the resumable protocol (chunks, an interrupted chunk and its resume, timeouts, 5xx/429 backoff, quota and upload-limit errors, expired sessions, thumbnail, captions), and the Vimeo uploader's against `tests/mock_vimeo.py`, an in-process mock of `POST /me/videos` and tus 1.0.0 (PATCH/HEAD, 409 offset conflicts, an interrupted chunk and its resume, timeouts, 5xx/429 backoff, the 4101/4102/4104 account errors, an expired upload link). No test contacts Google or Vimeo, and no workflow performs a real upload; CI fails unless `test_the_guard_allows_only_the_mock_this_test_started`, `test_the_guard_blocks_every_non_loopback_host_for_vimeo` and the resume and quota tests of both uploaders passed. `plan`'s tests run fake ffmpeg-skill checkouts. No test reaches a real OS keychain: the package's only import of keyring is replaced by a guard for the whole test run, every test gets an in-memory fake keychain, and CI fails unless `test_the_fake_keychain_is_what_runs` passed. The OS registry conformance checks and the JSON Schema validation of the contract run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present and `jsonschema` is installed; the integration tests against a real ffmpeg-skill run when `PUBLISH_FFMPEG_SKILL_DIR` (or `../ffmpeg-skill`), FFmpeg and Python >= 3.10 are present. CI's `conformance` job provides all of them and fails if any test is skipped.

## Licence

MIT.

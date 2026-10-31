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



Upload finished deliverables to delivery platforms (YouTube first) for the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS) (department 納品 / Delivery), after QC and a human's confirmation. Platform delivery specs (codecs, resolution, loudness) belong to [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill); this skill reads them and owns only the upload-side limits.

**Status: 0.1.0, unreleased.** `contract`, `doctor`, `platforms`, `plan`, `auth` and `upload` work. `upload` is a dry run unless it is given the exact confirmation token; then the YouTube uploader ([ADR-004](docs/decisions.md), [roadmap](ECOSYSTEM_ROADMAP.md) step 4) sends the video, its thumbnail and its captions with the YouTube Data API v3 resumable upload. It has been tested only against a local mock of the API, never against YouTube itself: the first real upload is the owner's, with their credentials and their confirmation.

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
publish contract --json                                   # machine-readable contract (alias: skill --json)
```

Metadata flags, the same on `plan` and `upload`: `--title`, `--description` or `--description-file` (UTF-8), `--tag` (repeat), `--privacy private|unlisted|public` (default `private`; the YouTube API would default to `public`), `--thumbnail`, `--caption` (repeat) with `--caption-language` (its BCP-47 tag, one per `--caption`, in order).

## Safety ([ADR-001](docs/decisions.md))

- **Dry run by default.** Without `--confirm`, `upload` reads the file and reports; it opens no connection.
- **A confirmation token per deliverable.** The dry run prints `confirm_token`, derived from the platform, the file's sha256 and size, and the metadata (title, description, tags, privacy, the thumbnail's bytes, and each caption file's bytes and language; [ADR-002](docs/decisions.md), [ADR-004](docs/decisions.md)). Only that exact token on the command line goes further, so an approval cannot be reused for a re-exported file, another platform or edited metadata. No environment variable or config file confirms for you. Metadata that breaks an upload limit is refused (`METADATA_INVALID`) before a token is printed.
- **Credentials from the environment or the OS keychain** ([ADR-003](docs/decisions.md)), never from files or flags, and never printed: reports say `present` / `absent`, and every output passes through a redactor. YouTube reads `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET` and `PUBLISH_YOUTUBE_REFRESH_TOKEN`. See below.
- **Exactly what was confirmed is sent** ([ADR-004](docs/decisions.md)). The video resource is the bound title, description, tags and privacy, nothing added; the thumbnail and captions are the bound files. Every file is hashed as it is sent, and the last chunk is held back unless the bytes match the token, so a file edited mid-upload is never published (`CONFIRM_MISMATCH`). Privacy is `private` unless `--privacy` says otherwise.
- **Network code lives in one module**, `src/publish_skill/platforms/youtube.py` (stdlib `http.client`, verified TLS), and runs only after the token matched and the credentials were read. The access token and the upload session URI are redacted like the credentials. The only process the package starts is ffmpeg-skill (below), from an argv list, never a shell.

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

## Credentials ([ADR-003](docs/decisions.md))

- **Two sources, the environment first.** If any of a platform's variables is set in the environment, the environment is that platform's only source and the keychain is not consulted; otherwise the OS keychain is. One platform's values never mix sources (a refresh token belongs to the OAuth client that issued it).
- **The OS keychain** is the macOS Keychain, the Windows Credential Manager or the Secret Service on Linux, reached through [keyring](https://pypi.org/project/keyring/) (MIT), an optional extra: `pip install 'publish-skill[keychain]'` installs `keyring>=25.0`. Without it, credentials come from the environment only and `doctor` says so. Only those three OS keychains are used: keyring's own backend choice (`PYTHON_KEYRING_BACKEND`, plug-in or file backends) is ignored. `PUBLISH_KEYCHAIN=off` turns the keychain off (headless machines, CI).
- **Items** are stored under the service `publish-skill`, with the variable's name as the account.
- **`publish auth login --platform youtube`** reads the values from stdin, one per line in the order of the platform's variables (or of `--variable NAME`, repeatable), or from a prompt that does not echo when stdin is a terminal. No flag takes a value, so none reaches shell history. Every value is read before the first is stored. `login` stores values you already have; it does not run Google's OAuth consent screen.
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

## JSON and errors

With `--json`, stdout holds exactly one JSON document. Failures are `{"ok": false, "error": {"code", "message", "retryable", "details"}}`; the code fixes the exit code (`contract --json` lists them under `errors`).

## Tests

```bash
python -m unittest discover -s tests -v
```

Every unit test runs with network access patched to fail, except to `127.0.0.1` on the port of a mock server the test itself started: the uploader's tests run against `tests/mock_youtube.py`, an in-process mock of the token endpoint and the resumable protocol (chunks, an interrupted chunk and its resume, timeouts, 5xx/429 backoff, quota and upload-limit errors, expired sessions, thumbnail, captions). No test contacts Google, and no workflow performs a real upload; CI fails unless `test_the_guard_allows_only_the_mock_this_test_started` passed. `plan`'s tests run fake ffmpeg-skill checkouts. No test reaches a real OS keychain: the package's only import of keyring is replaced by a guard for the whole test run, every test gets an in-memory fake keychain, and CI fails unless `test_the_fake_keychain_is_what_runs` passed. The OS registry conformance checks and the JSON Schema validation of the contract run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present and `jsonschema` is installed; the integration tests against a real ffmpeg-skill run when `PUBLISH_FFMPEG_SKILL_DIR` (or `../ffmpeg-skill`), FFmpeg and Python >= 3.10 are present. CI's `conformance` job provides all of them and fails if any test is skipped.

## Licence

MIT.

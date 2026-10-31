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

**Status: 0.1.0, unreleased.** `contract`, `doctor`, `platforms`, `plan`, `auth` and the dry run of `upload` work. **No uploader is implemented, so nothing is ever uploaded yet**; the YouTube uploader arrives in [roadmap](ECOSYSTEM_ROADMAP.md) step 4.

```bash
pip install -e .                                          # stdlib only, Python >= 3.9
pip install -e '.[keychain]'                              # optional: the OS keychain (keyring>=25.0)
publish doctor                                            # platforms, credentials present/absent, keychain, ffmpeg-skill
publish plan final.mp4 --platform youtube --title "Launch" --privacy unlisted --thumbnail thumb.jpg --json
                                                          # offline: every limit, PASS/WARN/FAIL with a fix per row
publish upload final.mp4 --platform youtube --title "Launch" --privacy unlisted --thumbnail thumb.jpg --json
                                                          # dry run: what would be sent, and the confirm_token
publish upload final.mp4 --platform youtube --title "Launch" --privacy unlisted --thumbnail thumb.jpg --confirm <confirm_token> --json
publish auth login --platform youtube                     # store credentials in the OS keychain (stdin or a hidden prompt)
publish auth status --json                                # present/absent in the environment and the keychain
publish auth logout --platform youtube                    # remove them from the keychain
publish contract --json                                   # machine-readable contract (alias: skill --json)
```

Metadata flags, the same on `plan` and `upload`: `--title`, `--description` or `--description-file` (UTF-8), `--tag` (repeat), `--privacy private|unlisted|public` (default `private`; the YouTube API would default to `public`), `--thumbnail`, `--caption` (repeat).

## Safety ([ADR-001](docs/decisions.md))

- **Dry run by default.** Without `--confirm`, `upload` reads the file and reports; it opens no connection.
- **A confirmation token per deliverable.** The dry run prints `confirm_token`, derived from the platform, the file's sha256 and size, and the metadata (title, description, tags, privacy, and the thumbnail's and captions' bytes; [ADR-002](docs/decisions.md)). Only that exact token on the command line goes further, so an approval cannot be reused for a re-exported file, another platform or edited metadata. No environment variable or config file confirms for you. Metadata that breaks an upload limit is refused (`METADATA_INVALID`) before a token is printed.
- **Credentials from the environment or the OS keychain** ([ADR-003](docs/decisions.md)), never from files or flags, and never printed: reports say `present` / `absent`, and every output passes through a redactor. YouTube reads `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET` and `PUBLISH_YOUTUBE_REFRESH_TOKEN`. See below.
- **Uploaders are stubs** that return `NOT_IMPLEMENTED` (exit 5); the package contains no network code yet. The only process it starts is ffmpeg-skill (below), from an argv list, never a shell.

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

Every unit test runs with network access patched to fail; `plan`'s tests run fake ffmpeg-skill checkouts. No test reaches a real OS keychain: the package's only import of keyring is replaced by a guard for the whole test run, every test gets an in-memory fake keychain, and CI fails unless `test_the_fake_keychain_is_what_runs` passed. The OS registry conformance checks and the JSON Schema validation of the contract run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present and `jsonschema` is installed; the integration tests against a real ffmpeg-skill run when `PUBLISH_FFMPEG_SKILL_DIR` (or `../ffmpeg-skill`), FFmpeg and Python >= 3.10 are present. CI's `conformance` job provides all of them and fails if any test is skipped.

## Licence

MIT.

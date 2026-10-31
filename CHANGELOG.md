# Changelog

## 0.1.0 - unreleased

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

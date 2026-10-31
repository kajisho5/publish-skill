---
name: publish-skill
description: Upload a finished, QC-passed deliverable to a delivery platform (YouTube first), and check it and its title, description, tags, privacy, thumbnail and captions against the platform's upload limits offline first. Use when asked to publish, upload or post a finished video to a platform, to check whether a video and its metadata will be accepted, or to check whether publishing is set up. Dry run by default; an upload needs the confirmation token the dry run printed for exactly that file, metadata and platform, and a human's approval. Credentials come from environment variables or the OS keychain (`publish auth login`) and are never printed. YouTube uploads use the Data API v3 resumable upload and resume interrupted chunks.
---

# publish-skill

Run `publish <command> ... --json` (installed by `pip install -e .`). Every command prints one JSON document. `publish doctor --json` says which platforms are implemented and whether their credentials are present (never their values).

| Task | Command |
|---|---|
| Is publishing set up? | `publish doctor --json` |
| Platforms and their credential variables | `publish platforms --json` |
| Credentials present / absent (environment and OS keychain) | `publish auth status --json` |
| Store credentials in the OS keychain (the user types them) | `publish auth login --platform youtube` |
| Remove them from the OS keychain | `publish auth logout --platform youtube` |
| Check the file and its metadata against the platform's limits (offline) | `publish plan final.mp4 --platform youtube --title "..." --privacy unlisted --thumbnail thumb.jpg --json` |
| What would be uploaded (dry run, no network) | `publish upload final.mp4 --platform youtube --title "..." --privacy unlisted --thumbnail thumb.jpg --json` |
| Upload, after QC and a human's approval | the same command plus `--confirm <confirm_token from the dry run>` |
| What this skill declares | `publish contract --json` |

Metadata flags, identical on `plan` and `upload`: `--title`, `--description` or `--description-file`, `--tag` (repeat), `--privacy private|unlisted|public` (default `private`), `--thumbnail`, `--caption` (repeat) with `--caption-language` (its BCP-47 tag, e.g. `en`, one per `--caption`, in order).

## Rules

- **Publishing is outward-facing and cannot be undone.** Upload only a deliverable that has passed QC (qc-skill), and only after a human has approved this upload of this file to this platform. Never add `--confirm` on your own initiative.
- **Plan first.** Run `publish plan` with the metadata you intend to send. Fix every `FAIL` row (each has a `fix`; rows owned by `ffmpeg-skill` are fixed with ffmpeg-skill) and show the human the `WARN` rows. `INCOMPLETE` means some rows could not be checked (usually ffmpeg-skill is missing): say so; it is not a pass.
- **Dry run next, every time.** Run `upload` with the same metadata flags and without `--confirm`, show the human the `input` (path, size, sha256), the `metadata`, the platform and the `credentials` status, and pass on `confirm_token` only after they approve. A token from an earlier dry run does not work for a re-exported file, another platform or edited metadata (`CONFIRM_MISMATCH`); run the dry run again.
- **Credentials.** They are read from the environment or the OS keychain (YouTube: `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET`, `PUBLISH_YOUTUBE_REFRESH_TOKEN`). If any of a platform's variables is set in the environment, the keychain is not consulted for that platform. Never ask the user to paste a secret into the chat, never put one on a command line, and never print or log one. Reports say `present` / `absent`.
- **`publish auth login` is for the user to run in their own terminal**: it reads the values from a hidden prompt (or stdin, one per line). Do not pipe a secret into it yourself. The keychain needs `pip install 'publish-skill[keychain]'`; without it, `doctor` reports the keychain as unavailable and credentials come from the environment only.

## Report it honestly

- A dry run is not an upload: say "nothing was uploaded" when `dry_run` is true. Neither is a plan.
- An upload succeeded only when `uploaded` is true: report `result.video_id`, `result.url` and `result.privacy`, any `result.warnings`, and that YouTube is still processing it.
- `UPLOAD_PARTIAL` (exit 4): the video **is** uploaded (`details.url`) but its thumbnail or captions are not. Say so; never run `upload` again (it would upload the video twice); the rest is added in YouTube Studio.
- `UPLOAD_INTERRUPTED`, `QUOTA_EXCEEDED`, `UPLOAD_LIMIT_EXCEEDED` (exit 4, retryable): nothing was published. Report the reason; retry only with the human's go-ahead (a new dry run is not needed for the same file and metadata), and not before the quota or daily limit resets.
- `UPLOAD_REJECTED` (exit 4): YouTube refused the request (`details.reasons`); nothing was published.
- `AUTH_FAILED` (exit 3): the refresh token was refused or lacks a scope (`details`); the user stores a new one with `publish auth login --platform youtube`.
- `plan` verdict `FAIL` / `INCOMPLETE` (exit 1): report the failing or unchecked rows with their `fix`; do not upload.
- `METADATA_INVALID` (exit 2): the metadata breaks a limit; run `plan` to see which and how to fix it.
- `NOT_IMPLEMENTED` (exit 5): the platform's uploader does not exist yet (each further platform is roadmap step 5). Say so; do not upload some other way.
- `CREDENTIALS_MISSING` (exit 3): name the variables from `details.missing` and tell the user to set them in their environment or to run `publish auth login --platform <id>` themselves.
- `KEYCHAIN_UNAVAILABLE` / `KEYCHAIN_ERROR` (exit 3): the OS keychain is not installed, not usable or refused; report the reason; the environment still works.

Exit codes: `0` ok (including a dry run, and a plan whose verdict is PASS or WARN), `1` a plan whose verdict is FAIL or INCOMPLETE, `2` refused (`UNKNOWN_PLATFORM`, `INPUT_NOT_FOUND`, `CONFIRM_MISMATCH`, `METADATA_INVALID`, `INVALID_ARGUMENT`), `3` credentials (`CREDENTIALS_MISSING`, `KEYCHAIN_UNAVAILABLE`, `KEYCHAIN_ERROR`, `AUTH_FAILED`), `4` the upload (`QUOTA_EXCEEDED`, `UPLOAD_LIMIT_EXCEEDED`, `UPLOAD_INTERRUPTED`, `UPLOAD_REJECTED`, `UPLOAD_PARTIAL`), `5` not implemented, `6` cancelled.

## Not this skill's job

Platform delivery specs and encoding (ffmpeg-skill), quality control (qc-skill), deciding whether to publish (the human), downloading media (ingest-skill).

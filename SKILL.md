---
name: publish-skill
description: Upload a finished, QC-passed deliverable to a delivery platform (YouTube first), and check it and its title, description, tags, privacy, thumbnail and captions against the platform's upload limits offline first. Use when asked to publish, upload or post a finished video to a platform, to check whether a video and its metadata will be accepted, or to check whether publishing is set up. Dry run by default; an upload needs the confirmation token the dry run printed for exactly that file, metadata and platform, and a human's approval. Credentials come from environment variables only and are never printed. In this release no uploader is implemented, so nothing is ever uploaded.
---

# publish-skill

Run `publish <command> ... --json` (installed by `pip install -e .`). Every command prints one JSON document. `publish doctor --json` says which platforms are implemented and whether their credentials are present (never their values).

| Task | Command |
|---|---|
| Is publishing set up? | `publish doctor --json` |
| Platforms and their credential variables | `publish platforms --json` |
| Check the file and its metadata against the platform's limits (offline) | `publish plan final.mp4 --platform youtube --title "..." --privacy unlisted --thumbnail thumb.jpg --json` |
| What would be uploaded (dry run, no network) | `publish upload final.mp4 --platform youtube --title "..." --privacy unlisted --thumbnail thumb.jpg --json` |
| Upload, after QC and a human's approval | the same command plus `--confirm <confirm_token from the dry run>` |

Metadata flags, identical on `plan` and `upload`: `--title`, `--description` or `--description-file`, `--tag` (repeat), `--privacy private|unlisted|public` (default `private`), `--thumbnail`, `--caption` (repeat).
| What this skill declares | `publish contract --json` |

## Rules

- **Publishing is outward-facing and cannot be undone.** Upload only a deliverable that has passed QC (qc-skill), and only after a human has approved this upload of this file to this platform. Never add `--confirm` on your own initiative.
- **Plan first.** Run `publish plan` with the metadata you intend to send. Fix every `FAIL` row (each has a `fix`; rows owned by `ffmpeg-skill` are fixed with ffmpeg-skill) and show the human the `WARN` rows. `INCOMPLETE` means some rows could not be checked (usually ffmpeg-skill is missing): say so; it is not a pass.
- **Dry run next, every time.** Run `upload` with the same metadata flags and without `--confirm`, show the human the `input` (path, size, sha256), the `metadata`, the platform and the `credentials` status, and pass on `confirm_token` only after they approve. A token from an earlier dry run does not work for a re-exported file, another platform or edited metadata (`CONFIRM_MISMATCH`); run the dry run again.
- **Credentials.** They are read from the environment only (YouTube: `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET`, `PUBLISH_YOUTUBE_REFRESH_TOKEN`). Never ask the user to paste a secret into the chat, never put one on a command line, and never print or log one. Reports say `present` / `absent`.

## Report it honestly

- A dry run is not an upload: say "nothing was uploaded" when `dry_run` is true. Neither is a plan.
- `plan` verdict `FAIL` / `INCOMPLETE` (exit 1): report the failing or unchecked rows with their `fix`; do not upload.
- `METADATA_INVALID` (exit 2): the metadata breaks a limit; run `plan` to see which and how to fix it.
- `NOT_IMPLEMENTED` (exit 5): the platform's uploader does not exist yet (YouTube arrives in roadmap step 4). Say so; do not upload some other way.
- `CREDENTIALS_MISSING` (exit 3): name the variables from `details.missing` and tell the user to set them in their environment.

Exit codes: `0` ok (including a dry run, and a plan whose verdict is PASS or WARN), `1` a plan whose verdict is FAIL or INCOMPLETE, `2` refused (`UNKNOWN_PLATFORM`, `INPUT_NOT_FOUND`, `CONFIRM_MISMATCH`, `METADATA_INVALID`, `INVALID_ARGUMENT`), `3` credentials missing, `5` not implemented, `6` cancelled.

## Not this skill's job

Platform delivery specs and encoding (ffmpeg-skill), quality control (qc-skill), deciding whether to publish (the human), downloading media (ingest-skill).

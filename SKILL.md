---
name: publish-skill
description: Upload a finished, QC-passed deliverable to a delivery platform (YouTube first). Use when asked to publish, upload or post a finished video to a platform, or to check whether publishing is set up. Dry run by default; an upload needs the confirmation token the dry run printed for exactly that file and platform, and a human's approval. Credentials come from environment variables only and are never printed. In this release no uploader is implemented, so nothing is ever uploaded.
---

# publish-skill

Run `publish <command> ... --json` (installed by `pip install -e .`). Every command prints one JSON document. `publish doctor --json` says which platforms are implemented and whether their credentials are present (never their values).

| Task | Command |
|---|---|
| Is publishing set up? | `publish doctor --json` |
| Platforms and their credential variables | `publish platforms --json` |
| What would be uploaded (dry run, no network) | `publish upload final.mp4 --platform youtube --json` |
| Upload, after QC and a human's approval | `publish upload final.mp4 --platform youtube --confirm <confirm_token from the dry run> --json` |
| Check against the platform's limits | `publish plan final.mp4 --platform youtube --json` (roadmap step 2, not implemented yet) |
| What this skill declares | `publish contract --json` |

## Rules

- **Publishing is outward-facing and cannot be undone.** Upload only a deliverable that has passed QC (qc-skill), and only after a human has approved this upload of this file to this platform. Never add `--confirm` on your own initiative.
- **Dry run first, every time.** Run without `--confirm`, show the human the `input` (path, size, sha256), the platform and the `credentials` status, and pass on `confirm_token` only after they approve. A token from an earlier dry run does not work for a re-exported file or another platform (`CONFIRM_MISMATCH`); run the dry run again.
- **Credentials.** They are read from the environment only (YouTube: `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET`, `PUBLISH_YOUTUBE_REFRESH_TOKEN`). Never ask the user to paste a secret into the chat, never put one on a command line, and never print or log one. Reports say `present` / `absent`.

## Report it honestly

- A dry run is not an upload: say "nothing was uploaded" when `dry_run` is true.
- `NOT_IMPLEMENTED` (exit 5): the platform's uploader does not exist yet (YouTube arrives in roadmap step 4). Say so; do not upload some other way.
- `CREDENTIALS_MISSING` (exit 3): name the variables from `details.missing` and tell the user to set them in their environment.

Exit codes: `0` ok (including a dry run), `2` refused (`UNKNOWN_PLATFORM`, `INPUT_NOT_FOUND`, `CONFIRM_MISMATCH`, `INVALID_ARGUMENT`), `3` credentials missing, `5` not implemented, `6` cancelled.

## Not this skill's job

Platform delivery specs and encoding (ffmpeg-skill), quality control (qc-skill), deciding whether to publish (the human), downloading media (ingest-skill).

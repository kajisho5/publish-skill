---
name: publish-skill
description: Upload a finished, QC-passed deliverable to a delivery platform (YouTube, Vimeo, TikTok, X or LinkedIn), and check it and its title, description, tags, privacy, thumbnail and captions against the platform's upload limits offline first. Use when asked to publish, upload or post a finished video to a platform, to check whether a video and its metadata will be accepted, or to check whether publishing is set up. Dry run by default; an upload needs the confirmation token the dry run printed for exactly that file, metadata and platform, and a human's approval. Credentials come from environment variables or the OS keychain (`publish auth login`) and are never printed. YouTube uploads use the Data API v3 resumable upload, Vimeo uploads the tus resumable upload TikTok uploads the Content Posting API's Direct Post in chunks X uploads the X API v2 chunked media upload and then one public post, and LinkedIn uploads the Videos API multipart upload and then one public post; all five resume interrupted chunks.
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
| The same for Vimeo (title, description, privacy only) | `--platform vimeo` on `plan` and `upload` |
| The same for TikTok (title and privacy only; private or public) | `--platform tiktok` on `plan` and `upload` |
| The same for X (title as the post's text; `--privacy public` required) | `--platform x` on `plan` and `upload` |
| The same for LinkedIn (title as the commentary; `--privacy public` required; .mp4 only) | `--platform linkedin` on `plan` and `upload` |
| What this skill declares | `publish contract --json` |

Metadata flags, identical on `plan` and `upload`: `--title`, `--description` or `--description-file`, `--tag` (repeat), `--privacy private|unlisted|public` (default `private`), `--thumbnail`, `--caption` (repeat) with `--caption-language` (its BCP-47 tag, e.g. `en`, one per `--caption`, in order).

## Rules

- **Publishing is outward-facing and cannot be undone.** Upload only a deliverable that has passed QC (qc-skill), and only after a human has approved this upload of this file to this platform. Never add `--confirm` on your own initiative.
- **Plan first.** Run `publish plan` with the metadata you intend to send. Fix every `FAIL` row (each has a `fix`; rows owned by `ffmpeg-skill` are fixed with ffmpeg-skill) and show the human the `WARN` rows. `INCOMPLETE` means some rows could not be checked (usually ffmpeg-skill is missing): say so; it is not a pass.
- **Dry run next, every time.** Run `upload` with the same metadata flags and without `--confirm`, show the human the `input` (path, size, sha256), the `metadata`, the platform and the `credentials` status, and pass on `confirm_token` only after they approve. A token from an earlier dry run does not work for a re-exported file, another platform or edited metadata (`CONFIRM_MISMATCH`); run the dry run again.
- **Credentials.** They are read from the environment or the OS keychain (YouTube: `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET`, `PUBLISH_YOUTUBE_REFRESH_TOKEN`; Vimeo: `PUBLISH_VIMEO_ACCESS_TOKEN`, a personal access token with the `upload` scope; TikTok: `PUBLISH_TIKTOK_ACCESS_TOKEN`, a user access token with the `video.publish` scope, valid for 24 hours; X: `PUBLISH_X_ACCESS_TOKEN`, an OAuth 2.0 user access token with `media.write`, `tweet.write`, `tweet.read` and `users.read`, valid for two hours; LinkedIn: `PUBLISH_LINKEDIN_ACCESS_TOKEN`, a member access token with `w_member_social` or `w_organization_social`, valid for 60 days, and `PUBLISH_LINKEDIN_AUTHOR`, the author's URN `urn:li:person:<id>` or `urn:li:organization:<id>`). If any of a platform's variables is set in the environment, the keychain is not consulted for that platform. Never ask the user to paste a secret into the chat, never put one on a command line, and never print or log one. Reports say `present` / `absent`.
- **`publish auth login` is for the user to run in their own terminal**: it reads the values from a hidden prompt (or stdin, one per line). Do not pipe a secret into it yourself. The keychain needs `pip install 'publish-skill[keychain]'`; without it, `doctor` reports the keychain as unavailable and credentials come from the environment only.

- **Vimeo sends the title, description and privacy only.** `--tag`, `--thumbnail` and `--caption` are refused for Vimeo (`METADATA_INVALID`); leave them off and say they must be added on Vimeo. `private` is Vimeo's `nobody` (only the owner). `plan --platform vimeo` is always `INCOMPLETE`, because ffmpeg-skill has no Vimeo delivery spec: say so; it is not a pass.
- **TikTok sends the title (its caption) and privacy only.** `--description`, `--tag`, `--thumbnail`, `--caption` and `--privacy unlisted` are refused (`METADATA_INVALID`), and so is a file that is not `.mp4`, `.mov` or `.webm` (`INVALID_ARGUMENT`). `private` is TikTok's `SELF_ONLY`. A TikTok app that TikTok has not audited can only post privately; say so if the human wants a public post.
- **X sends one public post: the title is its text.** X has no private or unlisted post, so `--privacy public` must be given, and the human must know the post is public the moment it is made. `--description`, `--tag`, `--thumbnail` and `--caption` are refused (`METADATA_INVALID`), and so is a file that is not `.mp4`, `.mov`, `.webm` or `.ts` (`INVALID_ARGUMENT`). The title length is X's weighted count (CJK and emoji count 2, a URL 23): a `WARN` on it means only an upper bound was over 280.
- **LinkedIn sends one public post: the title is its commentary.** CONNECTIONS and LOGGED_IN are not offered, so `--privacy public` must be given, and the human must know the post is public the moment it is made. The title is sent as typed: `#` and `@` are escaped, so no hashtag link or mention is made; say so. `--description`, `--tag`, `--thumbnail` and `--caption` are refused (`METADATA_INVALID`), and so is a file that is not `.mp4` (`INVALID_ARGUMENT`). The commentary's maximum length is not documented for the Posts API, so `plan` does not check it.

## Report it honestly

- A dry run is not an upload: say "nothing was uploaded" when `dry_run` is true. Neither is a plan.
- An upload succeeded only when `uploaded` is true: report `result.video_id`, `result.url` and `result.privacy`, any `result.warnings`, and that the platform is still processing it.
- Vimeo, `UPLOAD_INTERRUPTED` / `UPLOAD_REJECTED` / `CONFIRM_MISMATCH` after the video was created: nothing was completed, but Vimeo keeps an empty placeholder (`details.video_uri`); say so. `UPLOAD_LIMIT_EXCEEDED` on Vimeo is the account's weekly or daily quota (error 4102 / 4104); `UPLOAD_REJECTED` with error 4101 is full storage.
- TikTok: a receipt whose `result.status` is not `PUBLISH_COMPLETE` means the video is uploaded and TikTok is still processing the post; say so and do not run `upload` again. `UPLOAD_LIMIT_EXCEEDED` is the creator's daily post cap, `QUOTA_EXCEEDED` the app's daily quota of publishing users; `UPLOAD_REJECTED` with `details.fail_reason` (e.g. `duration_check_failed`) is a post TikTok failed.
- X: `result.url` is `null` (X's API documentation gives no post URL); report `result.post_id`. `UPLOAD_INTERRUPTED` with `retryable: false` at step `post` means the media is uploaded and it is unknown whether the post was made: tell the human to check the profile, and never run `upload` again before they have.
- LinkedIn: report `result.post_id` (a URN) and `result.url` when there is one. `UPLOAD_INTERRUPTED` with `retryable: false` at step `post` means the video is uploaded and it is unknown whether the post was made: tell the human to check the author's page, and never run `upload` again before they have.
- `UPLOAD_PARTIAL` (exit 4): the video **is** uploaded (`details.url`) but its thumbnail or captions are not. Say so; never run `upload` again (it would upload the video twice); the rest is added in YouTube Studio.
- `UPLOAD_INTERRUPTED`, `QUOTA_EXCEEDED`, `UPLOAD_LIMIT_EXCEEDED` (exit 4, retryable): nothing was published. Report the reason; retry only with the human's go-ahead (a new dry run is not needed for the same file and metadata), and not before the quota or daily limit resets.
- `UPLOAD_REJECTED` (exit 4): YouTube refused the request (`details.reasons`); nothing was published.
- `AUTH_FAILED` (exit 3): the refresh token (YouTube) or the access token (Vimeo, TikTok, X, LinkedIn) was refused or lacks a scope (`details`); the user stores a new one with `publish auth login --platform <id>`.
- `plan` verdict `FAIL` / `INCOMPLETE` (exit 1): report the failing or unchecked rows with their `fix`; do not upload.
- `METADATA_INVALID` (exit 2): the metadata breaks a limit; run `plan` to see which and how to fix it.
- `NOT_IMPLEMENTED` (exit 5): the platform's uploader does not exist yet (each further platform is roadmap step 5). Say so; do not upload some other way.
- `CREDENTIALS_MISSING` (exit 3): name the variables from `details.missing` and tell the user to set them in their environment or to run `publish auth login --platform <id>` themselves.
- `KEYCHAIN_UNAVAILABLE` / `KEYCHAIN_ERROR` (exit 3): the OS keychain is not installed, not usable or refused; report the reason; the environment still works.

Exit codes: `0` ok (including a dry run, and a plan whose verdict is PASS or WARN), `1` a plan whose verdict is FAIL or INCOMPLETE, `2` refused (`UNKNOWN_PLATFORM`, `INPUT_NOT_FOUND`, `CONFIRM_MISMATCH`, `METADATA_INVALID`, `INVALID_ARGUMENT`), `3` credentials (`CREDENTIALS_MISSING`, `KEYCHAIN_UNAVAILABLE`, `KEYCHAIN_ERROR`, `AUTH_FAILED`), `4` the upload (`QUOTA_EXCEEDED`, `UPLOAD_LIMIT_EXCEEDED`, `UPLOAD_INTERRUPTED`, `UPLOAD_REJECTED`, `UPLOAD_PARTIAL`), `5` not implemented, `6` cancelled.

## Not this skill's job

Platform delivery specs and encoding (ffmpeg-skill), quality control (qc-skill), deciding whether to publish (the human), downloading media (ingest-skill).

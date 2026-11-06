# Roadmap — publish-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **納品 (Delivery)**, status `scaffold`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

Scaffold (0.1.0, [#4](https://github.com/kajisho5/publish-skill/pull/4)): `contract`, `doctor`, `platforms` and the dry run of `upload` behind a per-deliverable confirmation token (ADR-001). `publish plan` ([#5](https://github.com/kajisho5/publish-skill/pull/5), ADR-002): the deliverable and its metadata against YouTube's cited upload limits and ffmpeg-skill's `check`, offline; the token binds the metadata. Credentials ([#6](https://github.com/kajisho5/publish-skill/pull/6), ADR-003): the environment or the OS keychain (`publish auth login|status|logout`, keyring as an optional extra), never printed. YouTube uploader ([#7](https://github.com/kajisho5/publish-skill/pull/7), ADR-004): the Data API v3 resumable upload of the video, thumbnail and captions after the exact confirmation token, tested against a loopback mock only; no real upload has been made yet. Vimeo uploader ([#8](https://github.com/kajisho5/publish-skill/pull/8), ADR-005): the tus resumable upload of the video with its title, description and privacy (`nobody` by default) after the exact confirmation token, with a personal access token, tested against a loopback mock only. TikTok uploader ([#9](https://github.com/kajisho5/publish-skill/pull/9), ADR-006): the Content Posting API Direct Post (creator info, init with FILE_UPLOAD, chunked PUT, the post's status) with its title and privacy (`SELF_ONLY` by default) after the exact confirmation token, with a user access token, tested against a loopback mock only. X uploader ([#10](https://github.com/kajisho5/publish-skill/pull/10), ADR-007): the X API v2 chunked media upload and one public post with the title as its text, after the exact confirmation token, with an OAuth 2.0 user access token, tested against a loopback mock only. LinkedIn uploader ([#11](https://github.com/kajisho5/publish-skill/pull/11), ADR-008): the Videos API multipart upload and one public post with the title as its escaped commentary, after the exact confirmation token, with a member access token and the author's URN, tested against a loopback mock only. Facebook uploader ([#12](https://github.com/kajisho5/publish-skill/pull/12), ADR-009): the Resumable Upload API and one Page video with its title, description and privacy (public or unlisted), after the exact confirmation token, with a user and a Page access token, tested against a loopback mock only. Instagram Reels uploader ([#13](https://github.com/kajisho5/publish-skill/pull/13), ADR-010): a resumable REELS container, the rupload upload and one public reel with the title as its caption, after the exact confirmation token, with a Facebook Login for Business token, tested against a loopback mock only. Step 5 is complete. Chapters, a scheduled time and localized metadata ([#17](https://github.com/kajisho5/publish-skill/pull/17), ADR-011): `plan` checks YouTube's chapter rules; YouTube uploads carry `status.publishAt`, a default language, localizations and one caption track per language.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| 配信先アップロード | YouTube ([#7](https://github.com/kajisho5/publish-skill/pull/7)), Vimeo ([#8](https://github.com/kajisho5/publish-skill/pull/8)), TikTok ([#9](https://github.com/kajisho5/publish-skill/pull/9)), X ([#10](https://github.com/kajisho5/publish-skill/pull/10)), LinkedIn ([#11](https://github.com/kajisho5/publish-skill/pull/11)), Facebook ([#12](https://github.com/kajisho5/publish-skill/pull/12)), Instagram Reels ([#13](https://github.com/kajisho5/publish-skill/pull/13)) | every platform with an ffmpeg-skill delivery spec (step 5 done) |
| アップロード制限 | YouTube ([#5](https://github.com/kajisho5/publish-skill/pull/5)), Vimeo ([#8](https://github.com/kajisho5/publish-skill/pull/8)), TikTok ([#9](https://github.com/kajisho5/publish-skill/pull/9)), X ([#10](https://github.com/kajisho5/publish-skill/pull/10)), LinkedIn ([#11](https://github.com/kajisho5/publish-skill/pull/11)), Facebook ([#12](https://github.com/kajisho5/publish-skill/pull/12)), Instagram Reels ([#13](https://github.com/kajisho5/publish-skill/pull/13)) | upload-side limits only (file size, duration, rate limits, metadata fields); the platform delivery specs belong to ffmpeg-skill `_platforms.py` -- read them, do not copy them |

## Wiring

The agent plans `publish.upload` as a CONFIRM decision after QC, runs only `plan` and the `upload` dry run, and uploads once on `video-agent publish --confirm` (step 6, video-production-agent#99). No real upload has been made yet.

## Steps

- [x] **1. Scaffold** — package, contract/doctor (network: true), SKILL.md, CI on 3 OSes ([#4](https://github.com/kajisho5/publish-skill/pull/4))
  - Done when: contract validates against the OS schema
- [x] **2. `publish plan` (offline)** — validate deliverable + title/description/tags/privacy/thumbnail/captions against platform limits; encode spec from ffmpeg-skill export --list / check ([#5](https://github.com/kajisho5/publish-skill/pull/5))
  - Done when: structured FAIL on an over-long title or wrong aspect, no network
- [x] **3. Credentials** — `publish auth login|status|logout`, OS keychain, secrets redacted everywhere ([#6](https://github.com/kajisho5/publish-skill/pull/6))
  - Done when: no token in stdout/stderr/receipt
- [x] **4. YouTube uploader** — Data API v3 resumable (video, thumbnail, captions); dry-run default, `--confirm` required ([#7](https://github.com/kajisho5/publish-skill/pull/7))
  - Done when: mock-server tests incl. resume
- [x] **5. Further platforms** — one PR each: Vimeo ([#8](https://github.com/kajisho5/publish-skill/pull/8)), TikTok ([#9](https://github.com/kajisho5/publish-skill/pull/9)), X ([#10](https://github.com/kajisho5/publish-skill/pull/10)), LinkedIn ([#11](https://github.com/kajisho5/publish-skill/pull/11)), Facebook ([#12](https://github.com/kajisho5/publish-skill/pull/12)), Instagram Reels ([#13](https://github.com/kajisho5/publish-skill/pull/13))
  - Done when: mock-server tests
  - [x] Vimeo — tus resumable upload, personal access token, title / description / privacy, cited limits; mock-server tests incl. resume and quota ([#8](https://github.com/kajisho5/publish-skill/pull/8), ADR-005)
  - [x] TikTok — Content Posting API Direct Post (FILE_UPLOAD, chunked PUT, post status), user access token, title / privacy, cited limits; mock-server tests incl. resume and the daily post cap ([#9](https://github.com/kajisho5/publish-skill/pull/9), ADR-006)
  - Remaining (ADR-007): the platforms ffmpeg-skill has a delivery spec for (`scripts/_platforms.py`) and no uploader yet: X, LinkedIn, Facebook, Instagram Reels (`shorts` is a YouTube upload)
  - [x] X — X API v2 chunked media upload (initialize, multipart append, finalize, processing status) and one public post, OAuth 2.0 user access token, title as the text / public only, cited limits; mock-server tests incl. resume and a post never sent twice ([#10](https://github.com/kajisho5/publish-skill/pull/10), ADR-007)
  - [x] LinkedIn — Videos API multipart upload (initializeUpload, signed part PUTs with ETags, finalizeUpload, status until AVAILABLE) and one public post with the Posts API, member access token and author URN, title as the escaped commentary / public only, cited limits (the commentary maximum is not documented: unknown, not checked); mock-server tests incl. resume and a post never sent twice ([#11](https://github.com/kajisho5/publish-skill/pull/11), ADR-008)
  - [x] Facebook — Resumable Upload API (session, upload with file_offset, resume) and one Page video (graph-video `/<PAGE_ID>/videos` with the file handle), user and Page access tokens, title / description / public or unlisted (secret), cited limits (size, duration and text limits not stated: unknown); mock-server tests incl. resume and a video never published twice ([#12](https://github.com/kajisho5/publish-skill/pull/12), ADR-009)
  - [x] Instagram Reels — resumable REELS container (graph.facebook.com), the rupload upload (offset 0; resent whole: no offset query is documented), status_code until FINISHED, media_publish, Facebook Login for Business token, title as the caption / public only, cited limits (300 MB, 3 s–15 min, 2200-character caption); mock-server tests incl. a resend after an interruption and a reel never published twice ([#13](https://github.com/kajisho5/publish-skill/pull/13), ADR-010)
- [x] **6. (agent side) SkillSpec publish_upload** — always CONFIRM, after QC ([video-production-agent#99](https://github.com/kajisho5/video-production-agent/pull/99), its ADR-087: decision `publish.upload` with a CONFIRM floor after the QC gate; render runs only the offline `plan` and the `upload` dry run; the upload is `video-agent publish <artifact> --confirm <token>` by a human, once)
  - Done when: plan shows a blocked publish step
- [ ] **7. First real uploads on test accounts** — one real upload per platform on a test account (private), with the result and the platform's processed facts recorded, so each uploader moves from mock-tested to used. Field review 2026-11-05 (Velisa ROADMAP, third round)
  - なぜ要るか：本番の納品日に初めて実アップロードする危険を無くす
  - Done when: a recorded private upload per platform, with its processing status
- [x] **8. Chapters, scheduled time and captions per language** — `plan` checks description chapters against YouTube's rules (first at 0:00, at least three, each at least 10 s), takes a scheduled publish time as upload metadata, and uploads one caption track per language (subtitle-skill output) with localised titles and descriptions given by the caller
  - なぜ要るか：チャプター付き・多言語字幕付き・予約公開の動画を、一度の操作で正しく上げられる
  - Done when: a plan with chapters starting at 0:05 is refused with the rule; a mock upload carries two caption tracks and a scheduled time
  - Done (ADR-011): `plan` FAILs description chapters that break YouTube's rules (first at 0:00, at least three, ascending, each at least 10 s; cited, 2026-11-06), so `upload` refuses them before a token; `--publish-at` (RFC 3339 with an offset, `private` only) → `status.publishAt`, `--language` → `snippet.defaultLanguage`, `--localizations FILE` → `localizations`, one caption track per language; YouTube only (other platforms refuse the fields); mock-tested (`test_chapters_starting_at_0_05_are_refused_with_the_rule`, `test_a_mock_upload_carries_two_caption_tracks_and_a_scheduled_time`); no real upload yet (step 7) ([#17](https://github.com/kajisho5/publish-skill/pull/17))
- [ ] **9. Clips as a series** — a set of Shorts / Reels from one source uploaded in order with their own titles and a link back to the full video, each one CONFIRM, never sent twice on retry
  - なぜ要るか：配信アーカイブから切り出した複数のショートを、取りこぼしや二重投稿なく出せる
  - Done when: a mock run of five clips with a failure on clip 3 resumes at clip 3 and uploads each once
- [ ] **10. Restricted delivery for organisers** — unlisted or private uploads (YouTube unlisted / private, Vimeo password or domain-limited) and a delivery receipt per session (URL, privacy, checksum of the uploaded file) for the organiser
  - なぜ要るか：学会や企業イベントの限定公開を、セッションごとの URL 一覧付きで納品できる
  - Done when: a mock run of three sessions writes a receipt with three URLs and their privacy

## Boundaries

Platform delivery specs (配信プラットフォーム仕様) are owned by ffmpeg-skill (owner decision, 2026-10-30); this skill reads them and owns only upload-side limits (アップロード制限).

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (3.x, 42 tools, contract_version 2.0; treated as complete, proposals go to Velisa's ROADMAP). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).

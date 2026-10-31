# Roadmap — publish-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **納品 (Delivery)**, status `scaffold`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

Scaffold (0.1.0, [#4](https://github.com/kajisho5/publish-skill/pull/4)): `contract`, `doctor`, `platforms` and the dry run of `upload` behind a per-deliverable confirmation token (ADR-001). `publish plan` ([#5](https://github.com/kajisho5/publish-skill/pull/5), ADR-002): the deliverable and its metadata against YouTube's cited upload limits and ffmpeg-skill's `check`, offline; the token binds the metadata. Credentials ([#6](https://github.com/kajisho5/publish-skill/pull/6), ADR-003): the environment or the OS keychain (`publish auth login|status|logout`, keyring as an optional extra), never printed. YouTube uploader ([#7](https://github.com/kajisho5/publish-skill/pull/7), ADR-004): the Data API v3 resumable upload of the video, thumbnail and captions after the exact confirmation token, tested against a loopback mock only; no real upload has been made yet. Vimeo uploader ([#8](https://github.com/kajisho5/publish-skill/pull/8), ADR-005): the tus resumable upload of the video with its title, description and privacy (`nobody` by default) after the exact confirmation token, with a personal access token, tested against a loopback mock only. TikTok uploader ([#9](https://github.com/kajisho5/publish-skill/pull/9), ADR-006): the Content Posting API Direct Post (creator info, init with FILE_UPLOAD, chunked PUT, the post's status) with its title and privacy (`SELF_ONLY` by default) after the exact confirmation token, with a user access token, tested against a loopback mock only.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| 配信先アップロード | YouTube ([#7](https://github.com/kajisho5/publish-skill/pull/7)), Vimeo ([#8](https://github.com/kajisho5/publish-skill/pull/8)), TikTok ([#9](https://github.com/kajisho5/publish-skill/pull/9)) | further platforms: step 5 |
| アップロード制限 | YouTube ([#5](https://github.com/kajisho5/publish-skill/pull/5)), Vimeo ([#8](https://github.com/kajisho5/publish-skill/pull/8)), TikTok ([#9](https://github.com/kajisho5/publish-skill/pull/9)) | upload-side limits only (file size, duration, rate limits, metadata fields); the platform delivery specs belong to ffmpeg-skill `_platforms.py` -- read them, do not copy them |

## Wiring

None, and the agent has no SkillSpec for publishing.

## Steps

- [x] **1. Scaffold** — package, contract/doctor (network: true), SKILL.md, CI on 3 OSes ([#4](https://github.com/kajisho5/publish-skill/pull/4))
  - Done when: contract validates against the OS schema
- [x] **2. `publish plan` (offline)** — validate deliverable + title/description/tags/privacy/thumbnail/captions against platform limits; encode spec from ffmpeg-skill export --list / check ([#5](https://github.com/kajisho5/publish-skill/pull/5))
  - Done when: structured FAIL on an over-long title or wrong aspect, no network
- [x] **3. Credentials** — `publish auth login|status|logout`, OS keychain, secrets redacted everywhere ([#6](https://github.com/kajisho5/publish-skill/pull/6))
  - Done when: no token in stdout/stderr/receipt
- [x] **4. YouTube uploader** — Data API v3 resumable (video, thumbnail, captions); dry-run default, `--confirm` required ([#7](https://github.com/kajisho5/publish-skill/pull/7))
  - Done when: mock-server tests incl. resume
- [ ] **5. Further platforms** — one PR each
  - Done when: mock-server tests
  - [x] Vimeo — tus resumable upload, personal access token, title / description / privacy, cited limits; mock-server tests incl. resume and quota ([#8](https://github.com/kajisho5/publish-skill/pull/8), ADR-005)
  - [x] TikTok — Content Posting API Direct Post (FILE_UPLOAD, chunked PUT, post status), user access token, title / privacy, cited limits; mock-server tests incl. resume and the daily post cap ([#9](https://github.com/kajisho5/publish-skill/pull/9), ADR-006)
- [ ] **6. (agent side) SkillSpec publish_upload** — always CONFIRM, after QC
  - Done when: plan shows a blocked publish step

## Boundaries

Platform delivery specs (配信プラットフォーム仕様) are owned by ffmpeg-skill (owner decision, 2026-10-30); this skill reads them and owns only upload-side limits (アップロード制限).

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).

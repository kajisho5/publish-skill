# Roadmap — publish-skill

Part of the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS/blob/main/docs/ecosystem/ROADMAP.md) ecosystem: department **納品 (Delivery)**, status `scaffold`.
Written from a read-only audit of this repository on 2026-10-30. Each step below is meant to be one pull request; tick it off by linking the PR.

## Where it stands

Scaffold: README with `# publish` only.

## What this repository owns

`ecosystem.json` gives each job to exactly one repository. Work outside this list belongs to its owner and is handed over, not re-implemented.

| Owned job | Status | Note |
|---|---|---|
| 配信先アップロード | missing |  |
| 配信プラットフォーム仕様 | missing | the platform table already lives in ffmpeg-skill `_platforms.py`; read it, do not copy it |

## Wiring

None, and the agent has no SkillSpec for publishing.

## Steps

- [ ] **1. Scaffold** — package, contract/doctor (network: true), SKILL.md, CI on 3 OSes
  - Done when: contract validates against the OS schema
- [ ] **2. `publish plan` (offline)** — validate deliverable + title/description/tags/privacy/thumbnail/captions against platform limits; encode spec from ffmpeg-skill export --list / check
  - Done when: structured FAIL on an over-long title or wrong aspect, no network
- [ ] **3. Credentials** — `publish auth login|status|logout`, OS keychain, secrets redacted everywhere
  - Done when: no token in stdout/stderr/receipt
- [ ] **4. YouTube uploader** — Data API v3 resumable (video, thumbnail, captions); dry-run default, `--confirm` required
  - Done when: mock-server tests incl. resume
- [ ] **5. Further platforms** — one PR each
  - Done when: mock-server tests
- [ ] **6. (agent side) SkillSpec publish_upload** — always CONFIRM, after QC
  - Done when: plan shows a blocked publish step

## Boundaries

Platform spec ownership vs ffmpeg-skill `_platforms.py` -- owner decision needed.

---
Engine for every media operation: [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill) (42 tools, treated as complete). Orchestrator: [video-production-agent](https://github.com/kajisho5/video-production-agent).

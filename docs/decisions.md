# Architecture Decision Records

## ADR-001 Scaffold: nothing uploads by default, a confirmation token per deliverable, credentials from the environment only, uploaders as stubs

### Context

Roadmap step 1 asks for the package, `contract` / `doctor` with `network: true`, SKILL.md and CI on three operating systems, done when the contract validates against the OS schema. This is the first skill in the ecosystem whose job is to send something outside the machine. A wrong upload is public and cannot be taken back, so the safety rules are set in the scaffold, before any uploader exists. The OS roadmap records publishing as "CONFIRM after QC".

Step 2 (`publish plan`) was not folded into this PR. It needs the ffmpeg-skill adapter (capability detection of `export --list` / `check`) and a table of upload-side limits for each platform, gathered from each platform's documentation. That is a separate piece of work, not a small addition to the skeleton.

### Decision

- **Stdlib-only core, Python >= 3.9**, the same layout as voice-enhance-skill ADR-002: `pip install publish-skill` installs no third-party package. The CLI is `publish`.
  - `publish contract --json` (alias `skill --json`)
  - `publish doctor --json [--offline] [--ffmpeg-skill DIR]`
  - `publish platforms --json`
  - `publish upload FILE --platform ID [--confirm TOKEN] --json`
  - `publish plan FILE --platform ID --json` (NOT_IMPLEMENTED until step 2)

  With `--json`, stdout holds exactly one JSON document, on success and failure alike.
- **Contract in the OS registry shape**: `skill_id`, `version`, `contract_version`, `provides`, `dependencies` with `version_range`, and `not_provided`. `skill` is an object (`id`, `version`, `package`), because the OS schema types `skill` as an object; a test validates the contract against `registry/capability_contract.schema.json`.
  - `publish.plan` and `publish.upload` are published with lifecycle `PROPOSED`, and their tools with `implemented: false`. Each moves to `EXPERIMENTAL` in the PR that makes it work.
  - `execution.network` is `true`, and `network_tools` lists `publish/upload` alone. `network_requires` is `--confirm <token>`, and `network_in_this_release` is `false`.
- **Dry run is the default, and the only path that completes.** `publish upload` without `--confirm` reads the deliverable (size, sha256), reports the platform, the credential status and a `confirm_token`, and opens no connection (`dry_run: true`, `uploaded: false`, `network_used: false`). There is no `--yes`, and nothing turns confirmation on implicitly: no environment variable, config file or default does it.
- **The confirmation token binds the approval to the bytes.** `confirm_token` is `confirm-` + 16 hex characters of the sha256 of the skill id, the platform id, the deliverable's sha256 and its size.
  - The token printed by a dry run cannot upload a re-exported file, or the same file to another platform. Either is refused as `CONFIRM_MISMATCH` (exit 2), and nothing is sent.
  - The token is compared in constant time.
  - It is not a secret and not an authorisation. It proves that the caller saw the dry run for exactly this deliverable. The decision to publish stays with the human, recorded by the agent (roadmap step 6: SkillSpec `publish_upload`, always CONFIRM, after QC).
  - Step 2 adds the metadata (title, description, tags, privacy, thumbnail, captions) to the hashed fields, so editing any of it after the dry run also needs a new token.
- **Credentials from the environment only, never printed.**
  - Each platform names its variables (YouTube: `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET`, `PUBLISH_YOUTUBE_REFRESH_TOKEN`).
  - Nothing reads a credential from a file, a flag or a request field. A flag would leak into shell history and process listings.
  - Reports say `present` or `absent` per variable, never the value or its length. A blank value counts as absent.
  - `CREDENTIALS_MISSING` names the missing variables only.
  - The `Credentials` object's `repr` hides its values.
  - As a second line of defence, `credentials.redact` replaces every credential value inside every string value of every document the CLI prints (JSON and human output alike) with `[REDACTED]`. It does this however short the value is. Keys are left alone, because they come from this code and the CLI reads them.
  - Step 3 adds the OS keychain (`publish auth login|status|logout`).
- **Order of the gate:** platform known → deliverable is a regular file → token matches → credentials present → uploader. Every refusal happens before any credential is read or any connection could be made.
- **Uploaders are stubs.** `platforms/` is a registry, like voice-enhance-skill's engine registry; only registered ids can be named.
  - YouTube is registered for roadmap step 4, and its `upload` raises `NOT_IMPLEMENTED` (exit 5, `uploaded: false`).
  - The scaffold contains no network code at all. A unit test forbids `socket`, `urllib.request`, `http.client`, `requests`, `httpx` and `subprocess` in `src/`, and every unit test runs with socket connect and name lookup patched to fail.
  - Step 4 relaxes the source rule for its uploader module only, and tests the uploader against a mock server.
  - Each further platform is one PR (step 5).
- **ffmpeg-skill is located, not used yet.** `doctor` looks for a checkout (`--ffmpeg-skill`, `PUBLISH_FFMPEG_SKILL_DIR`, `VIDEO_AGENT_FFMPEG_SKILL_DIR`, `~/.claude/skills/ffmpeg-skill`, `./vendor/ffmpeg-skill`, `../ffmpeg-skill`) and reports `OK` or `MISSING`.
  - The contract declares it as an optional dependency for `publish.plan`, needed from step 2, with a capability-detection `version_range`.
  - Platform delivery specs stay in ffmpeg-skill, which owns them (owner decision, 2026-10-30). This skill reads them and never copies them.
- **Typed errors.** `PublishError` carries a code from a fixed table that decides the exit code and the `retryable` flag: 2 refused, 3 credentials missing, 5 not implemented, 6 cancelled, 7 internal. Only `CANCELLED` is retryable today. Step 4 adds upload failures, such as rate limits and interrupted resumable sessions.
- **Doctor status.**
  - `ok`: every platform is implemented with complete credentials, and ffmpeg-skill is found.
  - `degraded`: a platform is not implemented or has no credentials, or ffmpeg-skill is not found. The skill itself works, so the exit code is 0. This is the status of this release everywhere.
  - `fail`: Python is too old (exit 1).
  - Doctor never uses the network. `--offline` is accepted, so the agent can pass it unconditionally.
- **CI.**
  - `unit`: Ubuntu 24.04, macOS and Windows, each with Python 3.9 and 3.12. It runs pyflakes, the unit tests, `contract`, `doctor` (asserting `degraded` and youtube not ready) and a dry-run upload.
  - `conformance`: a read-only checkout of AI-video-production-OS plus `jsonschema`. It runs every test, including the registry conformance checks and the schema validation, and fails if any test is skipped.

### Follow-ups

- Step 2: `publish plan` (offline), the ffmpeg-skill adapter, upload-side limits per platform, and metadata in the confirmation token.
- Step 3: credentials in the OS keychain, with redaction kept on every output path, including receipts.
- Step 4: the YouTube resumable uploader, with mock-server tests including resume.
- Step 6 (agent side): SkillSpec `publish_upload`, always CONFIRM, after QC.

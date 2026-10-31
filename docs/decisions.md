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

## ADR-002 `publish plan`: an upload-limit table with a cited source per value, ffmpeg-skill's delivery spec through `check`, metadata in the confirmation token

### Context

Roadmap step 2 asks for `publish plan`, offline. It validates the deliverable and its title, description, tags, privacy, thumbnail and captions against the platform's limits, and encodes the spec from ffmpeg-skill `export --list` / `check`. It is done when it gives a structured FAIL on an over-long title or a wrong aspect, with no network.

Two repositories hold limits that apply to one upload. `ecosystem.json` gives the upload-side limits (file size, duration, metadata fields, rate limits) to this skill, and the platform delivery specs (codecs, resolution, aspect, loudness) to ffmpeg-skill, in `scripts/_platforms.py` (owner decision, 2026-10-30). ADR-001 also left two items for this step: metadata in the confirmation token, and the ffmpeg-skill dependency's capability detection.

### Decision

- **One table of upload-side limits per registered platform, in `limits.py`, every value cited.**
  - Each documented entry carries `value`, `unit`, `source` (the platform's own page), `accessed` (the date it was read) and `quote` (the sentence the value rests on).
  - A limit the documentation does not state is recorded as `status: "unknown"`, with `value: None` and the pages searched, and nothing is checked against it. No value is guessed. Per-tag length, tag count and the per-user upload count are unknown for YouTube today.
  - A limit ffmpeg-skill owns is recorded as `status: "delegated"`, with no value: codec, resolution, aspect and frame rate. Its value is never restated here.
  - Where a page writes "GB" or "MB" without saying decimal or binary, the decimal (smaller) reading is used, so a file `plan` passes is under the limit either way. `documented_as` keeps the page's own words.
  - A recommendation is recorded with `checked: false`, such as the 640 px minimum thumbnail width. So is a quota, which `plan` cannot see.
  - YouTube's table was read on 2026-10-31 from the Data API v3 reference (videos, videos.insert, thumbnails.set, captions.insert) and the YouTube Help Center (upload length, supported formats, caption formats, thumbnails). The README has the table. `contract --json` publishes it under `upload_limits`. Tests check that every documented value cites an official page and a date, that the researched YouTube values are unchanged, and that the README lists every key.
  - YouTube is the only registered platform, so it is the only table. Each further platform brings its own table in its own PR (step 5), and a test requires one table per registered platform.
- **`publish plan FILE --platform ID [metadata flags] [--ffmpeg-skill DIR] --json`.** It prints one row per limit. Each row has `check`, `status`, `value`, `limit` and `owner`, plus `source` / `accessed` for this skill's limits and a `fix` when the row does not pass.
  - Row status: `PASS`, `WARN`, `FAIL` or `UNCHECKED`.
  - The verdict is `FAIL` if any row fails, otherwise `INCOMPLETE` if any row is unchecked, otherwise `WARN` or `PASS`.
  - `ok` is true for `PASS` and `WARN` only, and the CLI exits 1 otherwise. A failed plan is a result, not an error, so it is a normal document like `doctor`'s `fail`, not the error shape.
  - Missing ffmpeg-skill never yields `PASS`. The delivery spec rows and the duration row are `UNCHECKED`, so the verdict is `INCOMPLETE`.
  - `plan` reads no credential, opens no connection and uploads nothing (`uploaded: false`, `network_used: false`).
  - The capability moves to `EXPERIMENTAL` and its tool to `implemented: true`, as ADR-001 planned. `upload` stays `PROPOSED`.
- **ffmpeg-skill's delivery spec is read by running ffmpeg-skill (`adapter.py`).** This follows color-grading-skill's adapter.
  - Capability detection: `scripts/_contract.py --json --static` must declare `export` with `--list`, and `check` with `--platform` (its enum must contain the platform's `ffmpeg_skill_platform`), `--json` and `--no-loudness`. It must also declare `execution.network: false`. Then `export.py --list` gives the presets, and `plan` names the one that re-exports for the platform.
  - There is no version window. A checkout is usable when those flags are present. Otherwise it is `UNSUPPORTED`, and `doctor` and `plan` say why.
  - The deliverable is checked with `check.py <file> --platform <p> --json --no-loudness`. Its rows pass through unchanged with owner `ffmpeg-skill`. The duration it measured is checked again against this skill's own limits: 12 hours FAIL, and 15 minutes WARN, because `plan` cannot tell whether the account is verified.
  - Loudness is skipped. It is not an upload-side limit, it costs a full decode, and QC and ffmpeg-skill `export --normalize` own it.
  - A missing tool, a timeout, or output that is not JSON becomes an `UNCHECKED` row with the reason. It never becomes a guess, and never a crash.
- **`adapter.py` is the only module that starts a process.** ADR-001's source rule is narrowed for that one file, the way ADR-001 planned to narrow it for step 4's uploader.
  - The unit test still bans `subprocess` in every other module, and adds `os.popen`, `os.exec*` and `os.spawn*` to the banned list everywhere. It requires `adapter.py` to be the only file that mentions `subprocess`.
  - A second test walks the adapter's AST. It allows one `subprocess.run` call, whose first argument is the argv list built above it (never a string) and whose `shell` is the constant `False`, with `timeout`, `env` and `stdin` set.
  - The child gets a minimal environment, so credential variables never reach ffmpeg-skill. stdin is closed.
  - The socket guard on every test is unchanged. The only processes tests start are fake ffmpeg-skill scripts, plus, in the integration tests, the real ffmpeg-skill, whose contract declares no network use. The adapter refuses a checkout whose contract does not declare that.
- **The confirmation token binds the metadata.** `confirm-` + 16 hex characters of the sha256 of the skill id, the platform id, the deliverable's sha256 and size, and the metadata's canonical JSON.
  - The canonical JSON covers title, description, tags (in order), privacy, the thumbnail's sha256 and size, and each caption file's sha256 and size (in order).
  - Files are bound by their bytes, not their path, like the deliverable. Editing any field after the dry run, dropping one, or replacing the thumbnail gives `CONFIRM_MISMATCH`.
  - A dry run without metadata flags has the default metadata, so `confirm_token(platform, sha256, size)` is unchanged in meaning.
  - `upload` takes the same metadata flags as `plan`. Before a token is printed or checked, it refuses metadata that FAILs a limit, as `METADATA_INVALID` (exit 2, not retryable, `details.failed` lists the rows). A token is therefore never issued for metadata the platform would reject.
- **Privacy defaults to `private`.** The YouTube API makes a video `public` when the field is left out. This skill always sends one, so a forgotten flag cannot publish to everyone.
- **CI.** The `conformance` job also checks out ffmpeg-skill (read-only) and installs FFmpeg. The integration tests then run against the real `check`, and nothing may skip. The `unit` job runs a plan with an over-long title and no ffmpeg-skill, and asserts the structured FAIL.

### Follow-ups

- Step 3: credentials in the OS keychain.
- Step 4: the YouTube uploader sends exactly the metadata the token bound, and reports `uploadLimitExceeded` and quota errors as typed, retryable or not.
- Step 5: each platform brings its limit table (cited, unknowns marked) and its `ffmpeg_skill_platform`.

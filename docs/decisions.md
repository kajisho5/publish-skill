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

## ADR-003 Credentials in the OS keychain: `publish auth status|login|logout`, the environment first, `keyring` as an optional extra

### Context

Roadmap step 3 asks for `publish auth login|status|logout`, the OS keychain, and secrets redacted everywhere. It is done when no token appears in stdout, stderr or a receipt. ADR-001 read credentials from the environment only and left the keychain for this step. Its rules stay: values are never printed or logged, reports say `present` / `absent`, the redactor runs over every output, every refusal comes before any credential is read, and nothing uses the network.

Python's standard library has no keychain API. Each OS has its own: the Security framework on macOS, the Credential Manager on Windows, and the Secret Service D-Bus API on Linux. The `keyring` library wraps all three, so the choice was between that library and three hand-written bindings.

### Decision

- **The OS keychain through `keyring`, as an optional extra.**
  - `pip install 'publish-skill[keychain]'` installs `keyring>=25.0`. The core install stays stdlib only (ADR-001), and `dependencies` stays empty.
  - Licences: keyring is MIT. Its dependencies are MIT (jaraco.classes, jaraco.functools, jaraco.context, more-itertools, jeepney, backports.tarfile), BSD-3-Clause (SecretStorage, pywin32-ctypes) or Apache-2.0 (importlib_metadata, and cryptography, which is Apache-2.0 OR BSD-3-Clause). All were checked on PyPI on 2026-10-31.
  - Minimum version: keyring 25.0 and 25.7.0 (the latest on 2026-10-31) were both installed and checked. Each has the three backend classes used here, their `viable` and `priority`, and `get_password` / `set_password` / `delete_password`. keyring 25.0 needs Python >= 3.8, so the extra works on every Python this package supports.
  - Without keyring, the keychain is `UNAVAILABLE`, and credentials come from the environment only. `doctor` reports `checks.keychain` with the reason and the install command, and adds a warning, so the status is `degraded`. `auth login` and `auth logout` refuse with `KEYCHAIN_UNAVAILABLE` (exit 3).
- **Only the three OS keychains.** `keychain.py` imports `keyring.backends.macOS`, `.Windows` and `.SecretService`. Of the viable ones, it uses the one with the highest priority.
  - keyring's own backend choice is never consulted. That covers `keyring.get_keyring()`, `PYTHON_KEYRING_BACKEND`, `keyringrc.cfg`, and plug-in backends found through entry points. A plug-in could keep secrets in a plain file (ADR-001: never from files) or in a network store (no network).
  - The Secret Service is local D-Bus IPC, not a network connection.
  - If no OS keychain is usable (on Linux, for example, there is no D-Bus session or the keyring is locked), the keychain is `UNAVAILABLE` with the reason.
  - `PUBLISH_KEYCHAIN=off` (or `0`, `false`, `no`) turns the keychain off. It is meant for headless machines and CI. It can only narrow the sources, never widen them.
- **Where values live.** Each value is an item of the service `publish-skill`. Its account (user name) is the variable's name, for example `PUBLISH_YOUTUBE_REFRESH_TOKEN`. The names are the platform's `credential_env`, unchanged, so the environment and the keychain hold the same variables.
- **Precedence: the environment first, per platform, as a whole.**
  - If any of a platform's variables is set in the environment, the environment is that platform's only source, and the keychain is not consulted. Otherwise the keychain is the source.
  - So one platform's values never mix sources. A refresh token belongs to the OAuth client that issued it. A client id from the environment paired with a refresh token from the keychain would fail at the platform, with an error that names neither.
  - With a partial environment, `CREDENTIALS_MISSING` names the missing variables, gives `source: "environment"`, and says that the keychain is not consulted.
  - The environment comes first because it is the explicit, per-process choice, and CI and agents set it. A value stored once in the keychain must not silently override it. This follows common practice, such as `GH_TOKEN` over gh's stored token.
  - `status` (and so `doctor`, `platforms` and the dry run) reports, for each platform, the chosen `source`, `variables` and `complete` for that source, and the presence of each variable in `environment` and in `keychain`. It adds a `note` when the environment shadows values in the keychain. All values are `present` or `absent`, never a value or a length.
- **`publish auth`.**
  - `status [--platform ID]` reports the keychain's availability and each platform's credential status.
  - `login --platform ID [--variable NAME ...]` stores values the user already has. It reads them from stdin, one per line, in the order of the platform's variables (or of `--variable`). When stdin is a terminal, it uses `getpass` instead: a prompt on stderr that does not echo.
    - No flag or positional argument takes a value. A test checks the parser, and checks that `--value`, `--token`, `--password` and a positional are rejected. A value on the command line would reach shell history and process listings.
    - Every value is read and checked (none may be empty or missing) before the first one is stored.
    - A store failure gives `KEYCHAIN_ERROR` with the names already stored.
    - If the environment shadows the stored values, the result says so.
  - `logout --platform ID [--variable NAME ...]` removes items. It reports `removed` and `not_found` by name, and warns when the environment still sets a variable.
  - `login` does not run Google's OAuth consent flow. That flow needs the network and a browser. Whether and how to add it is left to step 4.
- **The safety rules, kept.**
  - Never printed or logged. Nothing in the package logs. Keychain errors carry the backend's name, the operation, the variable's name and the exception's type, never the backend's message. The `Credentials` repr now also shows the source, and still no value.
  - The redactor backstop stays, and it still covers every credential variable in the environment. It now also covers every value this process has read from the keychain or been given by `auth login`, which `credentials.hold` records.
  - The redactor does not read the keychain itself. Reading on every output would read credentials before refusals and on commands that need none (`contract`, `plan`). A value this process never held cannot be in its output.
  - Refusals come first. The upload gate is unchanged: platform → file → metadata → token → credentials (ADR-001, ADR-002). `auth` refuses an unknown platform, an unknown or repeated variable, and an unavailable keychain before it reads stdin or touches the keychain. Tests call each refusal with the environment read, the keychain backend and stdin all replaced by probes that fail on use. A control case shows that the probes fire once the gate is passed.
  - No network. keychain.py and auth.py are under ADR-001's source rule (no socket, HTTP or process code). The socket guard on every test is unchanged.
- **Errors.** `KEYCHAIN_UNAVAILABLE` and `KEYCHAIN_ERROR` are added, both exit 3 (the credentials class) and not retryable. `CREDENTIALS_MISSING` now means "in neither source".
- **Tests never touch a real keychain.**
  - `keychain._import_keyring` is the package's only import of keyring. A test walks every module's AST to check this, and that nothing calls `get_keyring`, `set_keyring`, `import_module` or `__import__`.
  - `tests/helpers.py` replaces `_import_keyring`, for the whole test process, with a guard that raises `RealKeychainTouched`. That is a `BaseException`, so no `except Exception` in the package can swallow it.
  - Every `OfflineTestCase` gets a fresh in-memory fake backend with keyring's three methods, so the real wrapper code runs against it. The test also unsets `PUBLISH_KEYCHAIN`.
  - Backend selection is tested on stand-in classes, never the installed ones.
  - `KeychainGuardTests.test_the_fake_keychain_is_what_runs` asserts that the fake is the active keychain, and that the guard is in place and raises. In jobs that install the extra (`PUBLISH_EXPECT_KEYRING=1`), it also asserts that the real keyring is installed there, and is still unreachable.
  - CI fails if that test did not run and pass.
- **CI.**
  - `unit` installs the `keychain` extra on Python 3.12 and not on 3.9, on all three OSes. So both the env-only fallback and the extra are exercised everywhere. The `doctor` step asserts the fallback: without the extra, the keychain is `UNAVAILABLE` because keyring is not installed, and the warning says "environment only". With the extra, the job sets `PUBLISH_KEYCHAIN=off` for its command-line steps, so even they never read a runner's real keychain.
  - `conformance` installs the extra, too. Nothing may skip there, as before.

### Follow-ups

- Step 4: the uploader gets its credentials from `credentials.load` only. Every document it writes, including the upload receipt, goes through `redact`. Its tests repeat the no-value-in-output checks on the receipt. Step 4 also decides whether `auth login` should run the OAuth consent flow (network, browser) or keep storing a refresh token obtained elsewhere.
- Step 5: each platform's variables become keychain items under the same names, with no new code.

## ADR-004 The YouTube uploader: the Data API v3 resumable protocol over stdlib `http.client`, exactly the bound request, tested against a loopback mock only

### Context

Roadmap step 4 asks for the YouTube uploader: Data API v3 resumable upload of the video, the thumbnail and the captions, dry run by default, `--confirm` required. It is done when mock-server tests, including resume, pass. ADR-001 left this step to relax the "no network code" rule for its uploader module only; ADR-002 asked that the uploader send exactly the metadata the token bound and report quota errors as typed errors; ADR-003 asked that its receipts go through `redact`, with the no-value-in-output tests repeated on them, and that this step decide on the OAuth consent flow.

This is the first code in the ecosystem that publishes something. A real upload needs the owner's credentials and their confirmation, so none was made: not by this PR, not by its tests, not by CI.

### Protocol sources (read 2026-10-31)

- OAuth 2.0, refreshing an access token: https://developers.google.com/identity/protocols/oauth2/native-app (section "Refreshing an access token"). `POST https://oauth2.googleapis.com/token`, form-encoded `client_id`, `client_secret`, `refresh_token`, `grant_type=refresh_token`; the JSON answer has `access_token`, `expires_in`, `scope` and `token_type`. `invalid_grant` means the refresh token expired or was revoked.
- The resumable upload protocol: https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol. Start: `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=...` with `Authorization`, `Content-Type: application/json; charset=UTF-8`, `X-Upload-Content-Length`, `X-Upload-Content-Type` and the resource; `200 OK` with the session URI in `Location`. Upload: `PUT` to the session URI. Chunks: each a multiple of 256 KB except the last, the same size each, with `Content-Range: bytes FIRST-LAST/TOTAL`; `308` with `Range: bytes=0-N` after each, and the next chunk starts after N. Completion: `201 Created` (or 200) with the resource. Resumable failures: no answer, or 500, 502, 503, 504, with exponential backoff. Status: `PUT` with `Content-Length: 0` and `Content-Range: bytes */TOTAL`; `308` and a `Range` header (none: nothing received), or the completion answer; `Retry-After` is honoured. Resume from the byte after the `Range`. After an interruption, check the status rather than assume all or none of the chunk arrived. `404` means the session expired: start a new one, from the beginning. Other 4xx are permanent.
- The retry loop of the official sample: https://developers.google.com/youtube/v3/guides/uploading_a_video (retriable 500, 502, 503, 504 and connection errors; at most 10 retries; sleep `random() * 2 ** retry`).
- The methods: https://developers.google.com/youtube/v3/docs/videos/insert (scopes, `video/*` and `application/octet-stream`, 256 GB, `status.privacyStatus` defaults to `public`, `uploadLimitExceeded` is 400), https://developers.google.com/youtube/v3/docs/thumbnails/set (`videoId`, `image/jpeg`, `image/png`, 50 MB), https://developers.google.com/youtube/v3/docs/captions/insert (`part=snippet`; `snippet.videoId`, `snippet.language` and `snippet.name` must be given; scope `youtube.force-ssl` or `youtubepartner`) and https://developers.google.com/youtube/v3/docs/captions (`snippet.language` is a BCP-47 tag; `isDraft` defaults to false).
- The discovery document, https://www.googleapis.com/discovery/v1/apis/youtube/v3/rest (revision 20261029): `mediaUpload.protocols.resumable` exists for `videos.insert`, `thumbnails.set` and `captions.insert`, so one protocol serves all three.
- Errors: https://developers.google.com/youtube/v3/docs/errors (`quotaExceeded` is 403; `uploadLimitExceeded` is 400, the channel's daily limit, separate from the project's quota; `uploadRateLimitExceeded` on thumbnails.set is 429, "try the request again later"). The body is Google's JSON error, `{"error": {"code", "message", "errors": [{"reason", ...}]}}`.

### Decision

- **One module holds the network code: `src/publish_skill/platforms/youtube.py`.** ADR-001's source rule now exempts exactly that file, and only for `http.client` and `ssl`. A test requires it to be the only file that mentions `http.client`, keeps every other network and process word banned everywhere (including `import socket`, `urllib.request`, `urlopen`, `create_connection` there too), and walks its AST: its imports are stdlib only, it opens one `HTTPSConnection` (with a verifying `ssl.create_default_context()`) and one `HTTPConnection`, both with a timeout. Plain HTTP is allowed only to `127.0.0.1`, `::1` or `localhost`, which is how the tests reach their mock; any other endpoint is refused before a connection.
- **The standard library only.** `http.client`, not `urllib.request`: the protocol answers an incomplete upload with `308` and a `Range` header, which urllib's redirect handler treats as a redirect, and the uploader needs its own retry and status logic around every request. No dependency is justified: the protocol is a handful of requests with fixed headers.
- **Endpoints are code, not configuration.** `token_url`, `api_base`, chunk size, timeout and retry limits are class attributes. No flag, environment variable or file changes them; the tests patch them to the mock. The session URI must be on the API's own scheme, host and port, so the bearer token is never sent anywhere else.
- **Order inside the uploader**, after ADR-001's gate (platform, file, metadata, token, credentials) has passed:
  1. The access token. A refused refresh token (400/401/403 from the token endpoint) is `AUTH_FAILED` with Google's error code (`invalid_grant`), never a value. `auth login` keeps storing a refresh token obtained elsewhere: running the consent flow would need a browser and a local redirect listener, a second network surface for a one-time step, so it stays out (ADR-003 follow-up, decided).
  2. Scopes. When the token answer lists its scopes and none that a step accepts is granted (videos.insert and thumbnails.set: `youtube.upload`, `youtube`, `youtube.force-ssl` or `youtubepartner`; captions.insert: `youtube.force-ssl` or `youtubepartner`), the upload stops with `AUTH_FAILED` before any byte of the video is sent, rather than leaving a video without its captions.
  3. `videos.insert`, then `thumbnails.set` with the new id, then `captions.insert` once per caption file, each a resumable session.
- **Exactly the bound request.**
  - The video resource is built from the bound metadata alone: `snippet.title`, `snippet.description` and `snippet.tags` when set, and always `status.privacyStatus`. Nothing is added (no category, language or other default), and `part` names exactly the parts sent. The default privacy stays `private` (ADR-002).
  - Captions need a language that ADR-002's metadata did not have. `--caption-language` (one per `--caption`, in order) adds it to each caption entry, and the token binds it with the file's bytes. `plan` checks it (`captions.language`, cited from the captions resource page), so `upload` refuses captions without one as `METADATA_INVALID` before a token is printed. The track's `name` is its language tag and `isDraft` is false (the video's privacy governs who sees it), so nothing unbound is sent.
  - Every file is hashed as its bytes are confirmed by the session. The last chunk is held back unless the bytes so far plus that chunk have the sha256 the token bound. YouTube creates the video only when the last byte arrives, so a file edited between the gate and the end of the upload is never published (`CONFIRM_MISMATCH`). The thumbnail's and captions' bytes get the same check.
  - The receipt carries `sent` (the exact resources), and a test rebuilds `Metadata` from what reached the mock (resource, bytes, languages) and gets the very token that was confirmed.
- **Chunks, resume, retries, timeouts.**
  - Chunks are 8 MiB (32 × 256 KiB); a chunk size that is not a multiple of 256 KiB is refused.
  - Each request has a 60 s timeout. No answer, a reset, a timeout, 429, 500, 502, 503, 504, or a 400/403 whose reason is `rateLimitExceeded`, `userRateLimitExceeded` or `uploadRateLimitExceeded` is retried: the wait is `random() × 2^n` seconds (the sample's formula), at most 64 s, or `Retry-After` when longer, up to 5 minutes; at most 10 retries per request. 429 is retried although the sample lists only 5xx, because the errors page says to try `uploadRateLimitExceeded` (429) again later.
  - After a failed chunk the session's status is asked, and the upload resumes from the byte after its `Range`. Bytes the session confirms must come from the chunk just sent; anything else is a protocol error (`UPLOAD_REJECTED`). A 308 that kept none of a chunk counts as a retry, so a server that never progresses cannot loop forever.
  - `404` from a session starts a new session from the beginning, at most twice.
  - An interrupted run is not resumed by a later one: each run starts a new session, and an unfinished resumable upload creates no video. Keeping a session URI on disk would put an upload credential in a file (ADR-001).
- **Typed errors** (added to the table; exit 4 is the upload class):
  - `AUTH_FAILED` (exit 3, not retryable): the refresh token refused, a missing scope, or 401 / `authError` / `insufficientPermissions` from the API.
  - `QUOTA_EXCEEDED` (4, retryable after the quota resets): `quotaExceeded` (or `dailyLimitExceeded`). Never retried within the run.
  - `UPLOAD_LIMIT_EXCEEDED` (4, retryable later): `uploadLimitExceeded`, the channel's daily limit.
  - `UPLOAD_INTERRUPTED` (4, retryable): still failing after every retry, or the session expired three times. Nothing was published.
  - `UPLOAD_REJECTED` (4, not retryable): any other 4xx, or a protocol error (bad `Range`, a session URI on another host, completion before the last chunk, no video id).
  - `UPLOAD_PARTIAL` (4, not retryable): the video is uploaded but its thumbnail or captions failed. `details` has `video_id`, `url`, the failed step and its cause. Running `upload` again would upload the video twice, so the message says not to.
  - Details carry the HTTP status, the error reasons and Google's message (at most 300 characters), never a header or a token.
- **Redaction.** The access token and each session URI (which carries the upload id) are recorded with `credentials.hold`, so `redact` removes them like the credentials. `upload()` itself passes the receipt and any error through `redact` before returning or raising, and the CLI redacts again. The tests repeat ADR-003's no-value-in-output checks on receipts and on every failure kind, in JSON and human output, with the credentials from the environment and from the keychain, and assert that neither the access token nor any upload id appears.
- **Contract and doctor.** `publish.upload` is `EXPERIMENTAL`; the YouTube platform is `implemented: true`; `execution.network_in_this_release` is true, with `network_code` (the one module), `network_hosts` (`oauth2.googleapis.com`, `www.googleapis.com`) and `upload_protocol`. `doctor` reports YouTube as ready when its credentials are complete.
- **Tests never contact Google.**
  - `tests/mock_youtube.py` is an in-process `ThreadingHTTPServer` on `127.0.0.1`, port 0. It implements the token endpoint and the resumable protocol (contiguity, the 256 KiB rule, 308 and `Range`, the status query, 201/200 completion) for the three methods, and scripts faults per request: an error status with a reason, an interrupted chunk that keeps part of its bytes and drops the connection, a stall (the client's timeout), an expired session, and a callback (editing the file mid-upload).
  - `OfflineTestCase`'s socket guard now lets a test connect to, or resolve, only `127.0.0.1` on the port of a mock it started itself; anything else, Google included, raises `NetworkAttempted`. That is a `BaseException`, so neither the uploader's error handling nor the CLI's `except Exception` can turn it into an error document. Tests assert that the guard blocks Google's hosts, other loopback ports and `localhost`, and that a confirmed upload without the mock is stopped at `oauth2.googleapis.com`. CI fails unless `test_the_guard_allows_only_the_mock_this_test_started` ran and passed.
  - No workflow uploads anything. No real upload was made for this PR.

### Follow-ups

- The first real upload, by the owner, with their credentials and their confirmation: check the receipt against YouTube Studio (privacy, title, thumbnail, captions).
- An HTTP(S) proxy (CONNECT tunnel) for networks that need one; direct connections only for now.
- Step 5: each further platform brings its own uploader module, which the source rule then names alongside this one.
- Step 6 (agent side): SkillSpec `publish_upload`, always CONFIRM, after QC; `UPLOAD_PARTIAL` must never trigger an automatic re-run.

## ADR-005 The Vimeo uploader: the tus resumable upload with a personal access token, title / description / privacy only, tested against a loopback mock only

### Context

Roadmap step 5 asks for further platforms, one PR each, done when mock-server tests pass. Vimeo is the first: corporate and conference video work commonly uses it. ADR-002 asked each platform to bring its own cited limit table (unknowns marked) and its `ffmpeg_skill_platform`; ADR-003 that its variables become keychain items with no new code; ADR-004 that each further uploader is its own module, which the "no network in src" rule then names alongside youtube.py.

Before any code, the two things this uploader depends on were checked in Vimeo's own developer documentation: a resumable upload (tus) and personal access tokens. Both are documented and usable (sources below), so Vimeo was kept. No real upload was made: not by this PR, not by its tests, not by CI.

### Protocol sources (read 2026-10-31)

The developer site renders its pages with JavaScript; the text below was read from the page bundles and the OpenAPI document the reference page embeds (API 3.4, reference revision 3.4.9).

- Uploads, the resumable approach: https://developer.vimeo.com/api/upload/videos ("Working with Video Uploads", "current for our latest API release, version 3.4"). "Resumable, or tus, after the open-source tus protocol"; "we recommend this approach for just about everyone". Create: `POST /me/videos` with `Authorization: bearer {access_token}`, `Content-Type: application/json`, `Accept: application/vnd.vimeo.*+json;version=3.4`; body `upload.approach` = `tus` and `upload.size` = the file's length in bytes ("required for the resumable approach"); the answer is 200 with `upload.upload_link` and `uri`; check that `upload.approach` is `tus`. Upload: `PATCH {upload.upload_link}` with `Tus-Resumable: 1.0.0`, `Upload-Offset` ("0 for the initial request; the stated upload offset for subsequent requests") and `Content-Type: application/offset+octet-stream`; `Accept` "is ignored during the resumable upload process". "If the value of Upload-Offset is the same as the size of the video file, we received the entire file"; otherwise PATCH again from that offset; a stale offset gives 409. Verify: `HEAD {upload.upload_link}` with `Tus-Resumable: 1.0.0` and the `Accept` header; 200 with `Upload-Length` and `Upload-Offset`. A tus library's chunks: "we recommend setting the chunk size in the range of 128–512 MB". Each approach "begins with a POST request, which adds a video placeholder to your account. This placeholder continues to exist, even if you don't subsequently upload a video file to it." Limits: "The maximum file size is 300 GB, and the maximum duration is 24 hours." Metadata in the create body: `name`, `description`, `privacy.view`.
- The tus 1.0.0 protocol: https://tus.io/protocols/resumable-upload. PATCH answers 204 with the new `Upload-Offset`; a mismatched offset is 409; HEAD answers 200 or 204 with `Upload-Offset` (and `Upload-Length` when known); an unknown upload is 404, 410 or 403; an expired upload 404 or 410; an unsupported `Tus-Resumable` is 412.
- Personal access tokens: https://developer.vimeo.com/api/guides/start ("Generate an access token": on the app's page, "Authenticated (you)", tick the scopes, "Generate"; "You can't recover a lost token string, but you can always get a new one by creating a new token") and https://developer.vimeo.com/api/authentication (the scope table: `upload` "Upload videos"; a missing or unrecognised token is 401 with error code 8003; "Some access tokens have a limited lifespan. Others continue to work indefinitely, as long as you use them on a regular basis").
- Upload access: https://developer.vimeo.com/api/guides/videos/upload ("for your app to be able to upload videos to Vimeo, you need to turn on upload access. This capability isn't enabled by default"; Vimeo Free needs an approved app; "An access token with the upload and edit scopes").
- The method, https://developer.vimeo.com/api/reference/videos#upload_video ("Upload a video", `POST /users/{user_id}/videos`, alias `/me/videos`, security `oauth2: upload`): responses 201; 401 "Error code 8002: No user is associated with the access token"; 403 "Error code 4101: The authenticated user's maximum disk space has been reached", "4102: ... allotted weekly quota has been reached", "4104: ... allotted daily quota has been reached"; 400 2204, 2205, 2230, 3116; 500 4003. `upload.size`: "The maximum value of this field is `322122547200`, which corresponds to 300 GB". `privacy.view`: `anybody` (Public), `contacts` (deprecated), `disable` (Embed only), `nobody` ("No one except the owner can access the video ... `Private`"), `password`, `team` (Company only), `unlisted`, `users` (deprecated).
- https://developer.vimeo.com/api/reference/videos#edit_video ("Edit a video"): `name` "can hold a maximum of 128 characters"; `description` "a maximum of 5000 characters".
- Rate limits: https://developer.vimeo.com/guidelines/rate-limiting (requests per minute per authenticated user by membership: Free 25, Starter 125, Standard 250, Advanced 750, Studio 1500, Enterprise 2500; above that "HTTP status code 429 and error code 9000").

### Decision

- **A second network module: `src/publish_skill/platforms/vimeo.py`.** ADR-001's source rule now exempts exactly the two uploader modules, for `http.client` and `ssl` only. The test that enforces it names both, requires them to be the only files mentioning `http.client`, and checks the contract's `network_code` lists the same two. The AST test runs on both: stdlib imports only, one `HTTPSConnection` with `ssl.create_default_context()` and one loopback-only `HTTPConnection`, both with a timeout. vimeo.py does not import youtube.py: each uploader is self-contained, so a change to one protocol cannot change the other's requests.
- **Credentials: one variable, `PUBLISH_VIMEO_ACCESS_TOKEN`**, a personal access token with the `upload` scope (the guide also asks for `edit`). From the environment or the keychain under that name (ADR-003, no new code); `auth login --platform vimeo` stores it. The OAuth authorization-code flow is not implemented, for ADR-004's reason (a browser and a redirect listener for a one-time step).
- **The token goes to `api.vimeo.com` only.** The create `POST` carries `Authorization: bearer`. The `PATCH` and `HEAD` requests carry none, as the guide's tables list none. The upload link is a capability URL: it is held for `redact`, so it never reaches an output, and it must be https on `vimeo.com` or a `*.vimeo.com` host, or the upload stops before any byte of the file is sent (`UPLOAD_REJECTED`). The host rule is this skill's choice, not a documented value: the docs do not name the upload host. If Vimeo moves it elsewhere, the uploader fails safe and this rule is revisited. Only a loopback API base (the tests' mock) may hand out a link on its own scheme, host and port.
- **Exactly the bound request.** The create body is `upload` (`approach: tus`, `size` as the reference's string type), then the bound `name` and `description` when set, and always `privacy.view`. Nothing else is added (no `embed`, `download`, `locale` or folder). Privacy keeps the skill's three names: `private` -> `nobody` (the most private option Vimeo offers: "No one except the owner"), `unlisted` -> `unlisted`, `public` -> `anybody`. `privacy.view` is always sent, so a forgotten flag gives `nobody`, whatever the account's default. `password` needs a secret in the request, `team` a team plan, and `contacts` / `users` are deprecated, so none is offered. `disable` (embed only) is less private than `nobody`.
- **Fields the uploader does not send are refused, never dropped.** `Uploader.metadata_fields` names what an uploader sends (YouTube: all six; Vimeo: title, description, privacy). `plan` gives one FAIL row per other field that is set, so `upload` refuses it as `METADATA_INVALID` before a token is printed or a credential read. Vimeo tags (`PUT /videos/{id}/tags`), thumbnails and text tracks are separate requests with their own failure modes (an `UPLOAD_PARTIAL` case), so they are left to a later PR.
- **The tus transfer.** Chunks are 128 MiB (134 217 728 bytes, inside "128–512 MB" under either reading of MB); any size is accepted, since tus has no alignment rule. As in ADR-004, the file is hashed as the server confirms bytes, and the final chunk is sent only when the bytes so far plus that chunk have the bound sha256 (`CONFIRM_MISMATCH` otherwise). Vimeo completes the upload when the offset reaches `upload.size`, so an edited file is never completed. A confirmed offset outside the chunk just sent is a protocol error. An `Upload-Length` from `HEAD` that differs from the size is a protocol error too.
- **Retries.** As in ADR-004: a 60 s timeout per request, at most 10 retries per request, `random() × 2^n` s backoff capped at 64 s, or `Retry-After` up to 5 minutes.
  - A `PATCH` with no answer, a reset, a timeout, 5xx, 429 or 409 waits, then asks `HEAD` for the offset and resumes from it. A `PATCH` that kept nothing counts as a retry.
  - The create `POST` is retried only when the server answered 429 or 5xx. With no answer it is not retried: the placeholder may already exist, and a second `POST` would add another.
  - A 404, 410 or 403 from the upload link means the upload expired (tus). It is `UPLOAD_INTERRUPTED` and not restarted, because a restart needs a new create, which means a second video.
  - Every failure after the create says that Vimeo keeps an empty placeholder (`details.video_uri`, with the privacy sent). An interrupted run is not resumed by a later run: the upload link would have to be kept on disk, and it is an upload credential (ADR-001, ADR-004).
- **Typed errors, no new codes.** Vimeo's error body (`error`, `error_code`, `developer_message`) gives the code:
  - 4102 / 4104 (the weekly / daily quota): `UPLOAD_LIMIT_EXCEEDED` (retryable after it resets, never retried within the run).
  - 4101 (storage full): `UPLOAD_REJECTED` (not retryable: waiting does not free space).
  - 401: `AUTH_FAILED`.
  - Another 4xx, or a protocol error: `UPLOAD_REJECTED`.
  - Retries running out: `UPLOAD_INTERRUPTED`.
  - Details carry the status, the error code and the developer message (at most 300 characters), never a header, the token or the upload link.
- **The limits table** (`limits.py` → `vimeo`, the README, `contract --json`). Every value cites a developer.vimeo.com page and 2026-10-31.
  - Documented: the file size (322 122 547 200 bytes, stated in bytes), the duration (24 h), the title (128 characters) and description (5000 characters, so `plan` counts characters for Vimeo, bytes for YouTube), and the privacy names with their `privacy.view` mapping. Also recorded, but not checked: the tus content type, the recommended chunk size, the rate limits per membership, upload access and the scopes.
  - Unknown, not checked: the accepted containers, forbidden characters, and the per-account storage, weekly quota and daily quota. The errors exist, but no number is stated.
  - `plan` no longer prints a PASS row against an unknown forbidden-character limit, because nothing is checked against an unknown.
- **The delivery spec.** ffmpeg-skill has no `vimeo` platform in `scripts/_platforms.py` (2026-10-31), and that spec is ffmpeg-skill's (owner decision, 2026-10-30), so nothing is written here. `ffmpeg_skill_platform` is empty. `plan --platform vimeo` reports the delivery spec and the duration as `UNCHECKED` with the reason, so its verdict is `INCOMPLETE`, never `PASS`. The codec, resolution, aspect and frame rate entries are `delegated` with that note.
- **Contract and doctor.** Vimeo is `implemented: true` (`roadmap_step` 5). `network_code` lists both modules. `network_hosts` adds `api.vimeo.com` and the `*.vimeo.com` upload link. `upload_protocol.vimeo` describes the protocol. `not_provided` names Vimeo tags, thumbnails, text tracks and the OAuth flow. `doctor` reports Vimeo ready when its token is present.
- **Tests never contact Vimeo.**
  - `tests/mock_vimeo.py` is an in-process `ThreadingHTTPServer` on `127.0.0.1`, port 0. It implements `POST /me/videos` (bearer token, `Accept`, a tus body) and tus 1.0.0 `PATCH`/`HEAD`: `Tus-Resumable` (412), the content type (415), offset matching (409), 204 with `Upload-Offset`, `Upload-Length`, and 404 for an unknown or expired upload. It scripts faults per request: a status with a Vimeo error code, an interrupted chunk that keeps part of its bytes, a stall, an expired upload, and a callback.
  - `tests/test_vimeo.py` (40 tests) covers the chunked upload, resume after an interrupted chunk (`test_vimeo_resume_after_an_interrupted_chunk`), a resume when nothing arrived, a timeout, a 409 resync, 5xx/429 backoff with `Retry-After`, retries running out, the create not retried without an answer, an expired link, a file edited mid-upload, quota and storage errors (`test_vimeo_quota_is_typed_and_not_retried`), the rate limit, a refused token, the exact create body and the token rebuilt from what arrived, the privacy mapping, the refusals before any request or credential read, redaction of the token and the upload link in every outcome (JSON and human, environment and keychain), the plan rows and the limits table.
  - The socket guard is unchanged: only `127.0.0.1` on the port of a mock the test started. `test_the_guard_blocks_every_non_loopback_host_for_vimeo` asserts that it blocks Vimeo's hosts, public addresses, other loopback ports, `::1`, `0.0.0.0` and name lookups. A confirmed upload without the mock is stopped at `api.vimeo.com`. CI fails unless that test and the Vimeo resume and quota tests ran and passed.

### Follow-ups

- The first real Vimeo upload, by the owner, with their token and their confirmation: check the receipt against the video's page (privacy, title, description), and that the upload link's host passes the vimeo.com rule.
- Vimeo tags, thumbnail and text tracks, each with the `UPLOAD_PARTIAL` semantics of ADR-004.
- A Vimeo delivery spec in ffmpeg-skill (its owner's decision), so that `plan --platform vimeo` can reach `PASS`.
- Further platforms (step 5), one PR each.

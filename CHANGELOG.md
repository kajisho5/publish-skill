# Changelog

## 0.1.0 - unreleased

Credentials in the OS keychain (roadmap step 3).

- ADR-003: `publish auth status|login|logout`. Credentials come from the environment or the OS keychain (macOS Keychain, Windows Credential Manager, Secret Service), the environment first and per platform as a whole, so one platform's values never mix sources. `login` reads values from stdin or a prompt that does not echo, never from argv.
- The keychain is reached through `keyring>=25.0` (MIT), an optional extra: `pip install 'publish-skill[keychain]'`. Without it, credentials come from the environment only and `doctor` says so. Only the three OS keychains are used; `PUBLISH_KEYCHAIN=off` turns it off.
- Reports still say present / absent only; the redactor also covers every keychain value the process holds; every refusal still comes before any credential is read. New errors `KEYCHAIN_UNAVAILABLE` and `KEYCHAIN_ERROR` (exit 3).
- Tests run against an in-memory fake keychain, with the real one guarded off; CI checks that the fake is what runs, and installs the extra on Python 3.12.

`publish plan` (roadmap step 2).

- ADR-002: `publish plan FILE --platform ID` checks the deliverable and its title / description / tags / privacy / thumbnail / captions against the platform's upload limits, offline, with a PASS / WARN / FAIL / UNCHECKED row per limit and a fix for each; verdict PASS / WARN / FAIL / INCOMPLETE, exit 1 unless PASS or WARN.
- The YouTube upload-limit table (`limits.py`, the README, `contract --json` → `upload_limits`): every value cites the official page and the date it was read (2026-10-31); limits the documentation does not state are `unknown`, and the delivery spec stays with ffmpeg-skill (`delegated`).
- The ffmpeg-skill adapter: capability detection from its contract (`export --list`, `check --platform/--json/--no-loudness`, no network) and its `check` rows (codec, frame size, aspect, ...) passed through; the only module that starts a process.
- The confirmation token now binds the metadata, too, and `upload` takes the same metadata flags; metadata that breaks a limit is refused as `METADATA_INVALID` before a token is printed. Privacy defaults to `private`.

Scaffold (roadmap step 1).

- ADR-001: `publish contract` / `skill`, `doctor` (`--offline`, `--ffmpeg-skill`), `platforms`; `publish upload` is a dry run unless `--confirm` carries the token the dry run printed for exactly that file and platform; credentials from the environment only, reported as present / absent and redacted from every output; typed errors with exit codes and `retryable`.
- The YouTube uploader is registered as a stub (`NOT_IMPLEMENTED`, roadmap step 4) and `publish plan` arrives in step 2; both capabilities are declared `PROPOSED`. Nothing is uploaded in this release.

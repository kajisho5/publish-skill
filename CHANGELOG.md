# Changelog

## 0.1.0 - unreleased

`publish plan` (roadmap step 2).

- ADR-002: `publish plan FILE --platform ID` checks the deliverable and its title / description / tags / privacy / thumbnail / captions against the platform's upload limits, offline, with a PASS / WARN / FAIL / UNCHECKED row per limit and a fix for each; verdict PASS / WARN / FAIL / INCOMPLETE, exit 1 unless PASS or WARN.
- The YouTube upload-limit table (`limits.py`, the README, `contract --json` → `upload_limits`): every value cites the official page and the date it was read (2026-10-31); limits the documentation does not state are `unknown`, and the delivery spec stays with ffmpeg-skill (`delegated`).
- The ffmpeg-skill adapter: capability detection from its contract (`export --list`, `check --platform/--json/--no-loudness`, no network) and its `check` rows (codec, frame size, aspect, ...) passed through; the only module that starts a process.
- The confirmation token now binds the metadata, too, and `upload` takes the same metadata flags; metadata that breaks a limit is refused as `METADATA_INVALID` before a token is printed. Privacy defaults to `private`.

Scaffold (roadmap step 1).

- ADR-001: `publish contract` / `skill`, `doctor` (`--offline`, `--ffmpeg-skill`), `platforms`; `publish upload` is a dry run unless `--confirm` carries the token the dry run printed for exactly that file and platform; credentials from the environment only, reported as present / absent and redacted from every output; typed errors with exit codes and `retryable`.
- The YouTube uploader is registered as a stub (`NOT_IMPLEMENTED`, roadmap step 4) and `publish plan` arrives in step 2; both capabilities are declared `PROPOSED`. Nothing is uploaded in this release.

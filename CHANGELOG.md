# Changelog

## 0.1.0 - unreleased

Scaffold (roadmap step 1).

- ADR-001: `publish contract` / `skill`, `doctor` (`--offline`, `--ffmpeg-skill`), `platforms`; `publish upload` is a dry run unless `--confirm` carries the token the dry run printed for exactly that file and platform; credentials from the environment only, reported as present / absent and redacted from every output; typed errors with exit codes and `retryable`.
- The YouTube uploader is registered as a stub (`NOT_IMPLEMENTED`, roadmap step 4) and `publish plan` arrives in step 2; both capabilities are declared `PROPOSED`. Nothing is uploaded in this release.

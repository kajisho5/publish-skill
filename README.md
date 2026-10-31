<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/app-icon-dark.png">
    <img src="brand/app-icon.png" width="128" alt="publish-skill app icon">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/logo/publish-skill-wordmark-white.svg">
    <img src="brand/logo/publish-skill-wordmark-black.svg" height="48" alt="publish-skill">
  </picture>
</p>

<p align="center"><b>Upload to delivery platforms, after QC</b></p>



Upload finished deliverables to delivery platforms (YouTube first) for the [AI Video Production OS](https://github.com/kajisho5/AI-video-production-OS) (department 納品 / Delivery), after QC and a human's confirmation. Platform delivery specs (codecs, resolution, loudness) belong to [ffmpeg-skill](https://github.com/kajisho5/ffmpeg-skill); this skill reads them and owns only the upload-side limits.

**Status: scaffold (0.1.0).** `contract`, `doctor`, `platforms` and the dry run of `upload` work. **No uploader is implemented, so nothing is ever uploaded yet**; `publish plan` and the YouTube uploader arrive in [roadmap](ECOSYSTEM_ROADMAP.md) steps 2 and 4.

```bash
pip install -e .                                          # stdlib only, Python >= 3.9
publish doctor                                            # platforms, credentials present/absent, ffmpeg-skill
publish upload final.mp4 --platform youtube --json        # dry run: what would be sent, and the confirm_token
publish upload final.mp4 --platform youtube --confirm <confirm_token> --json
publish contract --json                                   # machine-readable contract (alias: skill --json)
```

## Safety ([ADR-001](docs/decisions.md))

- **Dry run by default.** Without `--confirm`, `upload` reads the file and reports; it opens no connection.
- **A confirmation token per deliverable.** The dry run prints `confirm_token`, derived from the platform and the file's sha256 and size. Only that exact token on the command line goes further, so an approval cannot be reused for a re-exported file or another platform. No environment variable or config file confirms for you.
- **Credentials from the environment only**, never from files or flags, and never printed: reports say `present` / `absent`, and every output passes through a redactor. YouTube reads `PUBLISH_YOUTUBE_CLIENT_ID`, `PUBLISH_YOUTUBE_CLIENT_SECRET` and `PUBLISH_YOUTUBE_REFRESH_TOKEN`. The OS keychain arrives in step 3.
- **Uploaders are stubs** that return `NOT_IMPLEMENTED` (exit 5); the package contains no network code yet.

## JSON and errors

With `--json`, stdout holds exactly one JSON document. Failures are `{"ok": false, "error": {"code", "message", "retryable", "details"}}`; the code fixes the exit code (`contract --json` lists them under `errors`).

## Tests

```bash
python -m unittest discover -s tests -v
```

Every unit test runs with network access patched to fail. The OS registry conformance checks and the JSON Schema validation of the contract run when `AI_VIDEO_PRODUCTION_OS_DIR` (or `../AI-video-production-OS`) is present and `jsonschema` is installed.

## Licence

MIT.

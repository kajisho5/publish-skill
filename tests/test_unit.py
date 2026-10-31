"""Unit tests: contract, typed errors, platform registry, credentials (environment only, never printed), the upload
confirmation gate, doctor, and the OS registry conformance and schema checks. Every test runs with socket connections
patched to fail, so any attempt to reach the network is a test failure; nothing is uploaded."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import OS_DIR, ROOT, env, run_cli, run_cli_raw

from publish_skill import SKILL_ID, __version__
from publish_skill import credentials as creds
from publish_skill.contract import CAPABILITY_IDS, contract
from publish_skill.doctor import doctor, locate_ffmpeg_skill
from publish_skill.errors import ERROR_TABLE, EXIT_CODES, PublishError
from publish_skill.platforms import UploadRequest, get_platform, platform_ids
from publish_skill.upload import confirm_token, sha256_file, upload

YT_VARS = ("PUBLISH_YOUTUBE_CLIENT_ID", "PUBLISH_YOUTUBE_CLIENT_SECRET", "PUBLISH_YOUTUBE_REFRESH_TOKEN")
SECRETS = {"PUBLISH_YOUTUBE_CLIENT_ID": "cid-7f3a91e2.apps.example",
           "PUBLISH_YOUTUBE_CLIENT_SECRET": "csecret-Zq81LmP0sW",
           "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//refresh-XyZ0987654321"}
NO_SECRETS = {k: None for k in YT_VARS}


def _no_network(*_a, **_k):
    raise AssertionError("network access attempted")


class OfflineTestCase(unittest.TestCase):
    """Fails the test on any socket connect or name lookup."""

    def setUp(self):
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection", "socket.getaddrinfo"):
            p = mock.patch(target, _no_network)
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp(prefix="publish-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.video = os.path.join(self.tmp, "final.mp4")
        with open(self.video, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8)


class NetworkGuardTests(OfflineTestCase):
    def test_guard_is_active(self):
        with self.assertRaises(AssertionError):
            socket.create_connection(("example.com", 443))


class ErrorTests(unittest.TestCase):
    def test_table_shape_and_retryable(self):
        for code, (exit_code, retryable) in ERROR_TABLE.items():
            self.assertTrue(re.fullmatch(r"[A-Z_]+", code))
            self.assertGreaterEqual(exit_code, 2)
            self.assertIsInstance(retryable, bool)
        self.assertEqual({c for c, (_, r) in ERROR_TABLE.items() if r}, {"CANCELLED"})
        self.assertEqual(EXIT_CODES["NOT_IMPLEMENTED"], 5)

    def test_error_document(self):
        e = PublishError("CONFIRM_MISMATCH", "no", {"platform": "youtube"})
        self.assertEqual(e.to_dict(), {"code": "CONFIRM_MISMATCH", "message": "no", "retryable": False,
                                       "details": {"platform": "youtube"}})
        self.assertEqual(e.exit_code, 2)
        with self.assertRaises(ValueError):
            PublishError("NOPE", "x")


class ContractTests(OfflineTestCase):
    def test_os_registry_keys_and_provides(self):
        c = contract()
        for k in ("skill_id", "id", "skill", "version", "contract_version", "provides", "dependencies", "not_provided"):
            self.assertIn(k, c)
        self.assertEqual((c["skill_id"], c["version"]), (SKILL_ID, __version__))
        self.assertEqual({p["id"] for p in c["provides"]}, set(CAPABILITY_IDS.values()))
        tool_ids = {t["tool_id"] for t in c["tools"]}
        for p in c["provides"]:
            self.assertEqual(set(p), {"id", "lifecycle", "tool_id"})
            self.assertIn(p["tool_id"], tool_ids)
            self.assertEqual(p["lifecycle"], "PROPOSED")       # nothing uploads or plans yet (ADR-001)
        for d in c["dependencies"]:
            self.assertIsInstance(d["version_range"], str)
            self.assertFalse(d["version_range"].replace(".", "").isdigit(), d)
        json.dumps(c, allow_nan=False)

    def test_network_and_confirmation_are_declared(self):
        c = contract()
        self.assertTrue(c["execution"]["network"])              # roadmap step 1: network: true
        self.assertEqual(c["execution"]["network_tools"], ["publish/upload"])
        self.assertFalse(c["execution"]["network_in_this_release"])
        self.assertFalse(c["execution"]["shell"])
        tools = {t["tool_id"]: t for t in c["tools"]}
        self.assertEqual({t for t, v in tools.items() if v["network"]}, {"publish/upload"})
        self.assertEqual(c["confirmation"]["required_for"], ["publish/upload"])
        self.assertFalse(c["confirmation"]["from_environment_or_config"])
        self.assertEqual(c["credentials"]["source"], "environment")
        self.assertFalse(c["credentials"]["printed"])

    def test_errors_and_platforms_mirror_the_code(self):
        c = contract()
        self.assertEqual(c["errors"], {k: {"exit_code": e, "retryable": r} for k, (e, r) in ERROR_TABLE.items()})
        self.assertEqual(sorted(c["platforms"]["registered"]), platform_ids())
        for spec in c["platforms"]["registered"].values():
            self.assertFalse(spec["implemented"])

    def test_version_comes_from_pyproject(self):
        want = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
        self.assertEqual(__version__, want)

    def test_cli_contract_and_skill_alias(self):
        rc, doc = run_cli("contract", "--json")
        rc2, doc2 = run_cli("skill", "--json")
        self.assertEqual((rc, rc2, doc["skill_id"]), (0, 0, SKILL_ID))
        self.assertEqual(doc, doc2)

    def test_no_shell_and_no_network_code_in_the_scaffold(self):
        # No uploader is implemented, so no module may contain network code yet; step 4 adds it to its uploader only.
        for f in (ROOT / "src" / "publish_skill").rglob("*.py"):
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, f.name)
            for banned in ("import socket", "urllib.request", "http.client", "import requests", "urlopen", "httpx",
                           "subprocess", "os.system"):
                self.assertNotIn(banned, text, f"{f.name}: {banned}")


class PlatformTests(OfflineTestCase):
    def test_registry(self):
        self.assertEqual(platform_ids(), ["youtube"])
        yt = get_platform("youtube")
        self.assertEqual((yt.implemented, yt.roadmap_step), (False, 4))
        self.assertEqual(yt.credential_env, YT_VARS)
        with self.assertRaises(PublishError) as e:
            get_platform("tiktok")
        self.assertEqual(e.exception.code, "UNKNOWN_PLATFORM")

    def test_stub_uploader_raises_not_implemented(self):
        req = UploadRequest(platform="youtube", path=self.video, size=1, sha256="0" * 64)
        with self.assertRaises(PublishError) as e:
            get_platform("youtube").upload(req, creds.Credentials("youtube", SECRETS))
        self.assertEqual(e.exception.code, "NOT_IMPLEMENTED")
        self.assertFalse(e.exception.details["uploaded"])

    def test_cli_platforms(self):
        with env(**NO_SECRETS):
            rc, doc = run_cli("platforms", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(doc["platforms"]["youtube"]["credentials"]["variables"], {v: "absent" for v in YT_VARS})
        self.assertFalse(doc["platforms"]["youtube"]["ready"])


class CredentialTests(OfflineTestCase):
    def test_status_reports_presence_only(self):
        with env(**SECRETS):
            st = creds.status("youtube")
        self.assertEqual(st, {"source": "environment", "variables": {v: "present" for v in YT_VARS}, "complete": True})
        with env(**dict(NO_SECRETS, PUBLISH_YOUTUBE_CLIENT_ID="x", PUBLISH_YOUTUBE_CLIENT_SECRET="   ")):
            st = creds.status("youtube")
        self.assertEqual(st["variables"]["PUBLISH_YOUTUBE_CLIENT_SECRET"], "absent")    # blank counts as absent
        self.assertFalse(st["complete"])

    def test_load_names_missing_variables_not_values(self):
        with env(**dict(NO_SECRETS, PUBLISH_YOUTUBE_CLIENT_ID=SECRETS["PUBLISH_YOUTUBE_CLIENT_ID"])):
            with self.assertRaises(PublishError) as e:
                creds.load("youtube")
        self.assertEqual(e.exception.code, "CREDENTIALS_MISSING")
        self.assertEqual(e.exception.details["missing"], list(YT_VARS[1:]))
        self.assertNotIn(SECRETS["PUBLISH_YOUTUBE_CLIENT_ID"], json.dumps(e.exception.to_dict()))

    def test_credentials_object_never_shows_values(self):
        c = creds.Credentials("youtube", SECRETS)
        for s in SECRETS.values():
            self.assertNotIn(s, repr(c))
            self.assertNotIn(s, str(c))
        self.assertEqual(c.get("PUBLISH_YOUTUBE_CLIENT_ID"), SECRETS["PUBLISH_YOUTUBE_CLIENT_ID"])

    def test_redact(self):
        with env(**SECRETS):
            doc = creds.redact({"msg": f"token {SECRETS['PUBLISH_YOUTUBE_REFRESH_TOKEN']} bad",
                                "list": [SECRETS["PUBLISH_YOUTUBE_CLIENT_SECRET"], 3, None]})
        self.assertEqual(doc, {"msg": "token [REDACTED] bad", "list": ["[REDACTED]", 3, None]})
        with env(**NO_SECRETS):
            self.assertEqual(creds.redact({"a": "b"}), {"a": "b"})


class UploadGateTests(OfflineTestCase):
    def test_dry_run_is_the_default(self):
        with env(**NO_SECRETS):
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--json")
        self.assertEqual(rc, 0)
        self.assertTrue(doc["dry_run"])
        self.assertFalse(doc["uploaded"])
        self.assertFalse(doc["network_used"])
        self.assertEqual(doc["input"]["sha256"], sha256_file(self.video))
        self.assertEqual(doc["input"]["size"], os.path.getsize(self.video))
        self.assertEqual(doc["confirm_token"], confirm_token("youtube", doc["input"]["sha256"], doc["input"]["size"]))
        self.assertIn(doc["confirm_token"], doc["next_step"])

    def test_token_binds_file_and_platform(self):
        t = confirm_token("youtube", "a" * 64, 10)
        self.assertTrue(t.startswith("confirm-"))
        self.assertEqual(t, confirm_token("youtube", "a" * 64, 10))
        self.assertNotEqual(t, confirm_token("youtube", "b" * 64, 10))
        self.assertNotEqual(t, confirm_token("youtube", "a" * 64, 11))
        self.assertNotEqual(t, confirm_token("vimeo", "a" * 64, 10))

    def test_wrong_or_stale_token_is_refused(self):
        token = upload(self.video, "youtube")["confirm_token"]
        with open(self.video, "ab") as fh:                       # the deliverable changed after the dry run
            fh.write(b"re-export")
        with env(**SECRETS):
            for bad in (token, "confirm-0000000000000000", "yes", ""):
                rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--confirm", bad, "--json")
                self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"), bad)
                self.assertFalse(doc["error"]["details"]["uploaded"])

    def test_no_environment_variable_confirms(self):
        with env(PUBLISH_CONFIRM="yes", PUBLISH_YES="1", CI="true", **SECRETS):
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--json")
        self.assertEqual(rc, 0)
        self.assertTrue(doc["dry_run"])

    def test_right_token_without_credentials(self):
        token = upload(self.video, "youtube")["confirm_token"]
        with env(**NO_SECRETS):
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--confirm", token, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (3, "CREDENTIALS_MISSING"))
        self.assertEqual(doc["error"]["details"]["missing"], list(YT_VARS))

    def test_right_token_with_credentials_reaches_the_stub(self):
        token = upload(self.video, "youtube")["confirm_token"]
        with env(**SECRETS):
            rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", "--confirm", token, "--json")
        doc = json.loads(out)
        self.assertEqual((rc, doc["error"]["code"]), (5, "NOT_IMPLEMENTED"))
        self.assertFalse(doc["error"]["details"]["uploaded"])
        for s in SECRETS.values():
            self.assertNotIn(s, out + err)

    def test_unknown_platform_and_missing_input(self):
        rc, doc = run_cli("upload", self.video, "--platform", "myspace", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "UNKNOWN_PLATFORM"))
        rc, doc = run_cli("upload", os.path.join(self.tmp, "nope.mp4"), "--platform", "youtube", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INPUT_NOT_FOUND"))
        rc, doc = run_cli("upload", self.tmp, "--platform", "youtube", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INPUT_NOT_FOUND"))

    def test_input_is_not_modified(self):
        before = (sha256_file(self.video), os.path.getmtime(self.video))
        upload(self.video, "youtube")
        self.assertEqual(before, (sha256_file(self.video), os.path.getmtime(self.video)))

    def test_plan_is_not_implemented_yet(self):
        rc, doc = run_cli("plan", self.video, "--platform", "youtube", "--json")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["roadmap_step"]), (5, "NOT_IMPLEMENTED", 2))

    def test_human_output_is_redacted_too(self):
        token = upload(self.video, "youtube")["confirm_token"]
        with env(**SECRETS):
            outs = [run_cli_raw(*a) for a in (("doctor",), ("platforms",), ("contract",),
                                              ("upload", self.video, "--platform", "youtube"),
                                              ("upload", self.video, "--platform", "youtube", "--confirm", token))]
        for rc, out, err in outs:
            for s in SECRETS.values():
                self.assertNotIn(s, out + err)


class SecretNeverPrintedTests(OfflineTestCase):
    def test_a_one_character_credential_is_redacted_without_breaking_the_document(self):
        token = upload(self.video, "youtube")["confirm_token"]
        with env(**dict(SECRETS, PUBLISH_YOUTUBE_CLIENT_ID="a")):
            rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", "--confirm", token)   # human output
            self.assertEqual(rc, 5)
            self.assertIn("error [NOT_IMPLEMENTED]", err)
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--confirm", token, "--json")
        self.assertEqual(doc["error"]["code"], "NOT_IMPLEMENTED")
        self.assertNotIn("a", doc["error"]["message"])


    def test_no_command_prints_a_credential(self):
        token = upload(self.video, "youtube")["confirm_token"]
        argvs = [("contract", "--json"), ("skill", "--json"), ("doctor", "--json"), ("platforms", "--json"),
                 ("upload", self.video, "--platform", "youtube", "--json"),
                 ("upload", self.video, "--platform", "youtube", "--confirm", token, "--json"),
                 ("upload", self.video, "--platform", "youtube", "--confirm", "nope", "--json"),
                 ("plan", self.video, "--platform", "youtube", "--json")]
        with env(**SECRETS):
            for argv in argvs:
                rc, out, err = run_cli_raw(*argv)
                json.loads(out)                                   # exactly one JSON document
                for s in SECRETS.values():
                    self.assertNotIn(s, out + err, argv)


class DoctorTests(OfflineTestCase):
    def test_degraded_with_nothing_implemented(self):
        with env(**NO_SECRETS):
            d = doctor(os.path.join(self.tmp, "no-ffmpeg-skill"))
        self.assertEqual((d["status"], d["ok"], d["ready_platforms"]), ("degraded", True, []))
        self.assertEqual(d["checks"]["ffmpeg_skill"]["status"], "MISSING")
        self.assertEqual(d["checks"]["platforms"]["youtube"]["credentials"]["variables"], {v: "absent" for v in YT_VARS})
        self.assertEqual(d["checks"]["operations"], {"plan": {"implemented": False, "roadmap_step": 2},
                                                     "upload": {"implemented": False, "roadmap_step": 4}})
        self.assertEqual(d["problems"], [])

    def test_credentials_complete_but_uploader_missing_is_not_ready(self):
        with env(**SECRETS):
            d = doctor(os.path.join(self.tmp, "no-ffmpeg-skill"))
        self.assertTrue(d["checks"]["platforms"]["youtube"]["credentials"]["complete"])
        self.assertFalse(d["checks"]["platforms"]["youtube"]["ready"])
        self.assertNotIn(SECRETS["PUBLISH_YOUTUBE_CLIENT_SECRET"], json.dumps(d))

    def test_ffmpeg_skill_located_by_marker(self):
        fake = Path(self.tmp) / "ffmpeg-skill"
        (fake / "scripts").mkdir(parents=True)
        (fake / "scripts" / "_contract.py").write_text("", encoding="utf-8")
        self.assertEqual(locate_ffmpeg_skill(str(fake))["status"], "OK")
        with env(PUBLISH_FFMPEG_SKILL_DIR=str(fake)):
            self.assertEqual(locate_ffmpeg_skill()["path"], str(fake.resolve()))

    def test_cli_doctor_exit_code(self):
        rc, doc = run_cli("doctor", "--json", "--offline", "--ffmpeg-skill", os.path.join(self.tmp, "none"))
        self.assertEqual((rc, doc["status"], doc["offline"]), (0, "degraded", True))


@unittest.skipUnless(OS_DIR, "needs an AI-video-production-OS checkout (AI_VIDEO_PRODUCTION_OS_DIR or ../AI-video-production-OS)")
class ConformanceTests(OfflineTestCase):
    def _registry(self):
        sys.path.insert(0, str(OS_DIR))
        try:
            from registry import conformance as conf
            from registry.schema import load_schema
        finally:
            sys.path.remove(str(OS_DIR))
        return conf, load_schema

    def test_registry_checks_pass(self):
        conf, _ = self._registry()
        results = conf.run_static_checks(contract())
        results.append(conf.check_no_unsafe_shell_out(str(ROOT / "src" / "publish_skill")))
        results.append(conf.check_doctor_status(lambda: doctor()))
        self.assertEqual([r.check for r in results], ["publishes_contract", "lifecycle_declared", "dependency_version_ranges",
                                                      "no_unsafe_shell_out", "doctor_status"])
        for r in results:
            self.assertEqual(r.status, "PASS", f"{r.check}: {r.detail}")

    def test_contract_validates_against_the_os_schema(self):
        # Roadmap step 1, "Done when: contract validates against the OS schema".
        try:
            import jsonschema
        except ImportError:
            self.skipTest("needs the jsonschema package (installed in CI's integration job)")
        _, load_schema = self._registry()
        jsonschema.Draft202012Validator(load_schema()).validate(contract())


if __name__ == "__main__":
    unittest.main()

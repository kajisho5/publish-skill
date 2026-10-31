"""Unit tests: contract, typed errors, platform registry, credentials (environment only, never printed), the upload
confirmation gate, doctor, and the OS registry conformance and schema checks. Every test runs with socket connections
patched to fail except to a loopback mock server the test started, so any attempt to reach the network is a test
failure; the uploads here go to tests/mock_youtube.py (and tests/test_vimeo.py's to tests/mock_vimeo.py) on 127.0.0.1."""
from __future__ import annotations

import ast
import json
import os
import re
import socket
import sys
import unittest
from pathlib import Path

from helpers import (LOOPBACK_PORTS, OS_DIR, ROOT, NetworkAttempted, OfflineTestCase, env, run_cli, run_cli_raw,
                     youtube_mock)

from publish_skill import SKILL_ID, __version__
from publish_skill import credentials as creds
from publish_skill.contract import CAPABILITY_IDS, contract
from publish_skill.doctor import doctor, locate_ffmpeg_skill
from publish_skill.errors import ERROR_TABLE, EXIT_CODES, PublishError
from publish_skill.platforms import Uploader, UploadRequest, get_platform, platform_ids
from publish_skill.upload import confirm_token, sha256_file, upload

YT_VARS = ("PUBLISH_YOUTUBE_CLIENT_ID", "PUBLISH_YOUTUBE_CLIENT_SECRET", "PUBLISH_YOUTUBE_REFRESH_TOKEN")
SECRETS = {"PUBLISH_YOUTUBE_CLIENT_ID": "cid-7f3a91e2.apps.example",
           "PUBLISH_YOUTUBE_CLIENT_SECRET": "csecret-Zq81LmP0sW",
           "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//refresh-XyZ0987654321"}
NO_SECRETS = {k: None for k in YT_VARS}
# the only files with network code: one module per implemented uploader (ADR-004, ADR-005)
NETWORK_MODULES = ("platforms/vimeo.py", "platforms/youtube.py")


class NetworkGuardTests(OfflineTestCase):
    def test_guard_is_active(self):
        with self.assertRaises(NetworkAttempted):
            socket.create_connection(("example.com", 443))
        for host in ("oauth2.googleapis.com", "www.googleapis.com", "api.vimeo.com", "files.tus.vimeo.com"):
            with self.assertRaises(NetworkAttempted):
                socket.getaddrinfo(host, 443)
        with self.assertRaises(NetworkAttempted):                       # loopback, but not a mock this test started
            socket.create_connection(("127.0.0.1", 9))
        s = socket.socket()
        self.addCleanup(s.close)
        with self.assertRaises(NetworkAttempted):
            s.connect(("142.250.0.1", 443))
        self.assertEqual(LOOPBACK_PORTS, set())


class ErrorTests(unittest.TestCase):
    def test_table_shape_and_retryable(self):
        for code, (exit_code, retryable) in ERROR_TABLE.items():
            self.assertTrue(re.fullmatch(r"[A-Z_]+", code))
            self.assertGreaterEqual(exit_code, 2)
            self.assertIsInstance(retryable, bool)
        # step 4 (ADR-004): an exhausted quota, the channel's daily limit and an interrupted transfer can succeed later
        self.assertEqual({c for c, (_, r) in ERROR_TABLE.items() if r},
                         {"CANCELLED", "QUOTA_EXCEEDED", "UPLOAD_LIMIT_EXCEEDED", "UPLOAD_INTERRUPTED"})
        self.assertEqual(EXIT_CODES["NOT_IMPLEMENTED"], 5)
        self.assertEqual({c for c, e in EXIT_CODES.items() if e == 4},
                         {"QUOTA_EXCEEDED", "UPLOAD_LIMIT_EXCEEDED", "UPLOAD_INTERRUPTED", "UPLOAD_REJECTED", "UPLOAD_PARTIAL"})
        self.assertEqual(EXIT_CODES["AUTH_FAILED"], 3)

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
        # plan works since step 2 (ADR-002), the YouTube upload since step 4 (ADR-004)
        self.assertEqual({p["id"]: p["lifecycle"] for p in c["provides"]},
                         {"publish.plan": "EXPERIMENTAL", "publish.upload": "EXPERIMENTAL"})
        for d in c["dependencies"]:
            self.assertIsInstance(d["version_range"], str)
            self.assertFalse(d["version_range"].replace(".", "").isdigit(), d)
        json.dumps(c, allow_nan=False)

    def test_network_and_confirmation_are_declared(self):
        c = contract()
        self.assertTrue(c["execution"]["network"])              # roadmap step 1: network: true
        self.assertEqual(c["execution"]["network_tools"], ["publish/upload"])
        self.assertTrue(c["execution"]["network_in_this_release"])
        self.assertEqual(c["execution"]["network_code"], ["src/publish_skill/platforms/vimeo.py",
                                                          "src/publish_skill/platforms/youtube.py"])
        self.assertEqual(c["execution"]["network_hosts"][:3], ["oauth2.googleapis.com", "www.googleapis.com", "api.vimeo.com"])
        self.assertEqual(len(c["execution"]["network_hosts"]), 4)
        self.assertTrue(c["execution"]["network_hosts"][3].startswith("*.vimeo.com"))
        self.assertEqual(sorted(c["execution"]["upload_protocol"]), ["vimeo", "youtube"])
        self.assertFalse(c["execution"]["shell"])
        tools = {t["tool_id"]: t for t in c["tools"]}
        self.assertEqual({t for t, v in tools.items() if v["network"]}, {"publish/upload"})
        self.assertEqual(c["confirmation"]["required_for"], ["publish/upload"])
        self.assertFalse(c["confirmation"]["from_environment_or_config"])
        self.assertEqual(c["credentials"]["sources"], ["environment", "keychain"])     # ADR-003
        self.assertFalse(c["credentials"]["printed"])
        self.assertFalse(c["credentials"]["from_files_or_flags"])

    def test_errors_and_platforms_mirror_the_code(self):
        c = contract()
        self.assertEqual(c["errors"], {k: {"exit_code": e, "retryable": r} for k, (e, r) in ERROR_TABLE.items()})
        self.assertEqual(sorted(c["platforms"]["registered"]), platform_ids())
        self.assertEqual({pid: spec["implemented"] for pid, spec in c["platforms"]["registered"].items()},
                         {"vimeo": True, "youtube": True})

    def test_version_comes_from_pyproject(self):
        want = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M).group(1)
        self.assertEqual(__version__, want)

    def test_cli_contract_and_skill_alias(self):
        rc, doc = run_cli("contract", "--json")
        rc2, doc2 = run_cli("skill", "--json")
        self.assertEqual((rc, rc2, doc["skill_id"]), (0, 0, SKILL_ID))
        self.assertEqual(doc, doc2)

    def test_no_shell_and_network_code_in_the_uploader_module_only(self):
        # ADR-004 narrows ADR-001's rule for exactly one file per implemented uploader: platforms/youtube.py and
        # (ADR-005) platforms/vimeo.py may hold network code (http.client), and nothing else may. Only the ffmpeg-skill
        # adapter may start a process (ADR-002). Every other banned word stays banned in all of them.
        src = ROOT / "src" / "publish_skill"
        network_words = ("import socket", "urllib.request", "http.client", "import requests", "urlopen", "httpx", "ssl",
                         "HTTPConnection", "HTTPSConnection", "create_connection")
        starts_process, has_network = [], []
        for f in src.rglob("*.py"):
            rel = f.relative_to(src).as_posix()
            text = f.read_text(encoding="utf-8")
            self.assertNotIn("shell=True", text, rel)
            for banned in network_words + ("subprocess", "os.system", "os.popen", "os.exec", "os.spawn"):
                if banned == "subprocess" and rel == "adapter.py":
                    continue
                if banned in network_words and rel in NETWORK_MODULES and banned in ("http.client", "ssl",
                                                                                            "HTTPConnection",
                                                                                            "HTTPSConnection"):
                    continue
                self.assertNotIn(banned, text, f"{rel}: {banned}")
            if "subprocess" in text:
                starts_process.append(rel)
            if "http.client" in text:
                has_network.append(rel)
        self.assertEqual(starts_process, ["adapter.py"])
        self.assertEqual(sorted(has_network), sorted(NETWORK_MODULES))
        self.assertEqual(sorted("src/publish_skill/" + m for m in NETWORK_MODULES), contract()["execution"]["network_code"])

    def test_the_uploader_imports_only_stdlib_http_and_opens_https_or_loopback_only(self):
        allowed = {"platforms/youtube.py": {"__future__", "hashlib", "http", "json", "random", "re", "ssl", "time",
                                            "typing", "urllib"},
                   "platforms/vimeo.py": {"__future__", "hashlib", "http", "json", "random", "ssl", "time", "typing",
                                          "urllib"}}
        self.assertEqual(sorted(allowed), sorted(NETWORK_MODULES))
        for rel, want in allowed.items():
            tree = ast.parse((ROOT / "src" / "publish_skill" / rel).read_text(encoding="utf-8"))
            imported = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    imported |= {a.name.split(".")[0] for a in n.names}
                elif isinstance(n, ast.ImportFrom) and n.level == 0:
                    imported.add(n.module.split(".")[0])
            self.assertEqual(imported, want, rel)
            conns = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                     and n.func.attr in ("HTTPConnection", "HTTPSConnection")]
            self.assertEqual(sorted(c.func.attr for c in conns), ["HTTPConnection", "HTTPSConnection"], rel)  # one site each
            https = [c for c in conns if c.func.attr == "HTTPSConnection"][0]
            ctx = {k.arg: k.value for k in https.keywords}["context"]                # a verifying default SSL context
            self.assertEqual(ast.unparse(ctx), "ssl.create_default_context()", rel)
            for c in conns:
                self.assertIn("timeout", {k.arg for k in c.keywords}, rel)

    def test_the_adapter_starts_processes_from_an_argv_list_without_a_shell(self):
        tree = ast.parse((ROOT / "src" / "publish_skill" / "adapter.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and isinstance(n.func.value, ast.Name) and n.func.value.id == "subprocess"]
        self.assertEqual([c.func.attr for c in calls], ["run"])        # one call site, subprocess.run
        call = calls[0]
        self.assertIsInstance(call.args[0], ast.Name)                  # the argv list built just above it, not a string
        kw = {k.arg: k.value for k in call.keywords}
        self.assertIsInstance(kw["shell"], ast.Constant)
        self.assertIs(kw["shell"].value, False)
        for k in ("timeout", "env", "stdin"):
            self.assertIn(k, kw)


class PlatformTests(OfflineTestCase):
    def test_registry(self):
        self.assertEqual(platform_ids(), ["vimeo", "youtube"])
        vm = get_platform("vimeo")
        self.assertEqual((vm.implemented, vm.roadmap_step, vm.credential_env), (True, 5, ("PUBLISH_VIMEO_ACCESS_TOKEN",)))
        self.assertEqual(vm.metadata_fields, ("title", "description", "privacy"))
        yt = get_platform("youtube")
        self.assertEqual((yt.implemented, yt.roadmap_step), (True, 4))
        self.assertEqual(yt.credential_env, YT_VARS)
        with self.assertRaises(PublishError) as e:
            get_platform("tiktok")
        self.assertEqual(e.exception.code, "UNKNOWN_PLATFORM")

    def test_stub_uploader_raises_not_implemented(self):
        # the base class, which each further platform starts from (step 5), still opens no connection
        class Stub(Uploader):
            id, name, roadmap_step = "stub", "Stub", 5

        req = UploadRequest(platform="stub", path=self.video, size=1, sha256="0" * 64)
        with self.assertRaises(PublishError) as e:
            Stub().upload(req, creds.Credentials("stub", SECRETS))
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
        self.assertEqual({k: st[k] for k in ("source", "variables", "complete", "environment")},
                         {"source": "environment", "variables": {v: "present" for v in YT_VARS}, "complete": True,
                          "environment": {v: "present" for v in YT_VARS}})
        self.assertEqual(st["keychain"]["variables"], {v: "absent" for v in YT_VARS})   # the fake keychain is empty
        self.assertNotIn(SECRETS["PUBLISH_YOUTUBE_CLIENT_SECRET"], json.dumps(st))
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

    def test_right_token_with_credentials_uploads_to_the_mock(self):
        m = youtube_mock(self, SECRETS)
        token = upload(self.video, "youtube")["confirm_token"]
        with env(**SECRETS):
            rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", "--confirm", token, "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        self.assertEqual((doc["dry_run"], doc["uploaded"], doc["network_used"]), (False, True, True))
        self.assertEqual(list(m.videos), [doc["result"]["video_id"]])
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], open(self.video, "rb").read())
        self.assertEqual(m.videos[doc["result"]["video_id"]]["resource"], {"status": {"privacyStatus": "private"}})
        for s in list(SECRETS.values()) + [m.access_token] + m.upload_ids:
            self.assertNotIn(s, out + err)

    def test_without_the_mock_a_confirmed_upload_would_reach_google_and_the_guard_stops_it(self):
        token = upload(self.video, "youtube")["confirm_token"]
        with env(**SECRETS), self.assertRaises(NetworkAttempted) as e:
            run_cli_raw("upload", self.video, "--platform", "youtube", "--confirm", token, "--json")
        self.assertIn("oauth2.googleapis.com", str(e.exception))

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

    def test_human_output_is_redacted_too(self):
        youtube_mock(self, SECRETS)
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
        youtube_mock(self, SECRETS)                     # expects SECRETS' client id, so "a" is refused: AUTH_FAILED
        token = upload(self.video, "youtube")["confirm_token"]
        with env(**dict(SECRETS, PUBLISH_YOUTUBE_CLIENT_ID="a")):
            rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", "--confirm", token)   # human output
            self.assertEqual(rc, 3)
            self.assertIn("error [AUTH_FAILED]", err)
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--confirm", token, "--json")
        self.assertEqual(doc["error"]["code"], "AUTH_FAILED")
        self.assertNotIn("a", doc["error"]["message"])


    def test_no_command_prints_a_credential(self):
        m = youtube_mock(self, SECRETS)
        token = upload(self.video, "youtube")["confirm_token"]
        argvs = [("contract", "--json"), ("skill", "--json"), ("doctor", "--json"), ("platforms", "--json"),
                 ("upload", self.video, "--platform", "youtube", "--json"),
                 ("upload", self.video, "--platform", "youtube", "--confirm", token, "--json"),
                 ("upload", self.video, "--platform", "youtube", "--confirm", "nope", "--json"),
                 ("plan", self.video, "--platform", "youtube", "--ffmpeg-skill", os.path.join(self.tmp, "none"), "--json"),
                 ("plan", self.video, "--platform", "youtube", "--ffmpeg-skill", os.path.join(self.tmp, "none"),
                  "--title", SECRETS["PUBLISH_YOUTUBE_CLIENT_SECRET"], "--json")]
        with env(**SECRETS):
            for argv in argvs:
                rc, out, err = run_cli_raw(*argv)
                json.loads(out)                                   # exactly one JSON document
                for s in list(SECRETS.values()) + [m.access_token] + m.upload_ids:
                    self.assertNotIn(s, out + err, argv)
        self.assertEqual(len(m.videos), 1)                                  # the confirmed run uploaded, once


class DoctorTests(OfflineTestCase):
    def test_degraded_without_credentials_or_ffmpeg_skill(self):
        with env(**NO_SECRETS):
            d = doctor(os.path.join(self.tmp, "no-ffmpeg-skill"))
        self.assertEqual((d["status"], d["ok"], d["ready_platforms"]), ("degraded", True, []))
        self.assertEqual(d["checks"]["ffmpeg_skill"]["status"], "MISSING")
        self.assertEqual(d["checks"]["platforms"]["youtube"]["credentials"]["variables"], {v: "absent" for v in YT_VARS})
        self.assertEqual(d["checks"]["operations"], {"plan": {"implemented": True, "roadmap_step": 2},
                                                     "upload": {"implemented": True, "roadmap_step": 4}})
        self.assertTrue(d["checks"]["platforms"]["youtube"]["implemented"])
        self.assertFalse(d["checks"]["platforms"]["youtube"]["ready"])
        self.assertEqual(d["problems"], [])

    def test_credentials_complete_and_uploader_implemented_is_ready(self):
        with env(**SECRETS):
            d = doctor(os.path.join(self.tmp, "no-ffmpeg-skill"))
        self.assertTrue(d["checks"]["platforms"]["youtube"]["credentials"]["complete"])
        self.assertTrue(d["checks"]["platforms"]["youtube"]["ready"])
        self.assertEqual(d["ready_platforms"], ["youtube"])
        self.assertEqual(d["status"], "degraded")                     # ffmpeg-skill and the keychain are still missing
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

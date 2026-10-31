"""Credentials in the OS keychain (roadmap step 3, ADR-003): the guard that keeps every test off the real keychain,
backend selection, precedence between the environment and the keychain, `publish auth status|login|logout`, refusals
before any credential is read, and no value in any output. Every keychain here is the in-memory fake from helpers.py;
every test runs with socket connections patched to fail."""
from __future__ import annotations

import ast
import importlib.util
import io
import json
import os
import re
import sys
import unittest
from unittest import mock

from helpers import (FAKE_BACKEND_NAME, REAL_DISCOVER, ROOT, OfflineTestCase, RealKeychainTouched, env, run_cli,
                     run_cli_raw, youtube_mock)

from publish_skill import auth, keychain
from publish_skill import credentials as creds
from publish_skill.cli import build_parser
from publish_skill.contract import contract
from publish_skill.doctor import doctor
from publish_skill.errors import PublishError
from publish_skill.metadata import build
from publish_skill.upload import upload

YT_VARS = ("PUBLISH_YOUTUBE_CLIENT_ID", "PUBLISH_YOUTUBE_CLIENT_SECRET", "PUBLISH_YOUTUBE_REFRESH_TOKEN")
ENV_SECRETS = {"PUBLISH_YOUTUBE_CLIENT_ID": "cid-7f3a91e2.apps.example",
               "PUBLISH_YOUTUBE_CLIENT_SECRET": "csecret-Zq81LmP0sW",
               "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//refresh-XyZ0987654321"}
KC_SECRETS = {"PUBLISH_YOUTUBE_CLIENT_ID": "kc-cid-55e1.apps.example",
              "PUBLISH_YOUTUBE_CLIENT_SECRET": "kc-csecret-Hh27Qa",
              "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//kc-refresh-Pp4455667788"}
NO_ENV = {k: None for k in YT_VARS}
ALL_SECRETS = list(ENV_SECRETS.values()) + list(KC_SECRETS.values())


class CredentialRead(BaseException):
    """Raised by the read probes: a credential was read where none may be."""


def _read_probe(*_a, **_k):
    raise CredentialRead("a credential was read")


class Stdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _lines(values) -> str:
    return "".join(v + "\n" for v in values)


class KeychainTestCase(OfflineTestCase):
    def store(self, values=None):
        for n, v in (KC_SECRETS if values is None else values).items():
            self.keychain_backend.items[(keychain.SERVICE, n)] = v

    def stored(self):
        return {a: v for (s, a), v in self.keychain_backend.items.items() if s == keychain.SERVICE}

    def no_keyring(self):
        """keyring not installed: the real discovery runs, and its import finds nothing."""
        for p in (mock.patch.object(keychain, "_discover", REAL_DISCOVER),
                  mock.patch.object(keychain, "_import_keyring", lambda: None)):
            p.start()
            self.addCleanup(p.stop)

    def assert_no_secret(self, text: str, where=None):
        for s in ALL_SECRETS:
            self.assertNotIn(s, text, where)


class KeychainGuardTests(KeychainTestCase):
    def test_the_fake_keychain_is_what_runs(self):
        # CI greps for this test passing (ADR-003): tests never reach a real keychain.
        self.assertIs(keychain.active(), self.keychain)
        self.assertEqual(keychain.availability()["backend"], FAKE_BACKEND_NAME)
        self.assertEqual(doctor(os.path.join(self.tmp, "none"))["checks"]["keychain"]["backend"], FAKE_BACKEND_NAME)
        self.assertIsNot(keychain._discover, REAL_DISCOVER)
        with self.assertRaises(RealKeychainTouched):
            keychain._import_keyring()
        if os.environ.get("PUBLISH_EXPECT_KEYRING") == "1":         # CI jobs that install the keychain extra
            self.assertIsNotNone(importlib.util.find_spec("keyring"), "the keychain extra should be installed here")

    def test_the_real_discovery_is_stopped_loudly(self):
        # Were the fake not installed, the real discovery would hit the guard, and nothing in the package swallows it.
        with mock.patch.object(keychain, "_discover", REAL_DISCOVER):
            with self.assertRaises(RealKeychainTouched):
                keychain.active()
            with self.assertRaises(RealKeychainTouched):
                run_cli_raw("auth", "status", "--json")

    def test_keyring_is_imported_in_one_place_only(self):
        # every import of keyring, with the function that encloses it
        found = set()

        def visit(node, fn):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Import):
                    mods = [a.name for a in child.names]
                elif isinstance(child, ast.ImportFrom):
                    mods = [child.module or ""]
                else:
                    mods = []
                if any(m == "keyring" or m.startswith("keyring.") for m in mods):
                    found.add((f.name, fn))
                visit(child, child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn)
        for f in (ROOT / "src" / "publish_skill").rglob("*.py"):
            visit(ast.parse(f.read_text(encoding="utf-8")), "<module>")
        self.assertEqual(found, {("keychain.py", "_import_keyring")})
        # nor reached another way: keyring's own backend choice, or a dynamic import (docstrings may name them)
        for f in (ROOT / "src" / "publish_skill").rglob("*.py"):
            used = {n.attr if isinstance(n, ast.Attribute) else n.id
                    for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))) if isinstance(n, (ast.Attribute, ast.Name))}
            for banned in ("get_keyring", "set_keyring", "import_module", "__import__"):
                self.assertNotIn(banned, used, f"{f.name}: {banned}")


class _Backend:
    """A stand-in for a keyring backend class: keyring exposes `viable` and `priority` on the class."""
    viable = True
    priority = 1


class _NoPriority(_Backend):
    """keyring's `priority` raises when a backend cannot initialise (e.g. no D-Bus session)."""
    priority = None


class BackendSelectionTests(KeychainTestCase):
    def lib(self, *classes):
        return {"version": "25.7.0", "backends": [(label, cls) for label, cls in classes]}

    def test_the_viable_backend_with_the_highest_priority(self):
        low = type("Low", (_Backend,), {"viable": True, "priority": 1})
        high = type("High", (_Backend,), {"viable": True, "priority": 9})
        dead = type("Dead", (_Backend,), {"viable": False, "priority": 99})
        kc = keychain._select(self.lib(("A", low), ("B", high), ("C", dead)))
        self.assertEqual((kc.name, kc.library), ("B", "keyring 25.7.0"))
        self.assertIsInstance(kc._backend, high)

    def test_no_viable_backend_is_unavailable(self):
        dead = type("Dead", (_Backend,), {"viable": False, "priority": 5})
        with self.assertRaises(keychain.KeychainUnavailable) as e:
            keychain._select(self.lib(("A", dead), ("B", _NoPriority)))
        self.assertEqual(e.exception.library, "keyring 25.7.0")
        self.assertIn("no OS keychain is usable", e.exception.reason)

    def test_only_the_three_os_keychains_are_candidates(self):
        self.assertEqual(keychain.BACKEND_LABELS, ("macOS Keychain", "Windows Credential Manager",
                                                   "Secret Service (Linux, D-Bus)"))
        src = (ROOT / "src" / "publish_skill" / "keychain.py").read_text(encoding="utf-8")
        imported = set(re.findall(r"from keyring\.backends import (\w+)", src))
        self.assertEqual(imported, {"macOS", "Windows", "SecretService"})
        for other in ("fail", "null", "chainer", "kwallet", "libsecret", "keyrings.alt", "PYTHON_KEYRING_BACKEND"):
            self.assertNotIn(f"backends import {other}", src)

    def test_without_keyring_the_keychain_is_unavailable_and_doctor_says_env_only(self):
        self.no_keyring()
        a = keychain.availability()
        self.assertEqual((a["status"], a["backend"], a["install"]), ("UNAVAILABLE", None, keychain.INSTALL))
        self.assertIn("not installed", a["reason"])
        self.assertIsNone(keychain.active())
        with env(**ENV_SECRETS):
            d = doctor(os.path.join(self.tmp, "none"))
            self.assertTrue(creds.load("youtube").get("PUBLISH_YOUTUBE_CLIENT_ID"))     # env-only still works
        self.assertEqual(d["checks"]["keychain"]["status"], "UNAVAILABLE")
        self.assertEqual(d["checks"]["keychain"]["fallback"], "credentials come from the environment only")
        self.assertEqual(d["status"], "degraded")
        self.assertTrue(any(w.startswith("keychain: unavailable") and "environment only" in w and keychain.INSTALL in w
                            for w in d["warnings"]), d["warnings"])
        self.assertEqual(d["checks"]["platforms"]["youtube"]["credentials"]["keychain"]["status"], "UNAVAILABLE")

    def test_publish_keychain_off_turns_it_off_without_discovery(self):
        with mock.patch.object(keychain, "_discover", _read_probe):
            for value in ("off", "0", "FALSE", "no"):
                with env(PUBLISH_KEYCHAIN=value):
                    a = keychain.availability()
                    self.assertEqual(a["status"], "UNAVAILABLE")
                    self.assertIn("PUBLISH_KEYCHAIN", a["reason"])
                    self.assertIsNone(a["install"])
        with env(PUBLISH_KEYCHAIN="on"):
            self.assertIs(keychain.active(), self.keychain)

    def test_require_gives_keychain_unavailable(self):
        self.no_keyring()
        with self.assertRaises(PublishError) as e:
            keychain.require()
        self.assertEqual((e.exception.code, e.exception.exit_code), ("KEYCHAIN_UNAVAILABLE", 3))
        self.assertEqual(e.exception.details["install"], keychain.INSTALL)

    def test_the_wrapper(self):
        kc = self.keychain
        self.assertEqual(kc.get("X"), "")
        kc.set("X", "  v1  ")
        self.assertEqual(self.keychain_backend.items[(keychain.SERVICE, "X")], "  v1  ")
        self.assertEqual(kc.get("X"), "v1")
        self.assertTrue(kc.delete("X"))
        self.assertFalse(kc.delete("X"))
        self.assertEqual({c[1] for c in self.keychain_backend.calls}, {keychain.SERVICE})
        kc.set("Y", "secret-value-42")
        for op, call in (("get", lambda: kc.get("Y")), ("set", lambda: kc.set("Y", "secret-value-43")),
                         ("delete", lambda: kc.delete("Y"))):
            self.keychain_backend.fail = {op}
            with self.assertRaises(PublishError) as e:
                call()
            self.assertEqual(e.exception.code, "KEYCHAIN_ERROR")
            self.assertNotIn("secret-value", json.dumps(e.exception.to_dict()))
            self.assertNotIn(f"{op} refused", e.exception.message)                    # the backend's text is not passed on


class PrecedenceTests(KeychainTestCase):
    def test_keychain_only(self):
        self.store()
        with env(**NO_ENV):
            st = creds.status("youtube")
            c = creds.load("youtube")
        self.assertEqual((st["source"], st["complete"]), ("keychain", True))
        self.assertEqual(st["variables"], {v: "present" for v in YT_VARS})
        self.assertEqual(st["environment"], {v: "absent" for v in YT_VARS})
        self.assertEqual(c.source, "keychain")
        self.assertEqual({n: c.get(n) for n in YT_VARS}, KC_SECRETS)
        self.assert_no_secret(repr(c) + json.dumps(st))

    def test_the_environment_wins_as_a_whole(self):
        self.store()
        with env(**ENV_SECRETS):
            st = creds.status("youtube")
            c = creds.load("youtube")
        self.assertEqual((st["source"], c.source), ("environment", "environment"))
        self.assertEqual({n: c.get(n) for n in YT_VARS}, ENV_SECRETS)
        self.assertIn("not consulted", st["note"])

    def test_a_partial_environment_never_mixes_with_the_keychain(self):
        self.store()
        with env(**dict(NO_ENV, PUBLISH_YOUTUBE_CLIENT_ID=ENV_SECRETS["PUBLISH_YOUTUBE_CLIENT_ID"])):
            st = creds.status("youtube")
            with self.assertRaises(PublishError) as e:
                creds.load("youtube")
        self.assertEqual((st["source"], st["complete"]), ("environment", False))
        self.assertEqual(e.exception.code, "CREDENTIALS_MISSING")
        self.assertEqual(e.exception.details, {"platform": "youtube", "missing": list(YT_VARS[1:]), "source": "environment"})
        self.assertIn("keychain is not consulted", e.exception.message)
        self.assert_no_secret(json.dumps(e.exception.to_dict()))

    def test_a_partial_keychain_names_what_is_missing(self):
        self.store({"PUBLISH_YOUTUBE_CLIENT_ID": KC_SECRETS["PUBLISH_YOUTUBE_CLIENT_ID"]})
        with env(**NO_ENV):
            st = creds.status("youtube")
            with self.assertRaises(PublishError) as e:
                creds.load("youtube")
        self.assertEqual((st["source"], st["complete"]), ("keychain", False))
        self.assertEqual(e.exception.details["missing"], list(YT_VARS[1:]))
        self.assertEqual(e.exception.details["source"], "keychain")
        self.assertIn("publish auth login --platform youtube", e.exception.message)

    def test_neither_source(self):
        with env(**NO_ENV):
            st = creds.status("youtube")
            with self.assertRaises(PublishError) as e:
                creds.load("youtube")
        self.assertEqual((st["source"], st["complete"]), (None, False))
        self.assertEqual(st["variables"], {v: "absent" for v in YT_VARS})
        self.assertEqual((e.exception.details["missing"], e.exception.details["source"]), (list(YT_VARS), None))

    def test_neither_source_and_no_keychain(self):
        self.no_keyring()
        with env(**NO_ENV):
            with self.assertRaises(PublishError) as e:
                creds.load("youtube")
        self.assertEqual(e.exception.code, "CREDENTIALS_MISSING")
        self.assertIn("unavailable", e.exception.message)

    def test_a_keychain_error_is_reported_not_crashed_on(self):
        self.store()
        self.keychain_backend.fail = {"get"}
        with env(**NO_ENV):
            st = creds.status("youtube")
            with self.assertRaises(PublishError) as e:
                creds.load("youtube")
            rc, doc = run_cli("doctor", "--json", "--ffmpeg-skill", os.path.join(self.tmp, "none"))
        self.assertEqual((st["keychain"]["status"], st["source"]), ("ERROR", None))
        self.assertEqual((e.exception.code, e.exception.exit_code), ("KEYCHAIN_ERROR", 3))
        self.assertEqual((rc, doc["checks"]["platforms"]["youtube"]["credentials"]["keychain"]["status"]), (0, "ERROR"))

    def test_upload_reads_the_keychain_only_after_the_gate(self):
        self.store()
        m = youtube_mock(self, KC_SECRETS)                 # accepts only the keychain's values
        token = upload(self.video, "youtube")["confirm_token"]
        with env(**NO_ENV):
            rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", "--confirm", token, "--json")
        doc = json.loads(out)
        self.assertEqual((rc, doc["uploaded"]), (0, True))       # the keychain's credentials reached the uploader
        self.assertEqual(m.kinds()[0], "token")
        self.assert_no_secret(out + err)
        for s in [m.access_token] + m.upload_ids:
            self.assertNotIn(s, out + err)


class RefusalBeforeReadTests(KeychainTestCase):
    """Every refusal happens before any credential is read: from the environment, the keychain or stdin."""

    def probes(self):
        self.store()
        for p in (mock.patch.object(creds, "_env_value", _read_probe),
                  mock.patch.object(self.keychain_backend, "get_password", _read_probe),
                  mock.patch.object(self.keychain_backend, "set_password", _read_probe),
                  mock.patch.object(self.keychain_backend, "delete_password", _read_probe)):
            p.start()
            self.addCleanup(p.stop)

    def test_upload_refusals(self):
        token = upload(self.video, "youtube")["confirm_token"]
        bad_md = build(title="x" * 101)
        self.probes()
        for call, code in ((lambda: upload(self.video, "myspace"), "UNKNOWN_PLATFORM"),
                           (lambda: upload(os.path.join(self.tmp, "nope.mp4"), "youtube"), "INPUT_NOT_FOUND"),
                           (lambda: upload(self.video, "youtube", metadata=bad_md), "METADATA_INVALID"),
                           (lambda: upload(self.video, "youtube", confirm=token + "0"), "CONFIRM_MISMATCH"),
                           (lambda: upload(self.video, "youtube", confirm=token, metadata=bad_md), "METADATA_INVALID")):
            with self.assertRaises(PublishError) as e:
                call()
            self.assertEqual(e.exception.code, code)
        with self.assertRaises(CredentialRead):              # control: past the gate, the first read hits the probe
            upload(self.video, "youtube", confirm=token)

    def test_auth_refusals(self):
        self.probes()
        stdin = mock.Mock(isatty=_read_probe, readline=_read_probe, read=_read_probe)
        for call, code in ((lambda: auth.login("myspace", stdin=stdin), "UNKNOWN_PLATFORM"),
                           (lambda: auth.login("youtube", ["PUBLISH_X"], stdin=stdin), "INVALID_ARGUMENT"),
                           (lambda: auth.login("youtube", [YT_VARS[0], YT_VARS[0]], stdin=stdin), "INVALID_ARGUMENT"),
                           (lambda: auth.logout("myspace"), "UNKNOWN_PLATFORM"),
                           (lambda: auth.logout("youtube", ["PUBLISH_X"]), "INVALID_ARGUMENT"),
                           (lambda: auth.status("myspace"), "UNKNOWN_PLATFORM")):
            with self.assertRaises(PublishError) as e:
                call()
            self.assertEqual(e.exception.code, code)
        self.no_keyring()
        for call in (lambda: auth.login("youtube", stdin=stdin), lambda: auth.logout("youtube")):
            with self.assertRaises(PublishError) as e:
                call()
            self.assertEqual(e.exception.code, "KEYCHAIN_UNAVAILABLE")

    def test_plan_contract_and_the_redactor_never_read_the_keychain(self):
        self.store()
        for p in (mock.patch.object(self.keychain_backend, "get_password", _read_probe),):
            p.start()
            self.addCleanup(p.stop)
        for argv in (("contract", "--json"), ("plan", self.video, "--platform", "youtube", "--ffmpeg-skill",
                                              os.path.join(self.tmp, "none"), "--json")):
            rc, out, _ = run_cli_raw(*argv)
            json.loads(out)
        self.assertEqual(creds.redact({"a": "b"}), {"a": "b"})


class LoginTests(KeychainTestCase):
    def login(self, stdin, *argv):
        with mock.patch.object(sys, "stdin", stdin):
            return run_cli_raw("auth", "login", "--platform", "youtube", *argv)

    def test_values_from_stdin_one_per_line(self):
        with env(**NO_ENV):
            rc, out, err = self.login(Stdin(_lines(KC_SECRETS.values())), "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, out)
        self.assertEqual((doc["auth"], doc["stored"], doc["backend"]), ("login", list(YT_VARS), FAKE_BACKEND_NAME))
        self.assertEqual(self.stored(), KC_SECRETS)
        self.assertEqual((doc["credentials"]["source"], doc["credentials"]["complete"]), ("keychain", True))
        self.assertEqual(doc["shadowed_by_environment"], [])
        self.assert_no_secret(out + err)

    def test_crlf_and_blanks_around_values_are_stripped(self):
        with env(**NO_ENV):
            rc, out, _ = self.login(Stdin("".join(f"  {v}\r\n" for v in KC_SECRETS.values())), "--json")
        self.assertEqual((rc, self.stored()), (0, KC_SECRETS))

    def test_a_hidden_prompt_on_a_terminal(self):
        answers = iter(KC_SECRETS.values())
        prompts = []

        def fake_getpass(prompt, stream=None):
            prompts.append((prompt, stream is sys.stderr))                # the prompt goes to stderr, never stdout
            return next(answers)
        with env(**NO_ENV), mock.patch.object(auth.getpass, "getpass", fake_getpass):
            with mock.patch.object(sys, "stdin", Stdin(tty=True)):
                rc, out, err = run_cli_raw("auth", "login", "--platform", "youtube", "--json")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.stored(), KC_SECRETS)
        self.assertEqual([p for p, _ in prompts], [f"{n} (input hidden): " for n in YT_VARS])
        self.assertTrue(all(to_stderr for _, to_stderr in prompts))
        self.assertEqual(json.loads(out)["stored"], list(YT_VARS))
        self.assert_no_secret(out + err)

    def test_one_variable(self):
        rc, out, _ = self.login(Stdin("only-the-token\n"), "--variable", "PUBLISH_YOUTUBE_REFRESH_TOKEN", "--json")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.stored(), {"PUBLISH_YOUTUBE_REFRESH_TOKEN": "only-the-token"})
        self.assertEqual(json.loads(out)["stored"], ["PUBLISH_YOUTUBE_REFRESH_TOKEN"])

    def test_short_or_empty_input_stores_nothing_and_prints_no_value(self):
        first = KC_SECRETS["PUBLISH_YOUTUBE_CLIENT_ID"]
        for text in (first + "\n", first + "\n\n" + "x\n", ""):
            rc, out, err = self.login(Stdin(text), "--json")
            doc = json.loads(out)
            self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"), text)
            self.assertIn("nothing was stored", doc["error"]["message"])
            self.assertEqual(self.stored(), {})
            self.assert_no_secret(out + err)

    def test_no_flag_or_argument_takes_a_value(self):
        login = build_parser()._subparsers._group_actions[0].choices["auth"]._subparsers._group_actions[0].choices["login"]
        takes_value = {a.dest for a in login._actions if a.option_strings and a.nargs != 0}
        self.assertEqual(takes_value, {"platform", "variables"})                # names only
        self.assertEqual([a.dest for a in login._actions if not a.option_strings], [])  # no positional
        for argv in (("--value", "s3cret"), ("s3cret",), ("--token", "s3cret"), ("--password", "s3cret")):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr", io.StringIO()):
                build_parser().parse_args(["auth", "login", "--platform", "youtube", *argv])
        rc, out, _ = self.login(Stdin(""), "--variable", "s3cret-pasted-here", "--json")      # a value in --variable
        doc = json.loads(out)
        self.assertEqual(doc["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(self.stored(), {})

    def test_a_store_failure_says_what_was_stored(self):
        backend = self.keychain_backend
        real_set = backend.set_password

        def second_fails(service, account, value):
            if account == YT_VARS[1]:
                raise RuntimeError("locked " + value)                    # even a backend that echoes the value
            real_set(service, account, value)
        backend.set_password = second_fails
        rc, out, err = self.login(Stdin(_lines(KC_SECRETS.values())), "--json")
        doc = json.loads(out)
        self.assertEqual((rc, doc["error"]["code"]), (3, "KEYCHAIN_ERROR"))
        self.assertEqual(doc["error"]["details"]["stored"], [YT_VARS[0]])
        self.assert_no_secret(out + err)

    def test_the_environment_shadowing_the_keychain_is_a_warning(self):
        with env(**dict(NO_ENV, PUBLISH_YOUTUBE_CLIENT_ID=ENV_SECRETS["PUBLISH_YOUTUBE_CLIENT_ID"])):
            rc, out, err = self.login(Stdin(_lines(KC_SECRETS.values())), "--json")
        doc = json.loads(out)
        self.assertEqual(doc["shadowed_by_environment"], ["PUBLISH_YOUTUBE_CLIENT_ID"])
        self.assertIn("not consulted", doc["warnings"][0])
        self.assert_no_secret(out + err)

    def test_keychain_unavailable(self):
        self.no_keyring()
        stdin = Stdin(_lines(KC_SECRETS.values()))
        rc, out, _ = self.login(stdin, "--json")
        doc = json.loads(out)
        self.assertEqual((rc, doc["error"]["code"]), (3, "KEYCHAIN_UNAVAILABLE"))
        self.assertEqual(stdin.tell(), 0)                                   # stdin was not read
        self.assertIn(keychain.INSTALL, doc["error"]["message"])


class LogoutAndStatusTests(KeychainTestCase):
    def test_logout(self):
        self.store({"PUBLISH_YOUTUBE_CLIENT_ID": "a-client-id-1"})
        with env(**NO_ENV):
            rc, doc = run_cli("auth", "logout", "--platform", "youtube", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual((doc["removed"], doc["not_found"]), (["PUBLISH_YOUTUBE_CLIENT_ID"], list(YT_VARS[1:])))
        self.assertEqual(self.stored(), {})
        self.assertEqual(doc["environment_still_set"], [])

    def test_logout_warns_when_the_environment_still_sets_a_variable(self):
        self.store()
        with env(**ENV_SECRETS):
            rc, out, err = run_cli_raw("auth", "logout", "--platform", "youtube", "--variable", YT_VARS[2], "--json")
        doc = json.loads(out)
        self.assertEqual(doc["removed"], [YT_VARS[2]])
        self.assertEqual(doc["environment_still_set"], list(YT_VARS))
        self.assertEqual(sorted(self.stored()), sorted(YT_VARS[:2]))
        self.assert_no_secret(out + err)

    def test_status(self):
        self.store()
        with env(**NO_ENV):
            rc, out, err = run_cli_raw("auth", "status", "--json")
            rc2, out2, err2 = run_cli_raw("auth", "status", "--platform", "youtube")      # human output
        doc = json.loads(out)
        self.assertEqual((rc, rc2), (0, 0))
        self.assertEqual(doc["keychain"]["status"], "OK")
        self.assertEqual(doc["platforms"]["youtube"]["source"], "keychain")
        self.assertIn("source=keychain complete=True", out2)
        self.assert_no_secret(out + err + out2 + err2)
        for v in json.dumps(doc["platforms"]).split('"'):
            self.assertNotRegex(v, r"^(kc-|1//)")


class SecretsNeverPrintedWithTheKeychainTests(KeychainTestCase):
    def test_no_command_prints_a_keychain_credential(self):
        self.store()
        m = youtube_mock(self, KC_SECRETS)
        token = upload(self.video, "youtube")["confirm_token"]
        argvs = [("contract", "--json"), ("doctor", "--json"), ("platforms", "--json"), ("auth", "status", "--json"),
                 ("upload", self.video, "--platform", "youtube", "--json"),
                 ("upload", self.video, "--platform", "youtube", "--confirm", token, "--json"),
                 ("upload", self.video, "--platform", "youtube", "--confirm", "nope", "--json"),
                 ("doctor",), ("platforms",), ("auth", "status"), ("upload", self.video, "--platform", "youtube"),
                 ("upload", self.video, "--platform", "youtube", "--confirm", token)]
        with env(**NO_ENV):
            for argv in argvs:
                rc, out, err = run_cli_raw(*argv)
                if "--json" in argv:
                    json.loads(out)
                self.assert_no_secret(out + err, argv)
                for s in [m.access_token] + m.upload_ids:
                    self.assertNotIn(s, out + err, argv)
        self.assertEqual(len(m.videos), 2)                 # both confirmed runs (JSON and human) uploaded

    def test_a_keychain_value_that_reaches_a_message_is_redacted(self):
        self.store()
        with env(**NO_ENV):
            creds.load("youtube")                                   # this process now holds the keychain values
            doc = creds.redact({"msg": "oops " + KC_SECRETS["PUBLISH_YOUTUBE_REFRESH_TOKEN"]})
        self.assertEqual(doc, {"msg": "oops [REDACTED]"})


class PackagingTests(unittest.TestCase):
    def test_keyring_is_an_optional_extra_with_a_minimum_version(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^dependencies = \[\]$")                 # the core stays stdlib-only
        self.assertRegex(text, r'(?m)^keychain = \["keyring>=25\.0"\]$')
        self.assertEqual(keychain.REQUIREMENT, "keyring>=25.0")
        self.assertEqual(keychain.INSTALL, "pip install 'publish-skill[keychain]'")
        c = contract()["credentials"]["keychain"]
        self.assertEqual((c["requirement"], c["optional_extra"]), ("keyring>=25.0", "keychain"))

    def test_docs_name_the_extra_and_the_precedence(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        decisions = (ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")
        self.assertTrue(re.search(r"(?m)^## ADR-003 ", decisions), "docs/decisions.md has no ADR-003")
        for name, text in (("README.md", readme), ("docs/decisions.md", decisions)):
            for needle in ("publish-skill[keychain]", "keyring>=25.0", "PUBLISH_KEYCHAIN=off"):
                self.assertTrue(needle in text, f"{name} does not mention {needle}")
        self.assertIn("publish auth login", (ROOT / "SKILL.md").read_text(encoding="utf-8"))

    def test_the_auth_tool_is_offline(self):
        tools = {t["tool_id"]: t for t in contract()["tools"]}
        self.assertFalse(tools["publish/auth"]["network"])
        self.assertTrue(tools["publish/auth"]["implemented"])


if __name__ == "__main__":
    unittest.main()

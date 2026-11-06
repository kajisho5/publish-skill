"""Roadmap step 10 (ADR-013): restricted delivery for organisers. Unlisted or private uploads (YouTube unlisted /
private; Vimeo password or domain-limited embed) and a delivery receipt per session (URL, privacy, sha256 of the
uploaded file). Uploads reach only the loopback mocks; no password or credential reaches an output or a file."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import unittest
from unittest import mock

from helpers import OfflineTestCase, run_cli, run_cli_raw, vimeo_mock, youtube_mock

from publish_skill import credentials as creds
from publish_skill import limits as L
from publish_skill.metadata import Metadata, build
from publish_skill.plan import metadata_checks
from publish_skill.platforms import vimeo
from publish_skill.upload import confirm_token

YT = {"PUBLISH_YOUTUBE_CLIENT_ID": "cid-org-31.apps.example", "PUBLISH_YOUTUBE_CLIENT_SECRET": "csecret-org-Hh2",
      "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//refresh-org-5150"}
VM_TOKEN = "vimeo-pat-organiser-8d1f"
VIDEO_PASSWORD = "Hall-B-2026"


class RestrictedTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, dict(YT, PUBLISH_VIMEO_ACCESS_TOKEN=VM_TOKEN))
        p.start()
        self.addCleanup(p.stop)
        self.files = []
        for i in range(1, 4):
            path = os.path.join(self.tmp, f"session-{i}.mp4")
            with open(path, "wb") as fh:
                fh.write(b"\x00\x00\x00\x18ftypmp42" + bytes([40 + i]) * (5000 + i))
            self.files.append(path)

    def manifest(self, doc, name="sessions.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def series(self, manifest, platform, *extra):
        rc, out, err = run_cli_raw("series", manifest, "--platform", platform, *extra, "--json")
        return rc, json.loads(out), out + err

    def confirm_all(self, manifest, platform):
        rc, dry, _ = self.series(manifest, platform)
        self.assertEqual(rc, 0, dry)
        return [a for c in dry["clips"] for a in ("--confirm", c["confirm_token"])]

    def sha(self, i):
        with open(self.files[i - 1], "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()


class ReceiptTests(RestrictedTestCase):
    def test_three_sessions_write_a_receipt_with_three_urls_and_their_privacy(self):
        # Done when: a mock run of three sessions writes a receipt with three URLs and their privacy
        m = youtube_mock(self, YT)
        man = self.manifest({"privacy": "unlisted", "sessions": [
            {"id": "S1-01", "file": "session-1.mp4", "title": "Opening keynote"},
            {"id": "S1-02", "file": "session-2.mp4", "title": "Panel (speakers only)", "privacy": "private"},
            {"id": "S1-03", "file": "session-3.mp4", "title": "Closing"}]})
        out = os.path.join(self.tmp, "receipt.json")
        rc, doc, text = self.series(man, "youtube", *self.confirm_all(man, "youtube"), "--receipt", out)
        self.assertEqual((rc, doc["complete"], doc["receipt"]), (0, True, out))
        with open(out, encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual((rec["schema"], rec["platform"], rec["complete"], rec["total"], rec["uploaded"]),
                         ("publish/delivery-receipt@1", "youtube", True, 3, 3))
        rows = rec["sessions"]
        self.assertEqual([r["id"] for r in rows], ["S1-01", "S1-02", "S1-03"])
        self.assertEqual([r["privacy"] for r in rows], ["unlisted", "private", "unlisted"])
        urls = [r["url"] for r in rows]
        self.assertEqual(len(set(urls)), 3)
        self.assertTrue(all(u.startswith("https://www.youtube.com/watch?v=") for u in urls))
        self.assertEqual([r["sha256"] for r in rows], [self.sha(i) for i in (1, 2, 3)])
        # the URL of each row is the video that holds exactly that session's bytes, with that privacy
        for r in rows:
            v = m.videos[r["video_id"]]
            self.assertEqual(hashlib.sha256(v["data"]).hexdigest(), r["sha256"])
            self.assertEqual(v["resource"]["status"]["privacyStatus"], r["privacy"])
        self.assertEqual(rows[0]["access"], "anyone with the link")
        for s in list(YT.values()) + [m.access_token] + m.upload_ids:
            self.assertNotIn(s, text + json.dumps(rec))

    def test_the_receipt_as_csv_and_after_a_failure(self):
        m = youtube_mock(self, YT)
        man = self.manifest({"privacy": "unlisted", "sessions": [
            {"file": f"session-{i}.mp4", "title": f"Talk {i}, room “A”"} for i in (1, 2, 3)]})
        out = os.path.join(self.tmp, "receipt.csv")
        tokens = self.confirm_all(man, "youtube")
        m.faults["init:videos"] = [None, ("status", 403, "quotaExceeded")]
        rc, doc, _ = self.series(man, "youtube", *tokens, "--receipt", out)
        self.assertEqual((rc, doc["error"]["code"]), (4, "QUOTA_EXCEEDED"))
        with open(out, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([(r["index"], r["status"]) for r in rows], [("1", "uploaded"), ("2", "pending"), ("3", "pending")])
        self.assertEqual(rows[0]["title"], "Talk 1, room “A”")
        self.assertTrue(rows[0]["url"].startswith("https://") and rows[1]["url"] == "")
        rc, doc, _ = self.series(man, "youtube", *tokens, "--receipt", out)
        self.assertEqual(rc, 0, doc)
        with open(out, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["status"] for r in rows], ["uploaded"] * 3)
        self.assertEqual(len(m.videos), 3)

    def test_clips_or_sessions_not_both(self):
        man = self.manifest({"clips": [{"file": "session-1.mp4"}], "sessions": [{"file": "session-2.mp4"}]}, "both.json")
        rc, doc, _ = self.series(man, "youtube")
        self.assertEqual(doc["error"]["code"], "INVALID_ARGUMENT")


class VimeoPasswordTests(RestrictedTestCase):
    def test_password_privacy_sends_the_password_and_never_shows_it(self):
        m = vimeo_mock(self, VM_TOKEN)
        os.environ[vimeo.PASSWORD_ENV] = VIDEO_PASSWORD
        flags = ("--title", "Members' session", "--privacy", "password")
        rc, dry = run_cli("upload", self.files[0], "--platform", "vimeo", *flags, "--json")
        self.assertEqual((rc, dry["credentials"]["optional"]), (0, {vimeo.PASSWORD_ENV: "present"}))
        rc, out, err = run_cli_raw("upload", self.files[0], "--platform", "vimeo", *flags, "--confirm",
                                   dry["confirm_token"], "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.created[0]["privacy"], {"view": "password"})
        self.assertEqual(m.created[0]["password"], VIDEO_PASSWORD)          # it reached Vimeo...
        self.assertNotIn(VIDEO_PASSWORD, out + err)                         # ...and nothing else
        self.assertEqual((doc["result"]["privacy"], doc["result"]["sent"]["password"]), ("password", "[REDACTED]"))
        # the token binds the privacy, not the password
        self.assertEqual(confirm_token("vimeo", dry["input"]["sha256"], dry["input"]["size"],
                                       Metadata(title="Members' session", privacy="password")), dry["confirm_token"])

    def test_without_the_password_nothing_is_sent(self):
        m = vimeo_mock(self, VM_TOKEN)
        os.environ.pop(vimeo.PASSWORD_ENV, None)
        rc, dry = run_cli("upload", self.files[0], "--platform", "vimeo", "--privacy", "password", "--json")
        self.assertEqual(dry["credentials"]["optional"], {vimeo.PASSWORD_ENV: "absent"})
        rc, doc = run_cli("upload", self.files[0], "--platform", "vimeo", "--privacy", "password", "--confirm",
                          dry["confirm_token"], "--json")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["missing"]),
                         (3, "CREDENTIALS_MISSING", [vimeo.PASSWORD_ENV]))
        self.assertEqual(m.requests, [])
        os.environ[vimeo.PASSWORD_ENV] = "x" * 33
        rc, doc = run_cli("upload", self.files[0], "--platform", "vimeo", "--privacy", "password", "--confirm",
                          dry["confirm_token"], "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertEqual(m.requests, [])

    def test_the_password_from_the_keychain(self):
        m = vimeo_mock(self, VM_TOKEN)
        os.environ.pop("PUBLISH_VIMEO_ACCESS_TOKEN")
        self.keychain_backend.items[("publish-skill", "PUBLISH_VIMEO_ACCESS_TOKEN")] = VM_TOKEN
        self.keychain_backend.items[("publish-skill", vimeo.PASSWORD_ENV)] = VIDEO_PASSWORD
        rc, dry = run_cli("upload", self.files[0], "--platform", "vimeo", "--privacy", "password", "--json")
        rc, out, err = run_cli_raw("upload", self.files[0], "--platform", "vimeo", "--privacy", "password", "--confirm",
                                   dry["confirm_token"], "--json")
        self.assertEqual(rc, 0, out)
        self.assertEqual(m.created[0]["password"], VIDEO_PASSWORD)
        self.assertNotIn(VIDEO_PASSWORD, out + err)

    def test_auth_login_takes_the_optional_variable_by_name_only(self):
        import io
        from publish_skill import auth
        doc = auth.login("vimeo", [vimeo.PASSWORD_ENV], stdin=io.StringIO(VIDEO_PASSWORD + "\n"))
        self.assertEqual(doc["stored"], [vimeo.PASSWORD_ENV])
        self.assertNotIn(VIDEO_PASSWORD, json.dumps(creds.redact(doc)))
        doc = auth.login("vimeo", stdin=io.StringIO("tok\n"))
        self.assertEqual(doc["stored"], ["PUBLISH_VIMEO_ACCESS_TOKEN"])            # the default: required ones only

    def test_only_vimeo_offers_password_and_domains(self):
        for privacy in ("password", "domains"):
            row = [r for r in metadata_checks("youtube", build(privacy=privacy)) if r["check"] == "privacy"][0]
            self.assertEqual(row["status"], "FAIL")
        self.assertEqual(L.limits_of("vimeo")["privacy.values"]["maps_to"], vimeo.PRIVACY_VIEW)
        self.assertIn("32 characters", L.limits_of("vimeo")["privacy.password"]["quote"])


class VimeoDomainTests(RestrictedTestCase):
    def test_domain_limited_privacy_allowlists_each_domain(self):
        m = vimeo_mock(self, VM_TOKEN)
        flags = ("--title", "Sponsor session", "--privacy", "domains", "--embed-domain", "events.example.org",
                 "--embed-domain", "intranet.example.com")
        rc, dry = run_cli("upload", self.files[0], "--platform", "vimeo", *flags, "--json")
        self.assertEqual(rc, 0, dry)
        rc, doc = run_cli("upload", self.files[0], "--platform", "vimeo", *flags, "--confirm", dry["confirm_token"], "--json")
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.created[0]["privacy"], {"view": "disable", "embed": "whitelist"})
        vid = doc["result"]["video_id"]
        self.assertEqual(m.domains[vid], ["events.example.org", "intranet.example.com"])
        self.assertEqual((doc["result"]["privacy"], doc["result"]["embed_domains"]),
                         ("domains", ["events.example.org", "intranet.example.com"]))
        domain_reqs = [r for r in m.requests if r["kind"] == "domain"]
        self.assertTrue(all(r["headers"]["authorization"] == "bearer " + VM_TOKEN for r in domain_reqs))

    def test_a_failed_domain_is_upload_partial_and_names_what_was_added(self):
        m = vimeo_mock(self, VM_TOKEN)
        m.faults["domain"] = [None, ("status", 400, 2204)]
        flags = ("--privacy", "domains", "--embed-domain", "a.example.org", "--embed-domain", "b.example.org")
        rc, dry = run_cli("upload", self.files[0], "--platform", "vimeo", *flags, "--json")
        rc, doc = run_cli("upload", self.files[0], "--platform", "vimeo", *flags, "--confirm", dry["confirm_token"], "--json")
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_PARTIAL"))
        self.assertEqual(doc["error"]["details"]["domains_added"], ["a.example.org"])
        self.assertIn("do not run upload again", doc["error"]["message"])
        self.assertEqual(len(m.videos), 1)

    def test_a_retriable_domain_answer_is_retried(self):
        m = vimeo_mock(self, VM_TOKEN)
        m.faults["domain"] = [("status", 503, None)]
        flags = ("--privacy", "domains", "--embed-domain", "a.example.org")
        rc, dry = run_cli("upload", self.files[0], "--platform", "vimeo", *flags, "--json")
        rc, doc = run_cli("upload", self.files[0], "--platform", "vimeo", *flags, "--confirm", dry["confirm_token"], "--json")
        self.assertEqual(rc, 0, doc)
        self.assertEqual([r["kind"] for r in m.requests].count("domain"), 2)

    def test_plan_rows(self):
        def rows(md):
            return [r for r in metadata_checks("vimeo", md) if r["check"] == "embed domains"]
        self.assertEqual(rows(build(privacy="domains"))[0]["status"], "FAIL")
        self.assertEqual(rows(build(privacy="unlisted", embed_domains=["a.org"]))[0]["status"], "FAIL")
        for bad in ("https://a.org", "a.org/path", "a.org:8080", "localhost", "bücher.example", "-a.org"):
            self.assertEqual(rows(build(privacy="domains", embed_domains=[bad]))[0]["status"], "FAIL", bad)
        self.assertEqual(rows(build(privacy="domains", embed_domains=["a.org", "A.org"]))[0]["status"], "FAIL")
        self.assertEqual(rows(build(privacy="domains", embed_domains=["xn--bcher-kva.example", "a.b.org"]))[0]["status"],
                         "PASS")
        self.assertEqual(rows(build(privacy="unlisted")), [])
        yt = [r for r in metadata_checks("youtube", build(embed_domains=["a.org"])) if "embed_domains" in r["check"]]
        self.assertEqual(yt[0]["status"], "FAIL")                                 # not sent to YouTube

    def test_a_vimeo_series_of_sessions_with_mixed_restrictions_and_a_receipt(self):
        m = vimeo_mock(self, VM_TOKEN)
        os.environ[vimeo.PASSWORD_ENV] = VIDEO_PASSWORD
        man = self.manifest({"sessions": [
            {"id": "A", "file": "session-1.mp4", "title": "Keynote", "privacy": "unlisted"},
            {"id": "B", "file": "session-2.mp4", "title": "Workshop", "privacy": "password"},
            {"id": "C", "file": "session-3.mp4", "title": "Sponsor", "privacy": "domains",
             "embed_domains": ["events.example.org"]}]}, "vimeo-sessions.json")
        out = os.path.join(self.tmp, "vimeo-receipt.json")
        rc, doc, text = self.series(man, "vimeo", *self.confirm_all(man, "vimeo"), "--receipt", out)
        self.assertEqual(rc, 0, doc)
        with open(out, encoding="utf-8") as fh:
            raw = fh.read()
        rows = json.loads(raw)["sessions"]
        self.assertEqual([(r["id"], r["privacy"]) for r in rows], [("A", "unlisted"), ("B", "password"), ("C", "domains")])
        self.assertEqual([r["url"] for r in rows], [f"https://vimeo.com/{v}" for v in sorted(m.videos)])
        self.assertEqual(rows[2]["access"], "embedded on events.example.org only")
        self.assertIn("password (shared separately)", rows[1]["access"])
        for secret in (VIDEO_PASSWORD, VM_TOKEN):
            self.assertNotIn(secret, raw + text)
        with open(man + ".publish-state.json", encoding="utf-8") as fh:
            self.assertNotIn(VIDEO_PASSWORD, fh.read())


if __name__ == "__main__":
    unittest.main()

"""The LinkedIn uploader (roadmap step 5, ADR-008), entirely against tests/mock_linkedin.py: an in-process mock of the
LinkedIn Videos API multipart upload (initializeUpload, the signed part PUTs, finalizeUpload, the video's status) and
the Posts API on 127.0.0.1. Parts and their ETags, a resent part after an interruption (a lost answer and a dropped
part), a timeout, backoff on 5xx and 429 with Retry-After, processing polled until AVAILABLE or PROCESSING_FAILED, a
post never sent twice, refused tokens, the escaped commentary, and that exactly the metadata the confirmation token
bound is what arrives. No test contacts LinkedIn: the socket guard lets a test reach only the loopback port of the mock
it started, and the guard test below asserts that it blocks every other host."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from unittest import mock

from helpers import LOOPBACK_PORTS, NetworkAttempted, OfflineTestCase, linkedin_mock, run_cli, run_cli_raw

from publish_skill import credentials as creds
from publish_skill import limits as L
from publish_skill.errors import PublishError
from publish_skill.metadata import Metadata, build
from publish_skill.plan import duration_checks, file_checks, metadata_checks, plan
from publish_skill.platforms import UploadRequest, get_platform, linkedin
from publish_skill.upload import confirm_token, upload

TOKEN_VAR, AUTHOR_VAR = "PUBLISH_LINKEDIN_ACCESS_TOKEN", "PUBLISH_LINKEDIN_AUTHOR"
TOKEN = "AQVlinkedin-member-token-Zx81LmP0sWq9Rt"
AUTHOR = "urn:li:person:Ab3dEf9GhIj"
UNIT = 256 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
PUBLIC = ("--privacy", "public")


def unescape(little):
    """`little` plain text back to the text: a backslash takes the next character literally."""
    out, i = [], 0
    while i < len(little):
        if little[i] == "\\" and i + 1 < len(little):
            i += 1
        out.append(little[i])
        i += 1
    return "".join(out)


class LinkedInTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.video = os.path.join(self.tmp, "final.mp4")         # 2.5 parts of 256 KiB: 3 parts
        with open(self.video, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2 * UNIT + UNIT // 2 - 12))
        self.size = os.path.getsize(self.video)
        p = mock.patch.dict(os.environ, {TOKEN_VAR: TOKEN, AUTHOR_VAR: AUTHOR})
        p.start()
        self.addCleanup(p.stop)

    def file(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def mock(self, author=AUTHOR, **kw):
        self.m = linkedin_mock(self, TOKEN, author, **kw)
        return self.m

    def confirmed(self, *flags, human=False, video=None):
        """Dry run, then the confirmed upload with the same flags (public, unless flags say otherwise)."""
        video = video or self.video
        flags = flags if "--privacy" in flags else flags + PUBLIC
        rc, dry = run_cli("upload", video, "--platform", "linkedin", *flags, "--json")
        self.assertEqual(rc, 0, dry)
        argv = ["upload", video, "--platform", "linkedin", *flags, "--confirm", dry["confirm_token"]]
        rc, out, err = run_cli_raw(*(argv if human else argv + ["--json"]))
        return rc, (None if human else json.loads(out)), out + err

    def data(self):
        with open(self.video, "rb") as fh:
            return fh.read()


class LinkedInLoopbackOnlyTests(LinkedInTestCase):
    def test_the_guard_blocks_every_non_loopback_host_for_linkedin(self):
        m = self.mock()
        self.assertEqual(LOOPBACK_PORTS, {m.port})
        s = socket.create_connection(("127.0.0.1", m.port))           # the mock: allowed
        s.close()
        for address in (("api.linkedin.com", 443), ("www.linkedin.com", 443), ("linkedin.com", 443),
                        ("dms.licdn.com", 443), ("www.linkedin-ei.com", 443), ("13.107.42.14", 443),
                        ("127.0.0.1", m.port + 1), ("::1", m.port)):
            with self.assertRaises(NetworkAttempted, msg=address):
                socket.create_connection(address)
        for host in ("api.linkedin.com", "localhost"):
            with self.assertRaises(NetworkAttempted):
                socket.getaddrinfo(host, m.port)

    def test_the_real_endpoint_is_linkedins_and_unreachable_from_a_test(self):
        self.assertEqual(linkedin.API_BASE, "https://api.linkedin.com")
        self.assertEqual(get_platform("linkedin").api_base, linkedin.API_BASE)
        req = UploadRequest("linkedin", self.video, self.size, hashlib.sha256(self.data()).hexdigest(),
                            build(privacy="public").to_dict())
        with self.assertRaises(NetworkAttempted) as e:
            linkedin.LinkedInUploader(sleep=lambda s: None).upload(
                req, creds.Credentials("linkedin", {TOKEN_VAR: TOKEN, AUTHOR_VAR: AUTHOR}))
        self.assertIn("api.linkedin.com", str(e.exception))

    def test_the_endpoint_must_be_https_or_loopback(self):
        req = UploadRequest("linkedin", self.video, self.size, "0" * 64, build(privacy="public").to_dict())
        for url in ("http://api.linkedin.com", "ftp://127.0.0.1"):
            up = linkedin.LinkedInUploader()
            up.api_base = url
            with self.assertRaises(PublishError) as e:
                up.upload(req, creds.Credentials("linkedin", {TOKEN_VAR: TOKEN, AUTHOR_VAR: AUTHOR}))
            self.assertEqual(e.exception.code, "INTERNAL_ERROR")

    def test_upload_urls_must_be_https_on_linkedin(self):
        up = linkedin.LinkedInUploader()
        for url in ("https://www.linkedin.com/dms-uploads/C5/uploadedVideo?sau=x", "https://linkedin.com/u"):
            up._check_upload_url(url)
        for url in ("http://www.linkedin.com/dms-uploads/x", "https://www.linkedin.com.evil.example/x",
                    "https://evil-linkedin.com/x", "https://127.0.0.1/x"):
            with self.assertRaises(PublishError, msg=url):
                up._check_upload_url(url)


class LinkedInUploadTests(LinkedInTestCase):
    def test_linkedin_multipart_upload_sends_exactly_the_file_then_one_post(self):
        m = self.mock()
        rc, doc, text = self.confirmed("--title", "Launch day")
        self.assertEqual(rc, 0, doc)
        r = doc["result"]
        self.assertEqual(m.kinds(), ["init", "put", "put", "put", "finalize", "status", "post"])
        self.assertEqual(m.part_indexes(), [0, 1, 2])
        self.assertEqual(m.inits[0], {"initializeUploadRequest": {"owner": AUTHOR, "fileSizeBytes": self.size,
                                                                  "uploadCaptions": False, "uploadThumbnail": False}})
        fin = m.finalized[0]["finalizeUploadRequest"]
        self.assertEqual(fin["uploadedPartIds"], [f"/ambry-video/signedId/C5505AQH0000mock-{n}-{n + 1}.bin"
                                                  for n in range(3)])     # the ETags, unquoted, in order
        self.assertEqual(len(m.posts), 1)
        self.assertEqual(m.posts[0]["data"], self.data())
        self.assertEqual(m.posts[0]["body"], {
            "author": AUTHOR, "commentary": "Launch day", "visibility": "PUBLIC",
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "content": {"media": {"id": r["video"]}}, "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False})
        self.assertEqual((r["video_id"], r["post_id"], r["privacy"], r["visibility"], r["url"]),
                         (m.posts[0]["id"], m.posts[0]["id"], "public", "PUBLIC", None))
        for req in m.requests:
            if req["kind"] == "put":                                 # the signed URL needs no token: none is sent
                self.assertNotIn("authorization", req["headers"])
                self.assertEqual(req["headers"]["content-type"], "application/octet-stream")
            else:
                self.assertEqual(req["headers"]["authorization"], "Bearer " + TOKEN)
                self.assertEqual(req["headers"]["linkedin-version"], linkedin.LINKEDIN_VERSION)
                self.assertEqual(req["headers"]["x-restli-protocol-version"], "2.0.0")
        self.assertEqual((r["transfer"]["bytes_sent"], r["transfer"]["retries"]), (self.size, 0))
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(AUTHOR, text)                               # a credential value: redacted too
        for url in m.upload_urls:
            self.assertNotIn(url, text)

    def test_the_post_urn_comes_from_x_restli_id(self):
        up = linkedin.LinkedInUploader()
        for header, want in (("urn:li:share:6844785523593134080", "urn:li:share:6844785523593134080"),
                             ("urn%3Ali%3AugcPost%3A68447855235931240", "urn:li:ugcPost:68447855235931240")):
            with mock.patch.object(up, "_send", return_value=linkedin._Response(201, {"x-restli-id": header}, b"")):
                self.assertEqual(up._post({}, {}, "urn:li:video:x"), want)
        for header in ("", "urn:li:video:1", "6844785523593134080"):
            with mock.patch.object(up, "_send", return_value=linkedin._Response(201, {"x-restli-id": header}, b"")):
                with self.assertRaises(PublishError):
                    up._post({}, {}, "urn:li:video:x")

    def test_an_organization_author_and_the_commentary_without_a_title(self):
        org = "urn:li:organization:2414183"
        os.environ[AUTHOR_VAR] = org
        m = self.mock(author=org)
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual((m.posts[0]["body"]["author"], m.posts[0]["body"]["commentary"]), (org, ""))

    def test_linkedin_resume_after_an_interrupted_part(self):
        # The second part's answer is lost after LinkedIn kept it: it is PUT again to the same URL, and finalize
        # carries the newer ETag.
        m = self.mock()
        m.faults["put"] = [None, ("lost",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.part_indexes(), [0, 1, 1, 2])
        self.assertEqual(m.posts[0]["data"], self.data())
        self.assertEqual(m.finalized[0]["finalizeUploadRequest"]["uploadedPartIds"][1],
                         "/ambry-video/signedId/C5505AQH0000mock-1-3.bin")
        t = doc["result"]["transfer"]
        self.assertEqual((t["retries"], t["resumed"]), (1, 1))
        self.assertEqual(self.waits, [1.0])
        self.assertEqual(len(m.inits), 1)

    def test_a_dropped_part_is_sent_again(self):
        m = self.mock()
        m.faults["put"] = [("drop",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.part_indexes(), [0, 0, 1, 2])
        self.assertEqual(m.posts[0]["data"], self.data())

    def test_a_timeout_is_an_interruption(self):
        m = self.mock()
        m.faults["put"] = [None, ("stall", 1.5)]
        with mock.patch.object(linkedin.LinkedInUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.posts[0]["data"], self.data())

    def test_5xx_and_429_are_retried_with_backoff_and_retry_after(self):
        m = self.mock(processing=["PROCESSING", "AVAILABLE"])
        m.faults["init"] = [("status", 503, None)]
        m.faults["put"] = [("status", 429, "Too Many Requests", {"Retry-After": "40"})]
        m.faults["finalize"] = [("status", 500, None, {"Retry-After": "7"})]
        m.faults["status"] = [("status", 502, None)]
        m.faults["post"] = [("status", 429, None)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(len(m.posts), 1)
        # init 1.0; put: Retry-After 40; finalize: Retry-After 7; status 1.0; the poll interval 5.0; post 1.0
        self.assertEqual(self.waits, [1.0, 40.0, 7.0, 1.0, 5.0, 1.0])

    def test_retries_run_out_as_a_typed_retryable_error_and_nothing_is_posted(self):
        m = self.mock()
        m.faults["put"] = [("status", 503, None)] * 5
        with mock.patch.object(linkedin.LinkedInUploader, "max_retries", 3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertIn("nothing was posted", doc["error"]["message"])
        self.assertEqual(m.posts, [])

    def test_initialize_and_finalize_without_an_answer_are_not_retried(self):
        for kind, kinds in (("init", ["init"]), ("finalize", ["init", "put", "put", "put", "finalize"])):
            m = self.mock()
            m.faults[kind] = [("stall", 1.0)]
            with mock.patch.object(linkedin.LinkedInUploader, "timeout_s", 0.3):
                rc, doc, _ = self.confirmed()
            self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"), kind)
            self.assertEqual(m.kinds(), kinds)

    def test_a_part_answered_without_an_etag_stops_before_finalize(self):
        m = self.mock()
        m.faults["put"] = [("noetag",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertNotIn("finalize", m.kinds())

    def test_upload_instructions_must_cover_the_file_in_order(self):
        self.assertEqual(linkedin.parts_of([{"firstByte": 0, "lastByte": 3, "uploadUrl": "u0"},
                                            {"firstByte": 4, "lastByte": 9, "uploadUrl": "u1"}], 10),
                         [(0, 3, "u0"), (4, 9, "u1")])
        for bad, size in (([], 10), (None, 10), ([{"firstByte": 0, "lastByte": 8, "uploadUrl": "u"}], 10),
                          ([{"firstByte": 0, "lastByte": 3, "uploadUrl": "u"},
                            {"firstByte": 5, "lastByte": 9, "uploadUrl": "u"}], 10),
                          ([{"firstByte": 0, "lastByte": 9}], 10), (["u"], 10)):
            with self.assertRaises(ValueError, msg=bad):
                linkedin.parts_of(bad, size)

    def test_an_upload_url_off_linkedin_is_refused_before_any_byte(self):
        m = self.mock()
        real = linkedin.LinkedInUploader._check_upload_url

        def strict(self, url):                        # as if the API base were LinkedIn's: the mock's URL is not https
            with mock.patch.object(self, "api_base", "https://api.linkedin.com"):
                return real(self, url)
        with mock.patch.object(linkedin.LinkedInUploader, "_check_upload_url", strict):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertEqual(m.kinds(), ["init"])

    def test_a_file_edited_during_the_upload_is_never_completed(self):
        m = self.mock()

        def edit():
            with open(self.video, "r+b") as fh:
                fh.seek(self.size - 1)
                fh.write(b"\xff" if self.data()[-1:] != b"\xff" else b"\x00")
        m.faults["put"] = [("call", edit)]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
        self.assertEqual(m.part_indexes(), [0, 1])
        self.assertEqual(m.posts, [])

    def test_processing_is_polled_until_available(self):
        m = self.mock(processing=["PROCESSING", "PROCESSING", "AVAILABLE"])
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.kinds()[-5:], ["finalize", "status", "status", "status", "post"])
        self.assertEqual(self.waits, [5.0, 5.0])
        status = [r for r in m.requests if r["kind"] == "status"][0]
        self.assertEqual(status["path"], "/rest/videos/urn%3Ali%3Avideo%3A" + doc["result"]["video"].split(":")[-1])

    def test_failed_processing_is_rejected_and_nothing_is_posted(self):
        m = self.mock(processing=["PROCESSING", "PROCESSING_FAILED"], failure_reason="Unsupported video codec")
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertIn("Unsupported video codec", doc["error"]["message"])
        self.assertIn("ffmpeg-skill export --preset linkedin", doc["error"]["message"])
        self.assertEqual(m.posts, [])

    def test_still_processing_after_the_polls_is_interrupted_and_nothing_is_posted(self):
        m = self.mock(processing=["PROCESSING"])
        with mock.patch.object(linkedin.LinkedInUploader, "status_polls", 3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertEqual(m.kinds().count("status"), 3)
        self.assertEqual(m.posts, [])

    def test_linkedin_post_is_never_sent_twice(self):
        # The post's answer is lost (LinkedIn made it) or LinkedIn answers 5xx: stop and say to check the page.
        for fault in (("lost",), ("status", 503, None)):
            m = self.mock()
            m.faults["post"] = [fault]
            rc, doc, _ = self.confirmed()
            self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", False))
            self.assertIn("Check the author's page", doc["error"]["message"])
            self.assertEqual(m.kinds().count("post"), 1)

    def test_a_refused_access_token(self):
        m = self.mock()
        os.environ[TOKEN_VAR] = "AQVexpired-token"
        rc, doc, text = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"))
        self.assertIn("publish auth login --platform linkedin", doc["error"]["message"])
        self.assertEqual(m.kinds(), ["init"])
        self.assertNotIn("AQVexpired-token", text)

    def test_a_missing_permission_is_auth_failed(self):
        m = self.mock()
        m.faults["post"] = [("status", 403, "Insufficient permissions to perform the requested action.")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"))
        self.assertEqual(m.posts, [])

    def test_a_refused_post_is_rejected_with_linkedins_message(self):
        m = self.mock()
        m.faults["post"] = [("status", 422, "Review field values for logical consistency.")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertIn("logical consistency", doc["error"]["message"])
        self.assertEqual(doc["error"]["details"]["status"], 422)

    def test_an_author_that_is_not_a_urn_is_refused_before_any_request(self):
        m = self.mock()
        os.environ[AUTHOR_VAR] = "Jane Smith"
        rc, doc, text = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertIn("PUBLISH_LINKEDIN_AUTHOR", doc["error"]["message"])
        self.assertEqual(m.requests, [])


class LinkedInMetadataTests(LinkedInTestCase):
    def test_the_received_post_reproduces_the_token(self):
        m = self.mock()
        title = "新作 Launch #film @team (beta) [1] *new* _x_ ~y~ {a|b} <c> \\ 🎬"
        flags = ("--title", title, "--privacy", "public")
        rc, dry = run_cli("upload", self.video, "--platform", "linkedin", *flags, "--json")
        rc, out, _ = run_cli_raw("upload", self.video, "--platform", "linkedin", *flags, "--confirm",
                                 dry["confirm_token"], "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        got = m.posts[0]
        sent = got["body"]["commentary"]
        self.assertEqual(sent, "新作 Launch \\#film \\@team \\(beta\\) \\[1\\] \\*new\\* \\_x\\_ \\~y\\~ \\{a\\|b\\} "
                               "\\<c\\> \\\\ 🎬")
        self.assertEqual(unescape(sent), title)
        rebuilt = Metadata(title=title, privacy="public")
        self.assertEqual(confirm_token("linkedin", hashlib.sha256(got["data"]).hexdigest(), len(got["data"]), rebuilt),
                         dry["confirm_token"])

    def test_little_escape(self):
        self.assertEqual(linkedin.little_escape("plain text, 日本語."), "plain text, 日本語.")
        for ch in "|{}@[]()<>#\\*_~":
            self.assertEqual(linkedin.little_escape(ch), "\\" + ch)

    def test_private_and_unlisted_and_unsent_fields_are_refused_before_a_token(self):
        m = self.mock()
        srt = self.file("en.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        for flags, check in (((), "privacy"), (("--privacy", "private"), "privacy"),
                             (("--privacy", "unlisted"), "privacy"),
                             (("--description", "more") + PUBLIC, "description (not sent to LinkedIn)"),
                             (("--tag", "film") + PUBLIC, "tags (not sent to LinkedIn)"),
                             (("--thumbnail", self.file("t.png", PNG)) + PUBLIC, "thumbnail (not sent to LinkedIn)"),
                             (("--caption", srt, "--caption-language", "en") + PUBLIC,
                              "captions (not sent to LinkedIn)")):
            with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
                rc, doc = run_cli("upload", self.video, "--platform", "linkedin", *flags, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"), flags)
            self.assertNotIn("confirm_token", json.dumps(doc))
            self.assertEqual([r["check"] for r in doc["error"]["details"]["failed"]], [check])
        self.assertEqual(m.requests, [])

    def test_a_file_linkedin_cannot_take_is_refused_before_a_token(self):
        m = self.mock()
        for name in ("final.mov", "final.webm"):
            path = self.file(name, os.urandom(100 * 1024))
            with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
                rc, doc = run_cli("upload", path, "--platform", "linkedin", *PUBLIC, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"), name)
        self.assertEqual(m.requests, [])

    def test_post_body_is_the_bound_metadata_only(self):
        body = linkedin.post_body(Metadata(privacy="public").to_dict(), AUTHOR, "urn:li:video:7")
        self.assertEqual(body["commentary"], "")
        self.assertEqual(sorted(body), ["author", "commentary", "content", "distribution", "isReshareDisabledByAuthor",
                                        "lifecycleState", "visibility"])
        self.assertEqual(body["content"], {"media": {"id": "urn:li:video:7"}})
        for md in (Metadata(), Metadata(privacy="unlisted"), Metadata(privacy="public", description="x"),
                   Metadata(privacy="public", tags=["x"])):
            with self.assertRaises(PublishError):
                linkedin.post_body(md.to_dict(), AUTHOR, "urn:li:video:7")


class LinkedInGateAndRedactionTests(LinkedInTestCase):
    def test_every_refusal_happens_before_any_request_or_credential(self):
        m = self.mock()
        token = upload(self.video, "linkedin", metadata=build(privacy="public"))["confirm_token"]
        bad = [("upload", self.video, "--platform", "linkedin", *PUBLIC, "--confirm", token + "0"),
               ("upload", self.video, "--platform", "linkedin", *PUBLIC, "--confirm", token, "--title", "edited"),
               ("upload", self.video, "--platform", "linkedin", "--confirm", token),
               ("upload", self.video, "--platform", "linkedin", *PUBLIC)]
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            for argv in bad:
                rc, out, _ = run_cli_raw(*argv, "--json")
                doc = json.loads(out)
                self.assertTrue(doc.get("dry_run") or rc == 2, (argv, doc))
        self.assertEqual(m.requests, [])

    def test_both_values_are_needed(self):
        m = self.mock()
        os.environ.pop(AUTHOR_VAR)
        token = upload(self.video, "linkedin", metadata=build(privacy="public"))["confirm_token"]
        rc, doc = run_cli("upload", self.video, "--platform", "linkedin", *PUBLIC, "--confirm", token, "--json")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["missing"]),
                         (3, "CREDENTIALS_MISSING", [AUTHOR_VAR]))
        self.assertEqual(m.requests, [])

    def test_the_values_from_the_keychain_upload_and_are_never_printed(self):
        os.environ.pop(TOKEN_VAR)
        os.environ.pop(AUTHOR_VAR)
        self.keychain_backend.items[("publish-skill", TOKEN_VAR)] = TOKEN
        self.keychain_backend.items[("publish-skill", AUTHOR_VAR)] = AUTHOR
        m = self.mock()
        rc, doc, text = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.requests[0]["headers"]["authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(AUTHOR, text)

    def test_receipts_and_errors_never_carry_the_token_or_an_upload_url(self):
        scenarios = [{}, {"put": [("lost",)]}, {"post": [("status", 403, "Forbidden")]},
                     {"put": [("status", 503, None)] * 30}, {"post": [("lost",)]}, {"put": [("noetag",)]}]
        for faults in scenarios:
            for human in (False, True):
                m = self.mock()
                m.faults.update({k: list(v) for k, v in faults.items()})
                with mock.patch.object(linkedin.LinkedInUploader, "max_retries", 2):
                    rc, _, text = self.confirmed("--title", TOKEN[:20], human=human)
                self.assertNotIn(TOKEN, text)
                for url in m.upload_urls:
                    self.assertNotIn(url, text)
                    self.assertNotIn("secret", text)

    def test_human_receipt(self):
        self.mock()
        rc, _, text = self.confirmed("--title", "Launch", human=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("to LinkedIn: video urn:li:share:7", text)
        self.assertIn("(privacy public)", text)
        self.assertNotIn("None", text)


class LinkedInPlanTests(LinkedInTestCase):
    def test_metadata_rows(self):
        rows = {r["check"]: r for r in metadata_checks("linkedin", build(title="x" * 5000, privacy="public"))}
        # the Posts API states no commentary maximum: the row passes with no limit, citing where it was looked for
        self.assertEqual((rows["title length"]["status"], rows["title length"]["limit"],
                          rows["title length"]["limit_key"]), ("PASS", None, "title.max_chars"))
        self.assertNotIn("title characters", rows)                   # every reserved character is escaped
        self.assertEqual(rows["privacy"]["status"], "PASS")
        rows = {r["check"]: r for r in metadata_checks("linkedin", build(title="hi"))}
        self.assertEqual((rows["privacy"]["status"], rows["privacy"]["fix"]), ("FAIL", "--privacy one of public"))

    def test_file_and_duration_rows_check_both_ends(self):
        rows = {r["check"]: r for r in file_checks("linkedin", "/x/a.mp4", 75 * 1024)}
        self.assertEqual((rows["file size"]["status"], rows["file size (minimum)"]["status"]), ("PASS", "PASS"))
        rows = {r["check"]: r for r in file_checks("linkedin", "/x/a.mp4", 75 * 1024 - 1)}
        self.assertEqual(rows["file size (minimum)"]["status"], "FAIL")
        rows = {r["check"]: r for r in file_checks("linkedin", "/x/a.mp4", 500 * 1000 ** 2 + 1)}
        self.assertEqual(rows["file size"]["status"], "FAIL")
        rows = {r["check"]: r for r in file_checks("linkedin", "/x/a.mov", 100 * 1024)}
        self.assertEqual(rows["file format"]["status"], "FAIL")
        rows = {r["check"]: r for r in duration_checks("linkedin", 2.5)}
        self.assertEqual((rows["duration"]["status"], rows["duration (minimum)"]["status"]), ("PASS", "FAIL"))
        rows = {r["check"]: r for r in duration_checks("linkedin", 1800.5)}
        self.assertEqual((rows["duration"]["status"], rows["duration (minimum)"]["status"]), ("FAIL", "PASS"))
        self.assertEqual([r["check"] for r in duration_checks("youtube", 1.0)], ["duration"])   # no minimum there
        self.assertEqual([r["check"] for r in file_checks("youtube", "/x/a.mp4", 1)], ["file size", "file format"])

    def test_plan_reads_ffmpeg_skills_linkedin_spec(self):
        self.assertEqual(get_platform("linkedin").ffmpeg_skill_platform, "linkedin")
        doc = plan(self.video, "linkedin", build(title="Launch", privacy="public"),
                   ffmpeg_skill_dir=os.path.join(self.tmp, "none"))
        self.assertEqual(doc["verdict"], "INCOMPLETE")
        self.assertEqual(doc["ffmpeg_skill"]["check_platform"], "linkedin")
        rows = {r["check"]: r for r in doc["checks"]}
        self.assertEqual((rows["file size"]["status"], rows["file size"]["limit"]), ("PASS", 500000000))
        self.assertEqual((rows["file size (minimum)"]["status"], rows["file size (minimum)"]["limit"]), ("PASS", 76800))
        self.assertEqual((rows["file format"]["status"], rows["file format"]["value"]), ("PASS", ".mp4"))
        self.assertFalse(doc["network_used"])

    def test_plan_fails_a_tiny_file_offline(self):
        tiny = self.file("tiny.mp4", b"\x00" * 1000)
        rc, doc = run_cli("plan", tiny, "--platform", "linkedin", "--title", "t", *PUBLIC, "--json")
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))


class LinkedInLimitsTableTests(OfflineTestCase):
    def test_linkedin_values_as_researched(self):
        # Read from the pages cited in limits.py on 2026-10-31; change only together with the quote and the date.
        want = {"video.min_bytes": 76800, "video.max_bytes": 500000000, "video.min_duration_s": 3,
                "video.max_duration_s": 1800, "video.file_formats": [".mp4"], "privacy.values": ["public"],
                "token.lifetime_s": 60 * 86400}
        for key, v in want.items():
            self.assertEqual(L.value("linkedin", key), v, key)
        for key in ("title.max_chars", "title.forbidden_chars", "rate_limit.requests", "quota.daily_posts"):
            self.assertEqual(L.limits_of("linkedin")[key]["status"], "unknown", key)
        for key in ("video.codecs", "video.resolution", "video.aspect", "video.fps"):
            self.assertEqual(L.limits_of("linkedin")[key]["status"], "delegated", key)
        self.assertEqual((L.value("linkedin", "video.min_bytes"), L.value("linkedin", "video.max_bytes")),
                         (linkedin.MIN_BYTES, linkedin.MAX_BYTES))
        self.assertEqual(L.value("linkedin", "video.file_formats"), list(linkedin.FILE_FORMATS))
        for e in L.limits_of("linkedin").values():
            for url in [e.get("source")] + list(e.get("searched") or []):
                if url:
                    self.assertTrue(url.startswith("https://learn.microsoft.com/en-us/linkedin/"), url)

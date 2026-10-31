"""The YouTube uploader (roadmap step 4, ADR-004), entirely against tests/mock_youtube.py: an in-process mock of the
OAuth token endpoint and the resumable upload protocol on 127.0.0.1. Chunked upload, resume after an interrupted
chunk, timeouts, backoff on 5xx and 429, quota and upload-limit errors, expired sessions, the thumbnail and captions,
and that exactly the metadata the confirmation token bound is what arrives. No test contacts Google: the socket guard
lets a test reach only the loopback port of the mock it started, and the guard tests below assert that."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import unittest
from unittest import mock

from helpers import LOOPBACK_PORTS, NetworkAttempted, OfflineTestCase, env, run_cli, run_cli_raw, youtube_mock
from mock_youtube import SCOPE_UPLOAD

from publish_skill import credentials as creds
from publish_skill.errors import PublishError
from publish_skill.metadata import Metadata, build
from publish_skill.platforms import UploadRequest, get_platform, youtube
from publish_skill.upload import confirm_token, upload

SECRETS = {"PUBLISH_YOUTUBE_CLIENT_ID": "cid-7f3a91e2.apps.example",
           "PUBLISH_YOUTUBE_CLIENT_SECRET": "csecret-Zq81LmP0sW",
           "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//refresh-XyZ0987654321"}
UNIT = 256 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
SRT = "1\n00:00:00,000 --> 00:00:01,000\ncaf\xe9\n".encode("utf-8")


class UploaderTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.video = os.path.join(self.tmp, "final.mp4")         # 2.5 chunks of 256 KiB
        with open(self.video, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2 * UNIT + UNIT // 2 - 12))
        self.size = os.path.getsize(self.video)
        p = mock.patch.dict(os.environ, SECRETS)
        p.start()
        self.addCleanup(p.stop)

    def file(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def mock(self, **kw):
        self.m = youtube_mock(self, SECRETS, **kw)
        return self.m

    def confirmed(self, *flags):
        """Dry run, then the confirmed upload with the same flags; (exit code, document, stdout + stderr)."""
        rc, dry = run_cli("upload", self.video, "--platform", "youtube", *flags, "--json")
        self.assertEqual(rc, 0, dry)
        rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", *flags, "--confirm", dry["confirm_token"],
                                   "--json")
        return rc, json.loads(out), out + err

    def assert_nothing_secret(self, text):
        for s in list(SECRETS.values()) + [self.m.access_token] + self.m.upload_ids:
            self.assertNotIn(s, text)
        self.assertNotIn("upload_id=", text)

    def data(self):
        with open(self.video, "rb") as fh:
            return fh.read()


class LoopbackOnlyTests(UploaderTestCase):
    def test_the_guard_allows_only_the_mock_this_test_started(self):
        m = self.mock()
        self.assertEqual(LOOPBACK_PORTS, {m.port})
        s = socket.create_connection(("127.0.0.1", m.port))           # the mock: allowed
        s.close()
        for address in (("oauth2.googleapis.com", 443), ("www.googleapis.com", 443), ("127.0.0.1", m.port + 1)):
            with self.assertRaises(NetworkAttempted):
                socket.create_connection(address)
        with self.assertRaises(NetworkAttempted):
            socket.getaddrinfo("localhost", m.port)                    # only the literal loopback address

    def test_the_real_endpoints_are_googles_and_unreachable_from_a_test(self):
        self.assertEqual((youtube.TOKEN_URL, youtube.API_BASE), ("https://oauth2.googleapis.com/token",
                                                                 "https://www.googleapis.com"))
        self.assertEqual(get_platform("youtube").token_url, youtube.TOKEN_URL)
        req = UploadRequest("youtube", self.video, self.size, hashlib.sha256(self.data()).hexdigest(), build().to_dict())
        with self.assertRaises(NetworkAttempted) as e:                # no mock: the first request is stopped
            youtube.YouTubeUploader(sleep=lambda s: None).upload(req, creds.Credentials("youtube", SECRETS))
        self.assertIn("oauth2.googleapis.com", str(e.exception))

    def test_every_request_of_an_upload_went_to_the_mock(self):
        m = self.mock()
        rc, doc, _ = self.confirmed("--thumbnail", self.file("t.png", PNG), "--caption", self.file("en.srt", SRT),
                                    "--caption-language", "en")
        self.assertEqual(rc, 0, doc)
        self.assertNotIn("unknown", m.kinds())
        self.assertEqual(LOOPBACK_PORTS, {m.port})

    def test_endpoints_must_be_https_or_loopback(self):
        req = UploadRequest("youtube", self.video, self.size, "0" * 64, build().to_dict())
        for attr, url in (("token_url", "http://oauth2.example.com/token"), ("api_base", "ftp://127.0.0.1")):
            up = youtube.YouTubeUploader()
            setattr(up, attr, url)
            with self.assertRaises(PublishError) as e:
                up.upload(req, creds.Credentials("youtube", SECRETS))
            self.assertEqual(e.exception.code, "INTERNAL_ERROR")


class ResumableProtocolTests(UploaderTestCase):
    def test_chunked_upload_sends_exactly_the_file(self):
        m = self.mock()
        rc, doc, text = self.confirmed("--title", "Launch")
        self.assertEqual(rc, 0, doc)
        vid = doc["result"]["video_id"]
        self.assertEqual(m.videos[vid]["data"], self.data())
        self.assertEqual(m.ranges(), [f"bytes 0-{UNIT - 1}/{self.size}", f"bytes {UNIT}-{2 * UNIT - 1}/{self.size}",
                                      f"bytes {2 * UNIT}-{self.size - 1}/{self.size}"])
        self.assertEqual(m.kinds(), ["token", "init:videos", "put", "put", "put"])
        init = m.requests[1]
        self.assertEqual(init["query"], {"part": "snippet,status", "uploadType": "resumable"})
        self.assertEqual(init["headers"]["x-upload-content-length"], str(self.size))
        self.assertEqual(init["headers"]["x-upload-content-type"], "application/octet-stream")
        self.assertEqual(m.videos[vid]["mime"], "application/octet-stream")
        r = doc["result"]
        self.assertEqual((r["url"], r["privacy"]), ("https://www.youtube.com/watch?v=" + vid, "private"))
        self.assertEqual((r["transfer"]["bytes_sent"], r["transfer"]["retries"], r["transfer"]["resumed"]), (self.size, 0, 0))
        self.assert_nothing_secret(text)

    def test_resume_after_an_interrupted_chunk(self):
        m = self.mock()
        m.faults["put"] = [None, ("interrupt", 100000)]        # the second chunk: 100000 bytes arrive, then the link drops
        rc, doc, text = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        resume_at = UNIT + 100000
        self.assertEqual(m.ranges(), [f"bytes 0-{UNIT - 1}/{self.size}", f"bytes {UNIT}-{2 * UNIT - 1}/{self.size}",
                                      f"bytes */{self.size}",                                  # how far did it get?
                                      f"bytes {resume_at}-{resume_at + UNIT - 1}/{self.size}",  # from the next byte
                                      f"bytes {resume_at + UNIT}-{self.size - 1}/{self.size}"])
        t = doc["result"]["transfer"]
        self.assertEqual((t["retries"], t["resumed"], t["session_restarts"]), (1, 1, 0))
        self.assertEqual(self.waits, [1.0])                     # backoff: 0.5 (the test's random) * 2**1
        self.assert_nothing_secret(text)

    def test_resume_when_nothing_of_the_chunk_arrived(self):
        m = self.mock()
        m.faults["put"] = [("interrupt", 0)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        self.assertEqual(m.ranges()[:3], [f"bytes 0-{UNIT - 1}/{self.size}", f"bytes */{self.size}",
                                          f"bytes 0-{UNIT - 1}/{self.size}"])

    def test_a_timeout_is_an_interruption(self):
        m = self.mock()
        m.faults["put"] = [None, ("stall", 1.5)]
        with mock.patch.object(youtube.YouTubeUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        self.assertIn(f"bytes */{self.size}", m.ranges())
        self.assertEqual(doc["result"]["transfer"]["resumed"], 1)

    def test_5xx_and_429_are_retried_with_exponential_backoff_and_retry_after(self):
        m = self.mock()
        m.faults["token"] = [("status", 500, "backendError")]
        m.faults["init:videos"] = [("status", 503, "backendError"), ("status", 429, "rateLimitExceeded", {"Retry-After": "7"})]
        m.faults["put"] = [("status", 502, "")]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        # token: retry 1 -> 1.0; init: retry 1 -> 1.0, retry 2 -> max(2.0, Retry-After 7); put: retry 1 -> 1.0
        self.assertEqual(self.waits, [1.0, 1.0, 7.0, 1.0])
        self.assertEqual(m.kinds(), ["token", "token", "init:videos", "init:videos", "init:videos", "put", "status",
                                     "put", "put", "put"])

    def test_backoff_is_capped(self):
        up = youtube.YouTubeUploader(sleep=lambda s: None, rand=lambda: 1.0)
        up._stats = {"retries": 0, "waited_s": 0.0}
        waits = []
        up._sleep_fn = waits.append
        for attempt in (1, 2, 3, 6, 7, 10):
            up._backoff(attempt, None)
        self.assertEqual(waits, [2.0, 4.0, 8.0, 64.0, 64.0, 64.0])
        resp = youtube._Response(429, {"retry-after": "100000"}, b"")
        up._backoff(1, resp)
        self.assertEqual(waits[-1], 300.0)                     # Retry-After is honoured up to 5 minutes

    def test_retries_run_out_as_a_typed_retryable_error_and_nothing_is_published(self):
        m = self.mock()
        m.faults["put"] = [("status", 503, "backendError")] * 50
        m.faults["status"] = [None] * 50
        with mock.patch.object(youtube.YouTubeUploader, "max_retries", 3):
            rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertFalse(err["details"]["uploaded"])
        self.assertEqual(err["details"]["last"]["status"], 503)
        self.assertEqual(m.videos, {})
        self.assertEqual(len(self.waits), 3)
        self.assert_nothing_secret(text)

    def test_a_connection_that_never_answers_runs_out_too(self):
        m = self.mock()
        m.faults["token"] = [("stall", 0.6)] * 5
        with mock.patch.object(youtube.YouTubeUploader, "timeout_s", 0.2), \
                mock.patch.object(youtube.YouTubeUploader, "max_retries", 2):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(doc["error"]["details"]["last"], {"status": None, "reasons": ["no answer"]})
        self.assertEqual(m.kinds(), ["token"] * 3)

    def test_an_expired_session_starts_again_from_the_beginning(self):
        m = self.mock()
        m.faults["put"] = [None, ("expire",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        self.assertEqual(m.kinds().count("init:videos"), 2)
        self.assertEqual(doc["result"]["transfer"]["session_restarts"], 1)
        self.assertEqual(len(m.videos), 1)

    def test_a_file_edited_during_the_upload_is_never_completed(self):
        m = self.mock()

        def edit():                                             # same size, one byte changed in the last chunk
            with open(self.video, "r+b") as fh:
                fh.seek(self.size - 1)
                old = fh.read(1)
                fh.seek(self.size - 1)
                fh.write(b"\x01" if old != b"\x01" else b"\x02")

        m.faults["put"] = [("call", edit)]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
        self.assertFalse(doc["error"]["details"]["uploaded"])
        self.assertEqual(m.videos, {})
        self.assertEqual(m.ranges(), [f"bytes 0-{UNIT - 1}/{self.size}", f"bytes {UNIT}-{2 * UNIT - 1}/{self.size}"])

    def test_a_server_that_keeps_none_of_a_chunk_is_retried_boundedly(self):
        m = self.mock()
        m.faults["put"] = [("interrupt", 0)] * 20
        with mock.patch.object(youtube.YouTubeUploader, "max_retries", 2):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(m.videos, {})

    def test_range_parsing_and_protocol_errors(self):
        up = youtube.YouTubeUploader()
        self.assertEqual(up._confirmed(youtube._Response(308, {}, b""), "x"), 0)
        self.assertEqual(up._confirmed(youtube._Response(308, {"range": "bytes=0-999"}, b""), "x"), 1000)
        with self.assertRaises(PublishError) as e:
            up._confirmed(youtube._Response(308, {"range": "bytes=5-9"}, b""), "x")
        self.assertEqual(e.exception.code, "UPLOAD_REJECTED")
        with self.assertRaises(PublishError) as e:                # more than was sent, or less than was confirmed
            up._advance("x", 10, b"abc", hashlib.sha256(), 14)
        self.assertEqual(e.exception.code, "UPLOAD_REJECTED")
        up.chunk_size = UNIT + 1
        with self.assertRaises(PublishError):
            up.upload(UploadRequest("youtube", self.video, self.size, "0" * 64, build().to_dict()),
                      creds.Credentials("youtube", SECRETS))

    def test_a_session_uri_on_another_host_is_refused(self):
        up = youtube.YouTubeUploader()
        for bad in ("", "https://evil.example/upload?upload_id=x", "http://www.googleapis.com/upload"):
            with self.assertRaises(PublishError) as e:
                up._check_session_uri(bad, "videos.insert")
            self.assertEqual(e.exception.code, "UPLOAD_REJECTED")
        up._check_session_uri("https://www.googleapis.com/upload/youtube/v3/videos?upload_id=x", "videos.insert")


class PlatformErrorTests(UploaderTestCase):
    def test_quota_exceeded_is_typed_and_not_retried(self):
        m = self.mock()
        m.faults["init:videos"] = [("status", 403, "quotaExceeded")]
        rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"]), (4, "QUOTA_EXCEEDED", True))
        self.assertEqual((err["details"]["status"], err["details"]["reasons"]), (403, ["quotaExceeded"]))
        self.assertFalse(err["details"]["uploaded"])
        self.assertEqual(m.kinds(), ["token", "init:videos"])     # no retry, no byte of the video sent
        self.assertEqual(self.waits, [])
        self.assert_nothing_secret(text)

    def test_quota_exceeded_mid_upload(self):
        m = self.mock()
        m.faults["put"] = [None, ("status", 403, "quotaExceeded")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "QUOTA_EXCEEDED"))
        self.assertEqual(m.videos, {})

    def test_the_channels_upload_limit(self):
        m = self.mock()
        m.faults["init:videos"] = [("status", 400, "uploadLimitExceeded")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_LIMIT_EXCEEDED", True))
        self.assertEqual(m.kinds(), ["token", "init:videos"])

    def test_a_refused_refresh_token(self):
        m = self.mock()
        with env(PUBLISH_YOUTUBE_REFRESH_TOKEN="1//revoked-token-0000"):
            rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"]), (3, "AUTH_FAILED", False))
        self.assertEqual(err["details"]["error"], "invalid_grant")
        self.assertEqual(m.kinds(), ["token"])
        self.assertNotIn("1//revoked-token-0000", text)
        self.assert_nothing_secret(text)

    def test_a_missing_scope_is_refused_before_any_byte(self):
        m = self.mock(scope=SCOPE_UPLOAD)                         # enough for the video, not for captions.insert
        rc, doc, _ = self.confirmed("--caption", self.file("en.srt", SRT), "--caption-language", "en")
        self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"))
        self.assertEqual(doc["error"]["details"]["step"], "captions.insert")
        self.assertEqual(m.kinds(), ["token"])

    def test_another_4xx_is_rejected_without_retry(self):
        m = self.mock()
        m.faults["init:videos"] = [("status", 400, "invalidTitle")]
        rc, doc, _ = self.confirmed("--title", "x")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_REJECTED", False))
        self.assertEqual(doc["error"]["details"]["reasons"], ["invalidTitle"])
        self.assertEqual(m.kinds(), ["token", "init:videos"])

    def test_a_failed_thumbnail_after_the_video_is_a_partial_upload(self):
        m = self.mock()
        m.faults["init:thumbnails"] = [("status", 403, "forbidden")]
        rc, doc, text = self.confirmed("--thumbnail", self.file("t.png", PNG))
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"]), (4, "UPLOAD_PARTIAL", False))
        self.assertTrue(err["details"]["uploaded"])
        self.assertEqual(list(m.videos), [err["details"]["video_id"]])
        self.assertEqual((err["details"]["failed_step"], err["details"]["cause"]["code"]), ("thumbnails.set", "UPLOAD_REJECTED"))
        self.assertIn("Do not run upload again", err["message"])
        self.assert_nothing_secret(text)


class BoundMetadataTests(UploaderTestCase):
    def test_privacy_defaults_to_private_and_nothing_unbound_is_sent(self):
        m = self.mock()
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["resource"], {"status": {"privacyStatus": "private"}})
        self.assertEqual(m.requests[1]["query"]["part"], "status")

    def test_the_received_upload_reproduces_the_token(self):
        # Everything that arrived at the mock -- the video bytes, the resource, the thumbnail and caption bytes and the
        # caption language -- rebuilt into Metadata gives the very token the human confirmed.
        m = self.mock()
        thumb, cap1, cap2 = self.file("t.png", PNG), self.file("en.srt", SRT), self.file("ja.vtt", b"WEBVTT\n\n")
        flags = ("--title", "Launch 動画", "--description", "line 1\nline 2", "--tag", "a b", "--tag", "c",
                 "--privacy", "unlisted", "--thumbnail", thumb, "--caption", cap1, "--caption-language", "en",
                 "--caption", cap2, "--caption-language", "ja")
        rc, dry = run_cli("upload", self.video, "--platform", "youtube", *flags, "--json")
        rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", *flags, "--confirm", dry["confirm_token"],
                                   "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        vid = doc["result"]["video_id"]
        got = m.videos[vid]
        res = got["resource"]
        self.assertEqual(res, {"snippet": {"title": "Launch 動画", "description": "line 1\nline 2", "tags": ["a b", "c"]},
                               "status": {"privacyStatus": "unlisted"}})
        self.assertEqual(res, doc["result"]["sent"]["video"])

        def facts(data, **extra):
            return dict({"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}, **extra)

        caps = [facts(c["data"], language=c["resource"]["snippet"]["language"]) for c in m.captions]
        rebuilt = Metadata(title=res["snippet"]["title"], description=res["snippet"]["description"],
                           tags=res["snippet"]["tags"], privacy=res["status"]["privacyStatus"],
                           thumbnail=facts(m.thumbnails[vid]["data"]), captions=caps)
        self.assertEqual(confirm_token("youtube", hashlib.sha256(got["data"]).hexdigest(), len(got["data"]), rebuilt),
                         dry["confirm_token"])
        self.assertEqual(doc["confirm_token"], dry["confirm_token"])
        self.assertEqual(m.thumbnails[vid]["mime"], "image/png")
        self.assertEqual([c["resource"] for c in m.captions],
                         [{"snippet": {"videoId": vid, "language": lang, "name": lang, "isDraft": False}} for lang in ("en", "ja")])
        self.assertEqual([c["query"]["part"] for c in m.captions], ["snippet", "snippet"])
        self.assertEqual(doc["result"]["thumbnail"], {"set": True, "type": "image/png"})
        self.assertEqual([c["language"] for c in doc["result"]["captions"]], ["en", "ja"])
        self.assertEqual(m.kinds(), ["token", "init:videos", "put", "put", "put", "init:thumbnails", "put",
                                     "init:captions", "put", "init:captions", "put"])

    def test_video_resource_is_the_bound_metadata_only(self):
        for md, want in ((Metadata(), {"status": {"privacyStatus": "private"}}),
                         (Metadata(title="", privacy="public"), {"snippet": {"title": ""}, "status": {"privacyStatus": "public"}}),
                         (Metadata(tags=["x"]), {"snippet": {"tags": ["x"]}, "status": {"privacyStatus": "private"}})):
            self.assertEqual(youtube.video_resource(md.to_dict()), want)
        with self.assertRaises(PublishError):
            youtube.video_resource(Metadata(privacy="everyone").to_dict())

    def test_a_privacy_the_platform_changed_is_reported(self):
        self.mock(report_privacy="private")
        rc, doc, _ = self.confirmed("--privacy", "public")
        self.assertEqual((rc, doc["result"]["privacy"]), (0, "private"))
        self.assertTrue(any("'public'" in w and "'private'" in w for w in doc["result"]["warnings"]))


class GateBeforeNetworkTests(UploaderTestCase):
    def test_every_refusal_happens_before_any_request(self):
        m = self.mock()
        token = upload(self.video, "youtube")["confirm_token"]
        bad = [("upload", self.video, "--platform", "myspace", "--confirm", token),
               ("upload", self.video, "--platform", "youtube", "--confirm", token + "0"),
               ("upload", self.video, "--platform", "youtube", "--confirm", token, "--title", "edited"),
               ("upload", self.video, "--platform", "youtube", "--confirm", token, "--title", "x" * 101),
               ("upload", self.video, "--platform", "youtube", "--confirm", token, "--caption", self.file("c.srt", SRT)),
               ("upload", self.video, "--platform", "youtube"),                         # a dry run
               ("upload", self.file("empty.mp4", b""), "--platform", "youtube", "--confirm",
                confirm_token("youtube", hashlib.sha256(b"").hexdigest(), 0))]
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            for argv in bad:
                rc, out, _ = run_cli_raw(*argv, "--json")
                doc = json.loads(out)
                self.assertTrue(doc.get("dry_run") or rc == 2, (argv, doc))
        self.assertEqual(m.requests, [])

    def test_human_receipt(self):
        self.mock()
        thumb = self.file("t.png", PNG)
        rc, dry = run_cli("upload", self.video, "--platform", "youtube", "--thumbnail", thumb, "--json")
        rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", "--thumbnail", thumb, "--confirm",
                                   dry["confirm_token"])
        self.assertEqual(rc, 0, err)
        self.assertIn("uploaded ", out)
        self.assertIn("(privacy private) https://www.youtube.com/watch?v=", out)
        self.assertIn("thumbnail set", out)
        self.assert_nothing_secret(out + err)

    def test_receipts_and_errors_never_carry_a_credential_token_or_session(self):
        # The no-value-in-output checks of ADR-003, repeated on receipts (JSON and human) and on every failure kind,
        # with each credential also planted where a careless uploader might echo it.
        scenarios = [{}, {"put": [None, ("interrupt", 1000)]}, {"init:videos": [("status", 403, "quotaExceeded")]},
                     {"init:thumbnails": [("status", 403, "forbidden")]}, {"put": [("status", 503, "")] * 30}]
        thumb = self.file("t.png", PNG)
        for faults in scenarios:
            m = self.mock()
            m.faults.update({k: list(v) for k, v in faults.items()})
            with mock.patch.object(youtube.YouTubeUploader, "max_retries", 2):
                for human in (False, True):
                    flags = ("--thumbnail", thumb, "--title", SECRETS["PUBLISH_YOUTUBE_CLIENT_SECRET"])
                    rc, dry = run_cli("upload", self.video, "--platform", "youtube", *flags, "--json")
                    argv = ["upload", self.video, "--platform", "youtube", *flags, "--confirm", dry["confirm_token"]]
                    rc, out, err = run_cli_raw(*(argv if human else argv + ["--json"]))
                    if not human:
                        json.loads(out)
                    self.assert_nothing_secret(out + err)

    def test_the_upload_function_redacts_its_receipt_itself(self):
        m = self.mock()
        with mock.patch.object(youtube.YouTubeUploader, "upload",
                               lambda self, req, c: {"video_id": "v", "echo": c.get("PUBLISH_YOUTUBE_REFRESH_TOKEN")}):
            receipt = upload(self.video, "youtube", confirm=upload(self.video, "youtube")["confirm_token"])
        self.assertEqual(receipt["result"]["echo"], creds.REDACTED)
        self.assertEqual(m.requests, [])


if __name__ == "__main__":
    unittest.main()

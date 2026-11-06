"""Roadmap step 8 (ADR-011): description chapters, a scheduled publish time and captions per language with localized
titles and descriptions. `plan` checks chapters against YouTube's rules (first at 0:00, at least three, ascending, each
at least 10 s), the scheduled time (RFC 3339 with an offset, privacy private) and the localizations (a default
language, BCP-47 keys); the YouTube uploader sends them in the videos.insert resource, and one caption track per
language. Uploads reach only tests/mock_youtube.py on 127.0.0.1."""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from unittest import mock

from helpers import OfflineTestCase, run_cli, run_cli_raw, youtube_mock

from publish_skill import limits as L
from publish_skill.errors import PublishError
from publish_skill.metadata import Metadata, build
from publish_skill.plan import chapters, metadata_checks, valid_publish_at
from publish_skill.platforms import youtube
from publish_skill.upload import confirm_token

SECRETS = {"PUBLISH_YOUTUBE_CLIENT_ID": "cid-5e1b77aa.apps.example",
           "PUBLISH_YOUTUBE_CLIENT_SECRET": "csecret-Pp20QaZx",
           "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//refresh-Step8Only000"}
UNIT = 256 * 1024
GOOD_CHAPTERS = "Talk by A.\n\n0:00 Intro\n1:30 Method\n12:05 Results\n1:02:00 Q&A\n"
SRT_EN = "1\n00:00:00,000 --> 00:00:01,000\nhello\n".encode("utf-8")
SRT_JA = "1\n00:00:00,000 --> 00:00:01,000\nこんにちは\n".encode("utf-8")


def rows_named(doc_or_rows, prefix):
    rows = doc_or_rows["checks"] if isinstance(doc_or_rows, dict) else doc_or_rows
    return [r for r in rows if r["check"].startswith(prefix)]


class Step8TestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, SECRETS)
        p.start()
        self.addCleanup(p.stop)

    def file(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def plan(self, *flags, platform="youtube"):
        return run_cli("plan", self.video, "--platform", platform, *flags, "--ffmpeg-skill",
                       os.path.join(self.tmp, "none"), "--json")


class ChapterTests(Step8TestCase):
    def test_chapters_starting_at_0_05_are_refused_with_the_rule(self):
        # Done when: a plan with chapters starting at 0:05 is refused with the rule
        desc = "0:05 Intro\n1:00 Method\n2:00 Results\n"
        rc, doc = self.plan("--title", "Talk", "--description", desc)
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))
        first = rows_named(doc, "chapters: first timestamp")[0]
        self.assertEqual((first["status"], first["value"], first["limit"]), ("FAIL", "0:05", "0:00"))
        self.assertIn("Make sure that the first timestamp you list starts with 00:00.", first["fix"])
        self.assertEqual(first["source"], "https://support.google.com/youtube/answer/9884579?hl=en")
        self.assertEqual(first["accessed"], "2026-11-06")
        # and upload refuses it before a token is printed
        rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--title", "Talk", "--description", desc, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"))
        self.assertNotIn("confirm_token", doc)
        self.assertIn("chapters: first timestamp", doc["error"]["message"])

    def test_good_chapters_pass(self):
        rows = rows_named(metadata_checks("youtube", build(title="T", description=GOOD_CHAPTERS), 4000.0), "chapters")
        self.assertEqual([r["check"] for r in rows], ["chapters: first timestamp", "chapters: count",
                                                      "chapters: ascending order", "chapters: length"])
        self.assertEqual({r["status"] for r in rows}, {"PASS"})
        self.assertEqual([c["seconds"] for c in chapters(GOOD_CHAPTERS)], [0, 90, 725, 3720])

    def test_fewer_than_three_short_and_unordered_chapters_fail(self):
        two = rows_named(metadata_checks("youtube", build(description="00:00 a\n0:30 b")), "chapters: count")[0]
        self.assertEqual((two["status"], two["value"], two["limit"]), ("FAIL", 2, 3))
        short = rows_named(metadata_checks("youtube", build(description="0:00 a\n0:09 b\n0:30 c")), "chapters: length")[0]
        self.assertEqual((short["status"], short["value"], short["limit"]), ("FAIL", 9, 10))
        self.assertIn("line 1", short["fix"])
        order = metadata_checks("youtube", build(description="0:00 a\n2:00 b\n1:00 c"))
        self.assertEqual(rows_named(order, "chapters: ascending order")[0]["status"], "FAIL")
        self.assertEqual(rows_named(order, "chapters: length"), [])

    def test_the_last_chapter_is_checked_against_the_measured_duration(self):
        md = build(description="0:00 a\n0:20 b\n0:40 c")
        self.assertEqual(rows_named(metadata_checks("youtube", md), "chapters: length")[0]["status"], "PASS")
        last = rows_named(metadata_checks("youtube", md, 45.0), "chapters: length")[0]
        self.assertEqual((last["status"], last["value"]), ("FAIL", 5.0))
        self.assertEqual(rows_named(metadata_checks("youtube", md, 50.0), "chapters: length")[0]["status"], "PASS")

    def test_chapter_lines_are_timestamps_at_the_start_of_a_line(self):
        self.assertEqual([c["seconds"] for c in chapters("- 0:00 a\n• 1:05 b\n[2:10] c\n(3:00) d")], [0, 65, 130, 180])
        self.assertEqual(chapters("see 0:30 for the demo\nratio 16:9\n10:61 no\n1:00:5 no\n12:345 no"), [])
        self.assertEqual(chapters(None), [])

    def test_no_chapter_lines_no_rows_and_no_rows_off_youtube(self):
        self.assertEqual(rows_named(metadata_checks("youtube", build(description="plain text")), "chapters"), [])
        self.assertEqual(rows_named(metadata_checks("vimeo", build(description="0:05 a")), "chapters"), [])

    def test_the_table_quotes_the_rules(self):
        for key, n in (("chapters.first_at_s", "00:00"), ("chapters.min_count", "three"), ("chapters.min_length_s", "10")):
            e = L.limits_of("youtube")[key]
            self.assertEqual((e["status"], e["accessed"]), ("documented", "2026-11-06"))
            self.assertIn(n, e["quote"])


class ScheduleTests(Step8TestCase):
    def test_rfc3339_with_an_offset_only(self):
        for ok in ("2026-12-01T09:00:00Z", "2026-12-01T09:00:00+09:00", "2026-12-01T09:00:00.250-05:30"):
            self.assertTrue(valid_publish_at(ok), ok)
        for bad in ("2026-12-01T09:00:00", "2026-12-01 09:00:00Z", "2026-13-01T09:00:00Z", "2026-02-30T09:00:00Z",
                    "2026-12-01T24:00:00Z", "2026-12-01T09:00:00+25:00", "tomorrow", "1764579600"):
            self.assertFalse(valid_publish_at(bad), bad)

    def test_plan_rows(self):
        rc, doc = self.plan("--title", "T", "--publish-at", "2026-12-01T09:00:00Z")
        self.assertEqual([r["status"] for r in rows_named(doc, "scheduled time")], ["PASS", "PASS"])
        rows = rows_named(metadata_checks("youtube", build(publish_at="2026-12-01 09:00", privacy="unlisted")),
                          "scheduled time")
        self.assertEqual([(r["check"], r["status"]) for r in rows], [("scheduled time format", "FAIL"),
                                                                     ("scheduled time privacy", "FAIL")])
        self.assertIn("--privacy private", rows[1]["fix"])

    def test_other_platforms_refuse_it_before_a_token(self):
        for platform in ("vimeo", "tiktok", "facebook"):
            privacy = "public" if platform == "facebook" else "private"
            rc, doc = run_cli("upload", self.video, "--platform", platform, "--privacy", privacy, "--publish-at",
                              "2026-12-01T09:00:00Z", "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"), platform)
            self.assertIn("publish_at (not sent to", doc["error"]["message"])

    def test_the_uploader_never_sends_a_schedule_with_another_privacy(self):
        with self.assertRaises(PublishError):
            youtube.video_resource(Metadata(privacy="public", publish_at="2026-12-01T09:00:00Z").to_dict())


class LocalizationTests(Step8TestCase):
    def test_file_shape(self):
        good = self.file("l10n.json", json.dumps({"ja": {"title": "発表", "description": "説明"},
                                                  "es-419": {"title": "Charla"}}, ensure_ascii=False))
        md = build(language="en", localizations_file=good)
        self.assertEqual(md.localizations, {"ja": {"title": "発表", "description": "説明"},
                                            "es-419": {"title": "Charla"}})
        for name, text in (("a.json", "[]"), ("b.json", "{}"), ("c.json", '{"ja": "x"}'), ("d.json", '{"ja": {"tags": "x"}}'),
                           ("e.json", '{"ja": {"title": 3}}'), ("f.json", "not json")):
            with self.assertRaises(PublishError) as e:
                build(localizations_file=self.file(name, text))
            self.assertEqual(e.exception.code, "INVALID_ARGUMENT", name)
        with self.assertRaises(PublishError) as e:
            build(localizations_file=self.file("g.json", b"\xff\xfe{}"))
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(PublishError) as e:
            build(localizations_file=os.path.join(self.tmp, "missing.json"))
        self.assertEqual(e.exception.code, "INPUT_NOT_FOUND")

    def test_plan_rows(self):
        l10n = self.file("l10n.json", json.dumps({"ja": {"title": "x" * 101}, "not a tag": {"title": "y"}}))
        rc, doc = self.plan("--title", "T", "--localizations", l10n)
        self.assertEqual(doc["verdict"], "FAIL")
        default = rows_named(doc, "default language")[0]
        self.assertEqual((default["status"], default["limit_key"]), ("FAIL", "localizations.default_language"))
        self.assertIn("defaultLanguageNotSet", default["fix"])
        self.assertEqual(rows_named(doc, "localization language (not a tag)")[0]["status"], "FAIL")
        self.assertEqual(rows_named(doc, "localization text (ja)")[0]["status"], "WARN")
        rows = metadata_checks("youtube", build(language="en", localizations_file=self.file("ok.json", '{"ja": {"title": "t"}}')))
        self.assertEqual({r["status"] for r in rows_named(rows, "default language") + rows_named(rows, "localization")},
                         {"PASS"})
        self.assertEqual(rows_named(metadata_checks("youtube", build(language="e n")), "default language")[0]["status"],
                         "FAIL")

    def test_other_platforms_refuse_them(self):
        rc, doc = self.plan("--title", "T", "--language", "en", platform="vimeo")
        row = rows_named(doc, "language (not sent to Vimeo)")[0]
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("drop --language", row["fix"])


class TokenTests(Step8TestCase):
    def test_metadata_without_the_new_fields_hashes_as_before(self):
        self.assertEqual(Metadata(title="T").canonical(),
                         '{"captions":[],"description":null,"privacy":"private","tags":[],"thumbnail":null,"title":"T"}')

    def test_each_new_field_changes_the_token(self):
        base = Metadata(title="T")
        tokens = {confirm_token("youtube", "0" * 64, 1, m) for m in (
            base, Metadata(title="T", language="en"), Metadata(title="T", publish_at="2026-12-01T09:00:00Z"),
            Metadata(title="T", language="en", localizations={"ja": {"title": "t"}}),
            Metadata(title="T", language="en", localizations={"ja": {"title": "u"}}))}
        self.assertEqual(len(tokens), 5)


class CaptionLanguageTests(Step8TestCase):
    def test_one_track_per_language(self):
        a, b = self.file("a.srt", SRT_EN), self.file("b.srt", SRT_EN + b"\n")
        rc, doc = self.plan("--title", "T", "--caption", a, "--caption-language", "en", "--caption", b,
                            "--caption-language", "en")
        row = rows_named(doc, "captions languages")[0]
        self.assertEqual((row["status"], row["value"]), ("FAIL", ["en", "en"]))


class MockUploadTests(Step8TestCase):
    def test_a_mock_upload_carries_two_caption_tracks_and_a_scheduled_time(self):
        # Done when: a mock upload carries two caption tracks and a scheduled time
        with open(self.video, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(UNIT + 1000))
        m = youtube_mock(self, SECRETS)
        en, ja = self.file("talk.en.srt", SRT_EN), self.file("talk.ja.srt", SRT_JA)
        l10n = self.file("l10n.json", json.dumps({"ja": {"title": "発表", "description": "説明"}},
                                                 ensure_ascii=False))
        flags = ["--title", "Talk", "--description", GOOD_CHAPTERS, "--language", "en", "--localizations", l10n,
                 "--publish-at", "2026-12-01T09:00:00Z", "--caption", en, "--caption-language", "en",
                 "--caption", ja, "--caption-language", "ja"]
        rc, dry = run_cli("upload", self.video, "--platform", "youtube", *flags, "--json")
        self.assertEqual((rc, dry["metadata"]["publish_at"], dry["metadata"]["language"]), (0, "2026-12-01T09:00:00Z", "en"))
        rc, out, err = run_cli_raw("upload", self.video, "--platform", "youtube", *flags, "--confirm",
                                   dry["confirm_token"], "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        vid = doc["result"]["video_id"]
        sent = m.videos[vid]["resource"]
        self.assertEqual(m.videos[vid]["query"]["part"], "localizations,snippet,status")
        self.assertEqual(sent["status"], {"privacyStatus": "private", "publishAt": "2026-12-01T09:00:00Z"})
        self.assertEqual(sent["snippet"]["defaultLanguage"], "en")
        self.assertEqual(sent["localizations"], {"ja": {"title": "発表", "description": "説明"}})
        self.assertEqual([(c["resource"]["snippet"]["language"], c["data"]) for c in m.captions],
                         [("en", SRT_EN), ("ja", SRT_JA)])
        r = doc["result"]
        self.assertEqual((r["publish_at"], r["localizations"], [c["language"] for c in r["captions"]]),
                         ("2026-12-01T09:00:00Z", ["ja"], ["en", "ja"]))
        self.assertEqual(r["warnings"], [])
        # the token rebuilt from what reached the mock is the one confirmed
        with open(self.video, "rb") as fh:
            data = fh.read()
        caps = [{"sha256": hashlib.sha256(c["data"]).hexdigest(), "size": len(c["data"]),
                 "language": c["resource"]["snippet"]["language"]} for c in m.captions]
        md = Metadata(title=sent["snippet"]["title"], description=sent["snippet"]["description"],
                      privacy=sent["status"]["privacyStatus"], captions=caps,
                      language=sent["snippet"]["defaultLanguage"], localizations=sent["localizations"],
                      publish_at=sent["status"]["publishAt"])
        self.assertEqual(confirm_token("youtube", hashlib.sha256(data).hexdigest(), len(data), md), dry["confirm_token"])
        for s in list(SECRETS.values()) + [m.access_token] + m.upload_ids:
            self.assertNotIn(s, out + err)
        rc, out, _ = run_cli_raw("upload", self.video, "--platform", "youtube", *flags, "--confirm", dry["confirm_token"])
        self.assertIn("scheduled to publish at 2026-12-01T09:00:00Z", out)

    def test_the_mock_refuses_what_youtube_documents_as_refused(self):
        m = youtube_mock(self, SECRETS)
        size = os.path.getsize(self.video)
        with open(self.video, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        from publish_skill import credentials as creds
        from publish_skill.platforms import UploadRequest
        md = Metadata(title="T", localizations={"ja": {"title": "t"}}).to_dict()        # no default language
        with self.assertRaises(PublishError) as e:
            youtube.YouTubeUploader().upload(UploadRequest("youtube", self.video, size, sha, md),
                                             creds.Credentials("youtube", SECRETS))
        self.assertEqual((e.exception.code, e.exception.details["reasons"]), ("UPLOAD_REJECTED", ["defaultLanguageNotSet"]))
        self.assertEqual(m.videos, {})


if __name__ == "__main__":
    unittest.main()

"""Roadmap step 9 (ADR-012): clips as a series. A manifest of Shorts / Reels cut from one source is uploaded in order,
each clip with its own title, its own confirmation token and a link back to the full video; a state file makes a re-run
resume at the first clip not uploaded, and no clip is ever sent twice. Uploads reach only the loopback mocks."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from helpers import OfflineTestCase, reels_mock, run_cli, run_cli_raw, youtube_mock

from publish_skill import series as S
from publish_skill.errors import PublishError

YT = {"PUBLISH_YOUTUBE_CLIENT_ID": "cid-series-91.apps.example",
      "PUBLISH_YOUTUBE_CLIENT_SECRET": "csecret-series-Lq7",
      "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//refresh-series-0042"}
IG_ID, IG_TOKEN = "17841400000000077", "EAAseries-ig-token-77"
FULL = "https://www.youtube.com/watch?v=FULLtalk01"


class SeriesTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, dict(YT, PUBLISH_INSTAGRAM_USER_ID=IG_ID, PUBLISH_INSTAGRAM_ACCESS_TOKEN=IG_TOKEN))
        p.start()
        self.addCleanup(p.stop)
        self.clips = []
        for i in range(1, 6):
            path = os.path.join(self.tmp, f"clip-{i:02d}.mp4")
            with open(path, "wb") as fh:
                fh.write(b"\x00\x00\x00\x18ftypmp42" + bytes([i]) * (3000 + i))
            self.clips.append(path)
        self.manifest = self.write_manifest({"full_video_url": FULL, "privacy": "public",
                                             "clips": [{"file": f"clip-{i:02d}.mp4", "title": f"Highlight {i}",
                                                        "description": f"Part {i} of the keynote."} for i in range(1, 6)]})
        # Instagram Reels sends the title (the caption) only: the same clips without descriptions
        self.reels_manifest = self.write_manifest({"full_video_url": FULL, "privacy": "public",
                                                   "clips": [{"file": f"clip-{i:02d}.mp4", "title": f"Highlight {i}"}
                                                             for i in range(1, 6)]}, "reels.json")

    def write_manifest(self, doc, name="clips.json"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def run_series(self, *extra, platform="youtube", manifest=None):
        rc, out, err = run_cli_raw("series", manifest or self.manifest, "--platform", platform, *extra, "--json")
        return rc, json.loads(out), out + err

    def tokens(self, platform="youtube", manifest=None):
        rc, doc, _ = self.run_series(platform=platform, manifest=manifest)
        self.assertEqual(rc, 0, doc)
        return [c["confirm_token"] for c in doc["clips"]]

    def confirm_all(self, tokens):
        return [a for t in tokens for a in ("--confirm", t)]

    def data(self, i):
        with open(self.clips[i - 1], "rb") as fh:
            return fh.read()


class SeriesTests(SeriesTestCase):
    def test_five_clips_with_a_failure_on_clip_3_resume_at_clip_3_and_upload_each_once(self):
        # Done when: a mock run of five clips with a failure on clip 3 resumes at clip 3 and uploads each once
        m = youtube_mock(self, YT)
        tokens = self.tokens()
        m.faults["init:videos"] = [None, None, ("status", 403, "quotaExceeded")]
        rc, doc, text = self.run_series(*self.confirm_all(tokens))
        self.assertEqual((rc, doc["error"]["code"]), (4, "QUOTA_EXCEEDED"))
        self.assertIn("series stopped at clip 3 of 5", doc["error"]["message"])
        self.assertEqual((doc["error"]["details"]["series"]["uploaded"], doc["error"]["details"]["uploaded_now"]), ([1, 2], [1, 2]))
        self.assertEqual(len(m.videos), 2)
        with open(S.default_state_path(self.manifest), encoding="utf-8") as fh:
            state = json.load(fh)
        self.assertEqual(sorted(state["clips"]), ["1", "2"])                    # clip 3 failed before anything was made
        # the same command again: clips 1 and 2 are skipped, the run resumes at clip 3
        rc, doc, text = self.run_series(*self.confirm_all(tokens))
        self.assertEqual(rc, 0, doc)
        self.assertEqual((doc["complete"], doc["uploaded_now"]), (True, [3, 4, 5]))
        self.assertEqual([c["outcome"] for c in doc["clips"]], ["skipped (already uploaded)"] * 2 + ["uploaded"] * 3)
        uploaded = sorted(v["data"] for v in m.videos.values())
        self.assertEqual(uploaded, sorted(self.data(i) for i in range(1, 6)))  # each clip exactly once
        self.assertEqual(len(m.videos), 5)
        # in order: the videos were created clip 1, 2, 3, 4, 5
        titles = [v["resource"]["snippet"]["title"] for v in m.videos.values()]
        self.assertEqual(titles, [f"Highlight {i}" for i in range(1, 6)])
        # a third run sends nothing at all, not even the OAuth request
        before = len(m.requests)
        rc, doc, _ = self.run_series(*self.confirm_all(tokens))
        self.assertEqual((rc, doc["uploaded_now"], doc["network_used"], len(m.requests)), (0, [], False, before))
        for s in list(YT.values()) + [m.access_token] + m.upload_ids:
            self.assertNotIn(s, text)

    def test_each_clip_carries_its_title_and_the_link_back(self):
        m = youtube_mock(self, YT)
        rc, doc, _ = self.run_series()
        self.assertEqual(doc["clips"][0]["metadata"]["description"], f"Part 1 of the keynote.\n\nFull video: {FULL}")
        self.assertEqual((doc["dry_run"], doc["network_used"], m.requests), (True, False, []))
        rc, doc, _ = self.run_series(*self.confirm_all(self.tokens()[:1]))
        self.assertEqual((rc, doc["uploaded_now"], doc["complete"]), (0, [1], False))
        sent = list(m.videos.values())[0]["resource"]
        self.assertEqual((sent["snippet"]["title"], sent["snippet"]["description"], sent["status"]["privacyStatus"]),
                         ("Highlight 1", f"Part 1 of the keynote.\n\nFull video: {FULL}", "public"))
        self.assertEqual(doc["clips"][1]["outcome"], "awaiting confirmation")
        self.assertIn("clip 2 is next", doc["next_step"])

    def test_each_clip_needs_its_own_token_and_the_run_stops_at_the_first_without_one(self):
        m = youtube_mock(self, YT)
        tokens = self.tokens()
        rc, doc, _ = self.run_series(*self.confirm_all([tokens[0], tokens[2]]))      # clip 2 not approved
        self.assertEqual((rc, doc["uploaded_now"]), (0, [1]))
        self.assertEqual([c["outcome"] for c in doc["clips"]][:3], ["uploaded", "awaiting confirmation", "not reached"])
        self.assertEqual(len(m.videos), 1)

    def test_a_token_matching_no_clip_is_refused_before_anything_is_sent(self):
        m = youtube_mock(self, YT)
        rc, doc, _ = self.run_series("--confirm", self.tokens()[0], "--confirm", "confirm-0000000000000000")
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
        self.assertEqual(m.requests, [])
        # a token from another platform's dry run does not work either
        same = self.write_manifest({"privacy": "public", "clips": [{"file": "clip-01.mp4", "title": "Highlight 1"}]},
                                   "one.json")
        rc, doc, _ = self.run_series(*self.confirm_all(self.tokens(platform="reels", manifest=same)), manifest=same)
        self.assertEqual(doc["error"]["code"], "CONFIRM_MISMATCH")

    def test_an_interrupted_process_leaves_the_clip_uncertain_until_a_human_resolves_it(self):
        m = youtube_mock(self, YT)
        tokens = self.tokens()
        real = S.upload
        calls = []

        def crash_on_clip_2(path, platform, confirm=None, metadata=None):
            if confirm is not None and path == self.clips[1]:
                calls.append(path)
                raise KeyboardInterrupt
            return real(path, platform, confirm, metadata)
        with mock.patch.object(S, "upload", crash_on_clip_2):
            rc, doc, _ = self.run_series(*self.confirm_all(tokens))
        self.assertEqual((rc, doc["error"]["code"]), (6, "CANCELLED"))
        rc, doc, _ = self.run_series()
        self.assertEqual([c["state"] for c in doc["clips"]][:3], ["uploaded", "sending", "pending"])
        self.assertIn("--resolve 2=", doc["next_step"])
        rc, doc, _ = self.run_series(*self.confirm_all(tokens))
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["clip"]), (4, "UPLOAD_PARTIAL", 2))
        self.assertEqual(len(m.videos), 1)
        # the human checked YouTube: clip 2 is not there
        rc, doc, _ = self.run_series("--resolve", "2=not-uploaded", *self.confirm_all(tokens))
        self.assertEqual((rc, doc["uploaded_now"], doc["complete"]), (0, [2, 3, 4, 5], True))
        self.assertEqual(len(m.videos), 5)

    def test_resolving_as_uploaded_skips_the_clip(self):
        m = youtube_mock(self, YT)
        state = S.default_state_path(self.manifest)
        first = self.tokens()[0]
        with open(state, "w", encoding="utf-8") as fh:
            json.dump({"schema": S.STATE_SCHEMA, "platform": "youtube",
                       "clips": {"1": {"status": "uncertain", "confirm_token": first}}}, fh)
        rc, doc, _ = self.run_series("--resolve", "1=uploaded", *self.confirm_all(self.tokens()))
        self.assertEqual((rc, doc["uploaded_now"]), (0, [2, 3, 4, 5]))
        self.assertEqual(len(m.videos), 4)
        rc, doc, _ = self.run_series("--resolve", "1=uploaded")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))         # not uncertain any more
        rc, doc, _ = self.run_series("--resolve", "1=maybe")
        self.assertEqual(doc["error"]["code"], "INVALID_ARGUMENT")

    def test_an_uploaded_clip_whose_metadata_changed_is_not_sent_again(self):
        m = youtube_mock(self, YT)
        rc, doc, _ = self.run_series(*self.confirm_all(self.tokens()[:1]))
        self.assertEqual(doc["uploaded_now"], [1])
        with open(self.manifest, encoding="utf-8") as fh:
            man = json.load(fh)
        man["clips"][0]["title"] = "Highlight one (edited)"
        self.write_manifest(man)
        rc, doc, _ = self.run_series()                    # even the dry run says so
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["clip"]), (2, "INVALID_ARGUMENT", 1))
        self.assertIn("not uploaded again", doc["error"]["message"])
        self.assertEqual(len(m.videos), 1)

    def test_the_state_is_tied_to_its_platform(self):
        youtube_mock(self, YT)
        self.run_series(*self.confirm_all(self.tokens()[:1]))
        with open(S.default_state_path(self.manifest), encoding="utf-8") as fh:
            state = fh.read()
        with open(S.default_state_path(self.reels_manifest), "w", encoding="utf-8") as fh:
            fh.write(state)                                   # a YouTube state next to the Reels manifest
        rc, doc, _ = self.run_series(platform="reels", manifest=self.reels_manifest)
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertIn("give another --state", doc["error"]["message"])
        rc, doc, _ = self.run_series("--state", os.path.join(self.tmp, "reels-state.json"), platform="reels",
                                     manifest=self.reels_manifest)
        self.assertEqual(rc, 0, doc)

    def test_the_state_holds_no_secret(self):
        m = youtube_mock(self, YT)
        self.run_series(*self.confirm_all(self.tokens()))
        with open(S.default_state_path(self.manifest), encoding="utf-8") as fh:
            text = fh.read()
        for s in list(YT.values()) + [m.access_token] + m.upload_ids:
            self.assertNotIn(s, text)
        self.assertNotIn("upload_id", text)
        state = json.loads(text)
        self.assertEqual({e["status"] for e in state["clips"].values()}, {"uploaded"})
        self.assertTrue(all(e["url"].startswith("https://www.youtube.com/watch?v=") for e in state["clips"].values()))


class ReelsSeriesTests(SeriesTestCase):
    def test_reels_get_the_link_in_the_caption_and_an_unknown_publish_blocks_the_series(self):
        m = reels_mock(self, IG_ID, IG_TOKEN)
        tokens = self.tokens(platform="reels", manifest=self.reels_manifest)
        m.faults["publish"] = [None, ("lost",)]                     # clip 2's publish answer is lost: unknown outcome
        rc, doc, _ = self.run_series(*self.confirm_all(tokens), platform="reels",
                                     manifest=self.reels_manifest)
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", False))
        self.assertEqual(doc["error"]["details"]["series"], {"total": 5, "uploaded": [1], "uncertain": [2]})
        self.assertEqual(m.kinds().count("publish"), 2)
        caption = [r for r in m.requests if r["kind"] == "container"][0]["form"]["caption"]
        self.assertEqual(caption, f"Highlight 1\n\nFull video: {FULL}")
        rc, doc, _ = self.run_series(*self.confirm_all(tokens), platform="reels",      # never sent again by itself
                                     manifest=self.reels_manifest)
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_PARTIAL"))
        self.assertEqual(m.kinds().count("publish"), 2)
        rc, doc, _ = self.run_series("--resolve", "2=uploaded", *self.confirm_all(tokens), platform="reels",
                                     manifest=self.reels_manifest)
        self.assertEqual((rc, doc["uploaded_now"], doc["complete"]), (0, [3, 4, 5], True))
        self.assertEqual(m.kinds().count("publish"), 5)


class ManifestTests(SeriesTestCase):
    def test_refusals_before_anything_is_sent(self):
        m = youtube_mock(self, YT)
        cases = [({"clips": []}, "INVALID_ARGUMENT"), ({"clips": [{"title": "no file"}]}, "INVALID_ARGUMENT"),
                 ({"clips": [{"file": "clip-01.mp4", "colour": "red"}]}, "INVALID_ARGUMENT"),
                 ({"full_video_url": "http://example.com/x", "clips": [{"file": "clip-01.mp4"}]}, "INVALID_ARGUMENT"),
                 ({"clips": [{"file": "missing.mp4"}]}, "INPUT_NOT_FOUND"),
                 ({"clips": [{"file": "clip-01.mp4", "title": "x" * 101}]}, "METADATA_INVALID"),
                 ({"clips": [{"file": "clip-01.mp4", "tags": "a,b"}]}, "INVALID_ARGUMENT"),
                 ({"clips": [{"file": "clip-01.mp4", "captions": [{"file": "x.srt"}]}]}, "INVALID_ARGUMENT")]
        for i, (man, code) in enumerate(cases):
            rc, doc, _ = self.run_series(manifest=self.write_manifest(man, f"m{i}.json"))
            self.assertEqual(doc["error"]["code"], code, man)
        rc, doc, _ = self.run_series(manifest=os.path.join(self.tmp, "nope.json"))
        self.assertEqual(doc["error"]["code"], "INPUT_NOT_FOUND")
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{")
        self.assertEqual(self.run_series(manifest=bad)[1]["error"]["code"], "INVALID_ARGUMENT")
        self.assertEqual(m.requests, [])

    def test_the_link_label_can_be_localised_and_the_link_is_optional(self):
        man = self.write_manifest({"full_video_url": FULL, "link_label": "本編",
                                   "clips": [{"file": "clip-01.mp4", "title": "A"}, {"file": "clip-02.mp4"}]}, "ja.json")
        rc, doc, _ = self.run_series(manifest=man)
        self.assertEqual([c["metadata"]["description"] for c in doc["clips"]], [f"本編: {FULL}"] * 2)
        man = self.write_manifest({"clips": [{"file": "clip-01.mp4", "title": "A"}]}, "nolink.json")
        rc, doc, _ = self.run_series(manifest=man)
        self.assertEqual((doc["clips"][0]["metadata"]["description"], doc["clips"][0]["metadata"]["privacy"]),
                         (None, "private"))

    def test_human_output(self):
        youtube_mock(self, YT)
        rc, out, _ = run_cli_raw("series", self.manifest, "--platform", "youtube")
        self.assertIn("dry run, nothing sent", out)
        self.assertIn("clip 5: pending  token confirm-", out)

    def test_unknown_platform_first(self):
        rc, doc = run_cli("series", os.path.join(self.tmp, "nope.json"), "--platform", "myspace", "--json")
        self.assertEqual(doc["error"]["code"], "UNKNOWN_PLATFORM")

    def test_parse_resolve(self):
        self.assertEqual(S._parse_resolve(["2=uploaded", "3=not-uploaded"]), {2: "uploaded", 3: "not-uploaded"})
        with self.assertRaises(PublishError):
            S._parse_resolve(["two=uploaded"])


if __name__ == "__main__":
    unittest.main()

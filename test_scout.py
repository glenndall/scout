"""Tests: python3 test_scout.py (with numpy installed). No network or downloads; the model test runs only if the
model file is there."""
import json, math, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout

SR = scout.SR


def tone(f0, seconds=4, tilt=1.0):
    """A synthetic voice-like sound: harmonics of f0 whose pitch keeps moving a little."""
    t = np.arange(SR * seconds) / SR
    phase = 2 * np.pi * f0 * (t + 0.12 / (2 * np.pi) * np.sin(2 * np.pi * t))
    return (0.1 * sum(np.sin(h * phase) / h ** tilt for h in range(1, 11))).astype(np.float32)


def fake_run(x):
    """Stands in for the model: a "voiceprint" from the average of each band (so it follows the sound's colour)."""
    v = x[0].std(0) + 1e-3
    return np.concatenate([v] * 7)[:512]


def vp(seed):
    v = np.random.default_rng(seed).normal(size=512)
    return v / np.linalg.norm(v)


class Voiceprint(unittest.TestCase):
    def test_fbank(self):
        f = scout.fbank(tone(170, 1))
        self.assertEqual(f.shape, (98, 80))                            # 10 ms steps over 1 s, 80 bands
        self.assertEqual(scout.fbank(np.zeros(100, np.float32)).shape, (0, 80))

    def test_pieces(self):
        self.assertIsNone(scout.voiceprint(tone(170, 1), fake_run))      # under 2 seconds: nothing to go on
        p = scout.voiceprint(tone(170, 4), fake_run)
        self.assertAlmostEqual(float(np.linalg.norm(p)), 1, places=5)
        same, other = scout.voiceprint(tone(170, 6), fake_run), scout.voiceprint(tone(170, 6, tilt=3), fake_run)
        self.assertLess(scout.voice_gap(p, same), scout.voice_gap(p, other))

    def test_match_and_judge(self):
        a, b = vp(1), vp(2)
        known = [{"name": "A", "print": a.tolist(), "channels": ["x"]},
                 {"name": "B", "print": b.tolist(), "channels": ["y", "z"]}]
        near_a = a + 0.02 * vp(3)
        who, gap = scout.voice_match(near_a / np.linalg.norm(near_a), known)
        self.assertEqual(who["name"], "A")
        self.assertLess(gap, scout.VOICE_MATCH)
        self.assertEqual(scout.voice_match(vp(4), known)[0], None)        # an unrelated voice
        self.assertEqual(scout.voice_match(None, known), (None, None))
        self.assertEqual(scout.judge(known[0], 40), (35, ["same voice as A"]))
        self.assertEqual(scout.judge(known[1], 40), (50, ["farm voice (2 channels)"]))   # heard on two channels
        self.assertEqual(scout.judge(known[1], 3), (0, []))              # hardly anyone talking: music, not a voice
        self.assertEqual(scout.judge(known[1], None)[0], 50)             # no captions to tell: go by the voice
        self.assertEqual(scout.judge(None, 40), (0, []))

    @unittest.skipUnless(os.path.isfile(scout.MODEL), "voice model not downloaded")
    def test_model(self):
        a = scout.voiceprint(tone(170, 6))
        self.assertLess(scout.voice_gap(a, scout.voiceprint(tone(170, 6)[::-1].copy())), 0.3)
        self.assertEqual(len(a), 512)


class Learn(unittest.TestCase):
    def test_learn_and_load(self):
        path = os.path.join(tempfile.mkdtemp(), "voices.json")
        self.assertEqual(scout.load_voices(path), [])
        self.assertEqual(scout.learn_voice("v1", vp(1), "Chan A", path), ("v1", 1))
        self.assertEqual(scout.learn_voice("v2", vp(2), "Chan B", path), ("v2", 2))
        self.assertEqual(scout.learn_voice("v3", vp(1), "Chan C", path), ("v1", 2))   # same voice: its channel added
        self.assertEqual(scout.learn_voice("v4", vp(1), "Chan C", path), ("v1", 2))   # ...once
        v1 = scout.load_voices(path)[0]
        self.assertEqual(v1["channels"], ["Chan A", "Chan C"])
        self.assertRegex(v1["learned"], r"^\d{4}-\d\d-\d\d$")

    def test_old_voices_skipped(self):
        path = os.path.join(tempfile.mkdtemp(), "voices.json")
        with open(path, "w") as f:
            json.dump({"voices": [{"name": "old", "pitch": 170.2, "tone": [0] * 13}]}, f)
        self.assertEqual(scout.load_voices(path), [])


class Sound(unittest.TestCase):
    def setUp(self):
        self.keep = scout.get

    def tearDown(self):
        scout.get = self.keep

    def test_start_of_sound(self):
        asked = []
        scout.get = lambda url, timeout=15: asked.append(url) or b"data"
        j = {"streamingData": {"adaptiveFormats": [
            {"mimeType": "audio/mp4", "bitrate": 130000, "contentLength": "900000", "url": "big?x=1"},
            {"mimeType": "audio/mp4", "bitrate": 32000, "contentLength": "400000", "url": "small?x=1",
             "audioTrack": {"displayName": "English (US) original", "audioIsDefault": True}},
            {"mimeType": "audio/mp4", "bitrate": 31000, "contentLength": "400000", "url": "dub?x=1",
             "audioTrack": {"displayName": "Spanish (US)", "audioIsDefault": False}},             # an AI dub
            {"mimeType": "audio/mp4", "bitrate": 30000, "contentLength": "400000", "url": "drc?x=1", "isDrc": True},
            {"mimeType": "audio/webm", "bitrate": 20000, "url": "webm?x=1"},
            {"mimeType": "video/mp4", "bitrate": 10000, "url": "video?x=1"}]}}
        self.assertEqual(scout.start_of_sound(j, 15), b"data")
        self.assertEqual(asked, ["small?x=1&range=0-99999"])            # smallest mp4 sound, first 20 s + header
        with self.assertRaises(RuntimeError):
            scout.start_of_sound({"playabilityStatus": {"reason": "Video unavailable"}})

    def test_words(self):
        j3 = json.dumps({"events": [{"tStartMs": 0, "segs": [{"utf8": "in this"}, {"utf8": " world"}]},
                                    {"tStartMs": 14000, "segs": [{"utf8": "everyone is"}]},
                                    {"tStartMs": 16000, "segs": [{"utf8": "too late"}]}]})
        self.assertEqual(scout.count_words(j3, 15), 5)
        xml = '<timedtext><body><p t="0" d="900">in this world</p><p t="20000" d="900">later</p></body></timedtext>'
        self.assertEqual(scout.count_words(xml, 15), 3)
        self.assertIsNone(scout.spoken_words({"captions": {}}))


class Ids(unittest.TestCase):
    def test_video_id(self):
        self.assertEqual(scout.video_id("https://youtube.com/shorts/tbQTA6u4lEM?si=lBvmrsKKTj00cJ7z"), "tbQTA6u4lEM")
        self.assertEqual(scout.video_id("https://www.youtube.com/watch?v=tbQTA6u4lEM"), "tbQTA6u4lEM")
        self.assertEqual(scout.video_id("tbQTA6u4lEM"), "tbQTA6u4lEM")
        self.assertIsNone(scout.video_id("hello"))


def voice_list(*voices, model=None):
    return {"format": "yakscout-voices", "version": 1, "model": model or scout.MODEL_ID, "voices": list(voices)}


class SharedLists(unittest.TestCase):
    """Shared voice lists: downloading them, using them, hiding one, and sharing a learned voice."""
    def setUp(self):
        self.keep = scout.VOICES, scout.SHARED, scout.hear, scout.voiceprint
        d = tempfile.mkdtemp()
        scout.VOICES, scout.SHARED = os.path.join(d, "voices.json"), os.path.join(d, "shared-voices.json")
        self.a = {"id": "farm-0001", "channels": ["Drip Dyl", "Volze"], "examples": ["tbQTA6u4lEM", "bad link"],
                  "print": (vp(1) * 3).tolist()}                          # not unit length: it gets normalised

    def tearDown(self):
        scout.VOICES, scout.SHARED, scout.hear, scout.voiceprint = self.keep

    def test_check_list(self):
        got = scout.check_list(voice_list(self.a, {"id": "x", "print": [1, 2]}, "junk"), "u")
        self.assertEqual([(v["name"], v["examples"], v["list"]) for v in got], [("farm-0001", ["tbQTA6u4lEM"], "u")])
        self.assertAlmostEqual(sum(x * x for x in got[0]["print"]), 1, places=3)
        with self.assertRaisesRegex(ValueError, "another voice model"):
            scout.check_list(voice_list(self.a, model="something-else/1"))
        with self.assertRaises(ValueError):
            scout.check_list({"voices": []})

    def test_refresh_use_and_hide(self):
        lists = {"https://a/voices.json": json.dumps(voice_list(self.a)).encode()}
        def fetch(u):
            if u not in lists:
                raise OSError("404")
            return lists[u]
        r = scout.refresh_lists("https://a/voices.json https://b/voices.json", fetch)
        self.assertEqual(r["https://a/voices.json"], "ok 1")
        self.assertIn("404", r["https://b/voices.json"])
        self.assertEqual([v["name"] for v in scout.known_voices()], ["farm-0001"])
        del lists["https://a/voices.json"]                                  # list a can't be reached: keep what we had
        scout.refresh_lists("https://a/voices.json", fetch)
        self.assertEqual(len(scout.shared_voices()), 1)
        near = vp(1) + 0.02 * vp(9)
        who, gap = scout.voice_match(near / np.linalg.norm(near), scout.known_voices())
        self.assertEqual((who["name"], scout.judge(who, 40)[0]), ("farm-0001", 50))   # 2 channels: drops a Short alone
        self.assertTrue(scout.forget_voice("farm-0001"))                    # x hides a shared voice here only
        self.assertEqual(scout.known_voices(), [])
        self.assertEqual(len(scout.shared_voices(hide=set())), 1)
        self.assertTrue(scout.switch_voice("farm-0001", True))              # and back on
        self.assertEqual([v["name"] for v in scout.known_voices()], ["farm-0001"])
        self.assertTrue(scout.switch_voice("farm-0001", False))             # off: listed, judges nothing
        self.assertEqual(scout.known_voices(), [])
        self.assertFalse(scout.switch_voice("farm-9999", False))
        scout.refresh_lists("off", fetch)                                   # no lists: nothing shared
        self.assertEqual(scout.shared_voices(hide=set()), [])

    def test_learn_skips_shared_and_share(self):
        scout.refresh_lists("https://a", lambda u: json.dumps(voice_list(self.a)).encode())
        scout.hear = lambda vid: (np.zeros(10, np.float32), 40, "New Chan")
        scout.voiceprint = lambda a, run=None: vp(1)                        # the shared voice
        self.assertEqual(scout.learn_short("AeyLzuVhr1s"), ("farm-0001", ["Drip Dyl", "Volze"], False))
        self.assertEqual(scout.load_voices(), [])                           # not learned twice
        scout.voiceprint = lambda a, run=None: vp(5)                        # a new voice
        self.assertEqual(scout.learn_short("qhIoV4SdbNA")[2], True)
        scout.voiceprint = lambda a, run=None: vp(5)
        scout.hear = lambda vid: (np.zeros(10, np.float32), 40, "Other Chan")
        scout.learn_short("ZN-Vzf-PCeg")
        e = scout.share_entry("qhIoV4SdbNA")
        self.assertEqual((e["id"], e["model"], e["channels"], e["examples"]),
                         ("qhiov4sdbna", scout.MODEL_ID, ["New Chan", "Other Chan"], ["qhIoV4SdbNA", "ZN-Vzf-PCeg"]))
        self.assertEqual(len(e["print"]), 512)
        self.assertIsNone(scout.share_entry("nope"))
        self.assertEqual(scout.check_list(voice_list(e))[0]["name"], "qhiov4sdbna")   # a share is a valid list entry

    def test_feed_hits_grow_a_voice(self):
        scout.refresh_lists("https://a", lambda u: json.dumps(voice_list(self.a)).encode())
        scout.voiceprint = lambda a, run=None: vp(5)
        scout.hear = lambda vid: (np.zeros(10, np.float32), 40, "Roomie Reports")
        scout.learn_short("qhIoV4SdbNA")                                    # learned once: 1 channel, 1 Short
        scout.hear = lambda vid: (np.zeros(10, np.float32), 40, "Other Chan")
        self.assertIn("new channel Other Chan", scout.look("ZN-Vzf-PCeg")[2])   # heard in the feed on another channel
        scout.look("ZN-Vzf-PCeg")                                           # the same Short again: nothing new
        e = scout.share_entry("qhIoV4SdbNA")
        self.assertEqual((e["channels"], e["examples"]), (["Roomie Reports", "Other Chan"], ["qhIoV4SdbNA", "ZN-Vzf-PCeg"]))
        scout.hear = lambda vid: (np.zeros(10, np.float32), 5, "Quiet Chan")  # too little talking to judge: not added
        scout.look("AeyLzuVhr1s")
        scout.voiceprint = lambda a, run=None: vp(1)                        # the shared voice: never changed here
        scout.hear = lambda vid: (np.zeros(10, np.float32), 40, "Third Chan")
        scout.look("AeyLzuVhr1s")
        self.assertEqual(scout.load_voices()[0]["channels"], ["Roomie Reports", "Other Chan"])
        self.assertEqual(scout.shared_voices()[0]["channels"], ["Drip Dyl", "Volze"])
        scout.voiceprint = lambda a, run=None: vp(5)                        # a busy farm: 60 more channels
        for i in range(60):
            scout.hear = lambda vid, i=i: (np.zeros(10, np.float32), 40, f"Chan {i}")
            scout.look(f"x{i:010d}")
        v = scout.load_voices()[0]
        self.assertEqual((len(v["channels"]), len(v["examples"])), (50, 50))   # kept at home
        e = scout.share_entry("qhIoV4SdbNA")
        self.assertEqual((len(e["channels"]), len(e["examples"])), (20, 10))   # sent: enough to review, link fits


class Server(unittest.TestCase):
    """What YakTube talks to: GET /status and POST /look."""
    def setUp(self):
        self.keep = scout.look, scout.VOICES, scout.SHARED
        d = tempfile.mkdtemp()
        scout.VOICES, scout.SHARED = os.path.join(d, "voices.json"), os.path.join(d, "shared-voices.json")
        scout.learn_voice("tbQTA6u4lEM", vp(1), "Drip Dyl")
        self.srv = scout.ThreadingHTTPServer(("127.0.0.1", 0), scout.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        scout.look, scout.VOICES, scout.SHARED = self.keep

    def ask(self, path, body=None):
        req = urllib.request.Request(self.base + path, None if body is None else json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    def test_status(self):
        code, st = self.ask("/status")
        self.assertEqual(code, 200)
        self.assertEqual(st["busy"], False)
        self.assertEqual([(v["name"], v["channels"]) for v in st["voices"]], [("tbQTA6u4lEM", 1)])
        self.assertRegex(st["voices"][0]["learned"], r"^[A-Z][a-z]{2} \d{1,2}$")              # e.g. "Nov 13"

    def test_learn_and_forget(self):
        keep = scout.learn_short
        def learn(vid):
            if vid == "quiet000000":
                raise scout.NoVoice("not enough talking at the start to learn a voice from")
            scout.learn_voice(vid, vp(1), "Volze")
            return "tbQTA6u4lEM", ["Drip Dyl", "Volze"], False
        scout.learn_short = learn
        try:
            self.assertEqual(self.ask("/learn", {"id": "AeyLzuVhr1s"}),
                             (200, {"name": "tbQTA6u4lEM", "channels": ["Drip Dyl", "Volze"], "new": False}))
            self.assertEqual(self.ask("/learn", {"id": "quiet000000"})[0], 422)
            self.assertEqual(self.ask("/learn", {"id": "nope"})[0], 400)
        finally:
            scout.learn_short = keep
        st = self.ask("/status")[1]
        self.assertEqual(st["voices"][0]["channel_names"], ["Drip Dyl", "Volze"])
        self.assertGreaterEqual(st["up"], 0)
        self.assertEqual(self.ask("/forget", {"name": "tbQTA6u4lEM"}), (200, {"forgot": True}))
        self.assertEqual(self.ask("/forget", {"name": "tbQTA6u4lEM"}), (200, {"forgot": False}))
        self.assertEqual(self.ask("/forget", {})[0], 400)
        self.assertEqual(self.ask("/status")[1]["voices"], [])

    def test_backup_and_restore_voices(self):
        full = self.ask("/voices")[1]["voices"]
        self.assertEqual(len(full[0]["print"]), 512)                              # voiceprints included
        scout.forget_voice("tbQTA6u4lEM")
        bad = {"name": "junk", "print": [1, 2]}
        self.assertEqual(self.ask("/voices", {"voices": full + [bad, "x"]}), (200, {"added": 1, "voices": 1}))
        again = dict(full[0], name="other name", channels=["Volze"])            # same voice under another name
        self.assertEqual(self.ask("/voices", {"voices": [again]})[1], {"added": 1, "voices": 1})
        self.assertEqual(scout.load_voices()[0]["channels"], ["Drip Dyl", "Volze"])   # merged into it

    def test_share_and_status_sources(self):
        scout.refresh_lists("https://a", lambda u: json.dumps(voice_list({"id": "farm-0002", "channels": ["A", "B"],
                                                                          "print": vp(7).tolist()})).encode())
        st = self.ask("/status")[1]
        self.assertEqual([(v["name"], v["source"]) for v in st["voices"]], [("tbQTA6u4lEM", "learned"), ("farm-0002", "shared")])
        self.assertEqual([(l["url"], l["count"]) for l in st["lists"]], [("https://a", 1)])
        self.assertEqual(st["voices"][0]["examples"], len(scout.load_voices()[0].get("examples", [])) or 1)   # what Share would send
        code, e = self.ask("/share?name=tbQTA6u4lEM")
        self.assertEqual((code, e["channels"], e["model"]), (200, ["Drip Dyl"], scout.MODEL_ID))
        self.assertEqual(self.ask("/share?name=farm-0002")[0], 404)          # only your own voices are shared from here
        self.assertEqual([v["on"] for v in st["voices"]], [True, True])
        self.assertEqual(self.ask("/switch", {"name": "tbQTA6u4lEM", "on": False}), (200, {"name": "tbQTA6u4lEM", "on": False}))
        self.assertEqual(self.ask("/switch", {"name": "farm-0002", "on": False})[0], 200)
        st = self.ask("/status")[1]                                          # off ones stay listed, marked off,
        self.assertEqual([(v["name"], v["on"]) for v in st["voices"]], [("tbQTA6u4lEM", False), ("farm-0002", False)])
        self.assertEqual(scout.known_voices(), [])                           # and judge nothing
        self.assertEqual(self.ask("/switch", {"name": "tbQTA6u4lEM", "on": True})[0], 200)
        self.assertEqual([v["name"] for v in scout.known_voices()], ["tbQTA6u4lEM"])
        self.assertEqual(self.ask("/switch", {"name": "nobody", "on": True})[0], 404)
        self.assertEqual(self.ask("/switch", {"name": "tbQTA6u4lEM", "on": "yes"})[0], 400)

    def test_verify(self):
        keep = scout.hear, scout.voiceprint
        scout.hear = lambda vid: (np.zeros(10, np.float32), 40, "Chan " + vid[:2])
        scout.voiceprint = lambda a, run=None: vp(1) if a is not None and len(a) == 10 else vp(9)
        try:
            code, r = self.ask("/verify", {"entry": {"id": "x", "print": vp(1).tolist(), "examples": ["tbQTA6u4lEM", "bad"]},
                                           "others": ["qhIoV4SdbNA", "bad"]})
            scout.hear = lambda vid: (np.zeros(11, np.float32), 40, "Other " + vid[:2])         # an ordinary Short: another voice
            code2, r2 = self.ask("/verify", {"entry": {"id": "x", "print": vp(1).tolist(), "examples": []},
                                             "others": ["qhIoV4SdbNA"]})
        finally:
            scout.hear, scout.voiceprint = keep
        self.assertEqual(code, 200)
        self.assertEqual([(e["id"], e["channel"], round(e["gap"], 3)) for e in r["examples"]], [("tbQTA6u4lEM", "Chan tb", 0.0)])
        self.assertEqual([(e["id"], round(e["gap"], 3)) for e in r["others"]], [("qhIoV4SdbNA", 0.0)])   # same voice: a false alarm
        self.assertEqual(code2, 200)
        self.assertGreater(r2["others"][0]["gap"], scout.VOICE_MATCH)        # a different voice: no alarm
        self.assertEqual(self.ask("/verify", {"entry": {"print": vp(1).tolist()}})[1]["others"], [])   # none asked for
        self.assertEqual(self.ask("/verify", {"entry": {"print": [1]}})[0], 400)

    def test_look(self):
        def look(vid, voices=None):
            if vid == "gone0000000":
                raise RuntimeError("Video unavailable")
            return 50, ["farm voice (2 channels)"], "facts"
        scout.look = look
        self.assertEqual(self.ask("/look", {"id": "tbQTA6u4lEM"}),
                         (200, {"score": 50, "why": ["farm voice (2 channels)"], "facts": "facts"}))
        self.assertEqual(self.ask("/look", {"id": "gone0000000"}), (502, {"error": "Video unavailable"}))
        self.assertEqual(self.ask("/look", {"id": "not an id"})[0], 400)
        self.assertEqual(self.ask("/look", {})[0], 400)
        self.assertEqual(self.ask("/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()

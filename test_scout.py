"""Tests: python3 test_scout.py. No network, no downloads, no ffmpeg needed (synthetic sound and pictures)."""
import array, json, math, os, sys, tempfile, threading, unittest, urllib.error, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import scout

R = scout.RATE


def voice(f0, tilt=1.0, seconds=10):
    """A synthetic voice: harmonics of f0, quieter the higher they go (tilt sets how fast). Like speech, its pitch
    keeps moving (here +-12% over each second) around f0."""
    out, phase = array.array("h"), 0.0
    for t in range(R * seconds):
        phase += 2 * math.pi * f0 * (1 + 0.12 * math.sin(2 * math.pi * t / R)) / R
        out.append(int(3000 * sum(math.sin(h * phase) / h ** tilt for h in range(1, 11))))
    return out


def picture(yellow_at=None):
    """One CW x CH frame, dark grey, with an optional small yellow block (a caption word) at x."""
    f = bytearray([40] * (scout.CW * scout.CH * 3))
    if yellow_at is not None:
        for y in range(int(scout.CH * .6), int(scout.CH * .6) + 4):
            for x in range(yellow_at, yellow_at + 10):
                i = (y * scout.CW + x) * 3
                f[i:i + 3] = bytes([250, 220, 30])
    return bytes(f)


class Voice(unittest.TestCase):
    def test_pitch_and_match(self):
        p = scout.voice_profile(voice(170))
        self.assertAlmostEqual(p["pitch"], 170, delta=5)
        known = [dict(p, name="farm")]
        self.assertEqual(scout.voice_match(scout.voice_profile(voice(171)), known), "farm")     # same voice
        self.assertIsNone(scout.voice_match(scout.voice_profile(voice(250)), known))            # higher voice
        self.assertIsNone(scout.voice_match(scout.voice_profile(voice(170, tilt=3)), known))    # same pitch, other tone
        self.assertIsNone(scout.voice_match(None, known))

    def test_no_speech(self):
        self.assertIsNone(scout.voice_profile(array.array("h", [0] * R * 10)))                  # silence
        self.assertIsNone(scout.voice_profile(voice(170, seconds=1)))                           # too little

    def test_learn_and_load(self):
        path = os.path.join(tempfile.mkdtemp(), "voices.json")
        self.assertEqual(scout.load_voices(path), [])                                           # none yet
        self.assertEqual(scout.learn_voice("a", {"pitch": 170, "tone": [0] * 13}, path), 1)
        self.assertEqual(scout.learn_voice("b", {"pitch": 120, "tone": [0] * 13}, path), 2)
        self.assertEqual(scout.learn_voice("a", {"pitch": 171, "tone": [0] * 13}, path), 2)     # same name replaces
        self.assertEqual({v["name"]: v["pitch"] for v in scout.load_voices(path)}, {"a": 171, "b": 120})
        self.assertRegex(scout.load_voices(path)[0]["learned"], r"^\d{4}-\d\d-\d\d$")         # when it was learned


class Captions(unittest.TestCase):
    def test_word_by_word(self):
        fps = scout.CFPS
        words = [picture(10 + (i // 3 % 2) * 30) for i in range(fps * 10)]   # a new word every 3 pictures
        c = scout.caption_facts(words)
        self.assertGreater(c["on"], 0.9)
        self.assertGreater(c["per_sec"], 1.5)
        self.assertEqual(scout.judge(c, None), (15, ["word-by-word captions"]))

    def test_none_or_still(self):
        self.assertEqual(scout.caption_facts([picture()] * scout.CFPS * 10)["on"], 0)      # no yellow at all
        still = scout.caption_facts([picture(20)] * scout.CFPS * 10)                       # a yellow logo that never changes
        self.assertEqual(still["per_sec"], 0)
        self.assertEqual(scout.judge(still, None), (0, []))
        self.assertIsNone(scout.caption_facts([picture(20)] * 10))                         # too short


class Judge(unittest.TestCase):
    def test_weights(self):
        caps = {"on": 0.9, "per_sec": 2.5}
        self.assertEqual(scout.judge(caps, "farm"), (50, ["same voice as farm", "word-by-word captions"]))
        self.assertEqual(scout.judge(None, "farm")[0], 35)
        self.assertEqual(scout.judge({"on": 0.3, "per_sec": 2.5}, None)[0], 0)             # captions only now and then

    def test_video_id(self):
        self.assertEqual(scout.video_id("https://youtube.com/shorts/tbQTA6u4lEM?si=lBvmrsKKTj00cJ7z"), "tbQTA6u4lEM")
        self.assertEqual(scout.video_id("https://www.youtube.com/watch?v=tbQTA6u4lEM"), "tbQTA6u4lEM")
        self.assertEqual(scout.video_id("tbQTA6u4lEM"), "tbQTA6u4lEM")
        self.assertIsNone(scout.video_id("hello"))


class Server(unittest.TestCase):
    """What YakTube talks to: GET /status and POST /look."""
    def setUp(self):
        self.keep = scout.look, scout.VOICES
        scout.VOICES = os.path.join(tempfile.mkdtemp(), "voices.json")
        scout.learn_voice("tbQTA6u4lEM", {"pitch": 170.2, "tone": [0] * 13})
        self.srv = scout.ThreadingHTTPServer(("127.0.0.1", 0), scout.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        scout.look, scout.VOICES = self.keep

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
        self.assertEqual([(v["name"], v["pitch"]) for v in st["voices"]], [("tbQTA6u4lEM", 170)])
        self.assertRegex(st["voices"][0]["learned"], r"^[A-Z][a-z]{2} \d{1,2}$")              # e.g. "Nov 13"

    def test_look(self):
        def look(vid, voices=None):
            if vid == "gone0000000":
                raise RuntimeError("Video unavailable")
            return 50, ["same voice as x", "word-by-word captions"], "facts"
        scout.look = look
        self.assertEqual(self.ask("/look", {"id": "tbQTA6u4lEM"}),
                         (200, {"score": 50, "why": ["same voice as x", "word-by-word captions"], "facts": "facts"}))
        self.assertEqual(self.ask("/look", {"id": "gone0000000"}), (502, {"error": "Video unavailable"}))   # couldn't watch
        self.assertEqual(self.ask("/look", {"id": "not an id"})[0], 400)
        self.assertEqual(self.ask("/look", {})[0], 400)
        self.assertEqual(self.ask("/nope")[0], 404)


if __name__ == "__main__":
    unittest.main()

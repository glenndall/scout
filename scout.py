#!/usr/bin/env python3
"""Scout: watches the Shorts YakTube is about to show and tells YakTube which ones come from content farms.

Content farms are the faceless channels that turn out Shorts by the hundred from one formula to farm views and ad
money. Being made with AI isn't what Scout looks for: plenty of people make clever, funny things with AI. What gives a
farm away is the formula repeated, above all the same synthetic voice across channel after channel.

Runs on any computer YakTube can reach (a Mac, an old Ubuntu PC), not on the NAS. Needs Python 3.9+, yt-dlp and
ffmpeg; nothing else. It waits for YakTube to connect (put this computer's address in YakTube's Settings > Scout).
YakTube hands it Shorts one at a time (POST /look); Scout downloads each at low quality and looks at the video itself:
  - the voice: farms use the same text-to-speech voice again and again. Scout learns that voice from Shorts you know
    are from a farm (`scout.py learn <link>`) and recognises it by its pitch and tone.
  - the captions: one bold yellow word at a time, in the middle of the screen (a clue, not proof: people use them too)
and answers with what it found. YakTube adds that to what it already read in the title and captions, and drops the
Short from the Shorts list if the total is high enough. If this isn't running, YakTube just carries on with the
title/caption check alone.

    python3 scout.py                       wait for YakTube (port 8095; --port to change it)
    python3 scout.py check <link or id>    show what Scout makes of a Short (YakTube isn't told)
    python3 scout.py learn <link or id>    this Short is from a farm: remember its voice
"""
import argparse, array, json, math, os, re, shutil, statistics, subprocess, sys, tempfile, threading, time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
VOICES = os.environ.get("SCOUT_VOICES", os.path.join(HERE, "voices.json"))   # voices learned from known farm Shorts
PORT = int(os.environ.get("SCOUT_PORT", "8095"))                                # where YakTube finds it
VERSION = "0.2"
RATE = 8000                # sound looked at in this many samples a second
CFPS, CW, CH = 8, 72, 128  # pictures for the caption check: 8 a second, shrunk to 72 x 128 pixels
VID_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])")

VOICE_PITCH = 0.05         # a voice matches a learned one if its pitch is within this many octaves (0.05 = 3.5%)...
VOICE_TONE = 3.2           # ...and its tone (average loudness across the pitch range) is within this many dB
SCORE_VOICE, SCORE_CAPTIONS = 35, 15
BANDS = [200, 300, 400, 500, 650, 800, 1000, 1250, 1550, 1900, 2400, 3000, 3600]   # Hz, where tone is measured


# ---------------------------------------------------------------- tools
def ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:                                   # pip install imageio-ffmpeg brings its own
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found. Install it (Ubuntu: sudo apt install ffmpeg) or: pip install imageio-ffmpeg")


def ytdlp():
    here = os.path.join(os.path.dirname(sys.executable), "yt-dlp")          # e.g. a virtualenv's bin/
    exe = os.environ.get("YT_DLP") or shutil.which("yt-dlp") or (here if os.path.isfile(here) else None)
    if exe:
        return [exe]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except ImportError:
        sys.exit("yt-dlp not found. Install it: pip install yt-dlp (keep it up to date: YouTube changes often)")


def download(vid, folder):
    """The Short, at the lowest quality there is, into `folder`. Returns the file's path."""
    r = subprocess.run(ytdlp() + ["--no-playlist", "--quiet", "--no-warnings", "-f", "worstvideo*+worstaudio/worst",
                                  "--merge-output-format", "mp4", "--ffmpeg-location", ffmpeg(), "--socket-timeout", "15",
                                  "-o", os.path.join(folder, "%(id)s.%(ext)s"), "https://www.youtube.com/shorts/" + vid],
                       capture_output=True, text=True, timeout=300)
    files = [f for f in os.listdir(folder) if f.startswith(vid)]
    if r.returncode or not files:
        raise RuntimeError((r.stderr.strip().splitlines() or ["download failed"])[-1][:200])
    return os.path.join(folder, files[0])


def frames(path):
    """The video as small colour pictures, CFPS a second: a list of CW*CH*3-byte strings (red, green, blue...)."""
    out = subprocess.run([ffmpeg(), "-v", "error", "-i", path, "-an", "-vf", f"fps={CFPS},scale={CW}:{CH}",
                          "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, timeout=180).stdout
    n = CW * CH * 3
    return [out[i:i + n] for i in range(0, len(out) - n + 1, n)]


def samples(path):
    """The sound as mono 16-bit numbers at RATE a second (empty if there isn't any)."""
    out = subprocess.run([ffmpeg(), "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                         capture_output=True, timeout=180).stdout
    a = array.array("h")
    a.frombytes(out[:len(out) // 2 * 2])
    if sys.byteorder == "big":
        a.byteswap()
    return a


# ---------------------------------------------------------------- looking (pure functions, no files)
def caption_facts(fr, fps=CFPS):
    """Bold yellow word-by-word captions: how much of the time there are yellow letters in the middle of the screen,
    and how often they change. None if the video is too short."""
    if len(fr) < fps * 8:
        return None
    on, changes, prev = 0, 0, None
    for f in fr:
        pts = set()
        for y in range(int(CH * .35), int(CH * .8)):
            row = y * CW * 3
            for x in range(CW):
                i = row + x * 3
                if f[i] > 190 and f[i + 1] > 170 and f[i + 2] < 100:        # yellow
                    pts.add((x, y))
        if 6 <= len(pts) <= 600:                                             # a few words, not a yellow wall
            on += 1
            if prev is not None and len(pts & prev) < 0.5 * min(len(pts), len(prev)):
                changes += 1                                                 # different letters from a moment ago
            prev = pts
        else:
            prev = None
    return {"on": on / len(fr), "per_sec": changes / (len(fr) / fps)}


def rms(chunk):
    return (sum(x * x for x in chunk) / len(chunk)) ** 0.5


def _goertzel(fr, f):
    w = 2 * math.cos(2 * math.pi * f / RATE)
    s1 = s2 = 0.0
    for x in fr:
        s1, s2 = x + w * s1 - s2, s1
    return s1 * s1 + s2 * s2 - w * s1 * s2


def _pitch(fr):
    """The voice's pitch in Hz for one 64 ms stretch (0 if it isn't a clear voiced sound)."""
    e = sum(x * x for x in fr)
    if not e:
        return 0
    best, at = 0.0, 0
    for lag in range(20, 161):                                               # 400 Hz down to 50 Hz
        c = sum(map(float.__mul__, fr[:len(fr) - lag], fr[lag:])) / e
        if c > best:
            best, at = c, lag
    return RATE / at if best > 0.45 else 0


def voice_profile(a):
    """A voice's fingerprint: its usual pitch and its tone (how loud each part of the range is, relative to the
    whole). None if there is hardly any speech."""
    n = 512
    speech = [i for i in range(0, len(a) - n, n) if rms(a[i:i + n]) > 800]
    if len(speech) < 20:
        return None
    pitches, tone = [], [0.0] * len(BANDS)
    pick = speech[::max(1, len(speech) // 60)][:60]
    for i in pick:
        fr = [float(x) for x in a[i:i + n]]
        m = sum(fr) / n
        fr = [(x - m) * (0.5 - 0.5 * math.cos(2 * math.pi * k / (n - 1))) for k, x in enumerate(fr)]   # smoothed edges
        p = _pitch(fr)
        if p:
            pitches.append(p)
        for k, b in enumerate(BANDS):                                       # each band: 5 points across a 1/3 octave,
            tone[k] += math.log10(sum(_goertzel(fr, b * 2 ** (j / 12)) for j in (-2, -1, 0, 1, 2)) + 1e-9)   # so a harmonic
                                                                               # landing between them doesn't swing it
    if len(pitches) < 10:
        return None
    mean = sum(tone) / len(tone)
    return {"pitch": round(statistics.median(pitches), 1), "tone": [round(10 * (t / len(pick) - mean / len(pick)), 1) for t in tone]}


def voice_gap(p, q):
    """(tone difference in dB, pitch difference in octaves) between two voice fingerprints."""
    return (math.sqrt(sum((x - y) ** 2 for x, y in zip(p["tone"], q["tone"])) / len(BANDS)),
            abs(math.log2(p["pitch"] / q["pitch"])))


def voice_match(p, known):
    """The learned voice this one matches, or None. `known` is a list of {"name", "pitch", "tone"}."""
    if not p:
        return None
    for k in known:
        tone, pitch = voice_gap(p, k)
        if pitch <= VOICE_PITCH and tone <= VOICE_TONE:
            return k["name"]
    return None


def judge(caps, voice):
    """(score 0-100, reasons) from caption_facts and the name of the learned voice it matched (or None). Neither is
    proof alone: YakTube adds this to its own score and only drops a Short when the total is high enough."""
    hits = []
    if voice:
        hits.append((SCORE_VOICE, f"same voice as {voice}"))
    if caps and caps["on"] >= 0.5 and caps["per_sec"] >= 1.5:
        hits.append((SCORE_CAPTIONS, "word-by-word captions"))
    return min(100, sum(n for n, _ in hits)), [w for _, w in hits]


def load_voices(path=None):
    try:
        with open(path or VOICES) as f:
            return json.load(f).get("voices", [])
    except (OSError, ValueError):
        return []


def learn_voice(name, profile, path=None):
    """Remember a voice from a Short known to be from a farm (replacing an earlier one of the same name)."""
    path = path or VOICES
    voices = [v for v in load_voices(path) if v["name"] != name] + [dict(profile, name=name, learned=time.strftime("%Y-%m-%d"))]
    with open(path, "w") as f:
        json.dump({"voices": voices}, f, indent=1)
    return len(voices)


def look(vid, voices=None):
    """Download one Short, look at it, delete it. (score, reasons, facts line)."""
    folder = tempfile.mkdtemp(prefix="scout-")
    try:
        path = download(vid, folder)
        caps, prof = caption_facts(frames(path)), voice_profile(samples(path))
        who = voice_match(prof, load_voices() if voices is None else voices)
        score, why = judge(caps, who)
        facts = (f"captions {caps['on']:.0%} on, {caps['per_sec']:.1f}/s" if caps else "too short") + \
                (f" | voice {prof['pitch']:.0f} Hz" if prof else " | no voice")
        return score, why, facts
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------- waiting for YakTube
LOOKING = threading.Lock()                                    # one Short at a time: it's the download that's slow


def status():
    """What YakTube shows in Settings > Scout."""
    def when(v):
        try:
            return time.strftime("%b %-d", time.strptime(v.get("learned", ""), "%Y-%m-%d"))
        except ValueError:
            return ""
    return {"scout": VERSION, "busy": LOOKING.locked(),
            "voices": [{"name": v["name"], "pitch": round(v["pitch"]), "learned": when(v)} for v in load_voices()]}


class Handler(BaseHTTPRequestHandler):
    """GET /status, and POST /look {"id": "..."} -> {"score": 0-100, "why": [...], "facts": "..."}."""
    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] == "/status":
            return self.reply(200, status())
        self.reply(404, {"error": "not here"})

    def do_POST(self):
        if self.path != "/look":
            return self.reply(404, {"error": "not here"})
        try:
            vid = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1000)) or b"{}").get("id", "")
        except ValueError:
            vid = ""
        if not (isinstance(vid, str) and VID_RE.fullmatch(vid)):
            return self.reply(400, {"error": "need an id"})
        with LOOKING:
            try:
                score, why, facts = look(vid)
            except Exception as ex:                             # couldn't download or read it
                print(f"[scout] {vid}: couldn't look ({ex})", flush=True)
                return self.reply(502, {"error": str(ex)[:200]})
        print(f"[scout] {vid} {score:3d}  {'; '.join(why) or '-'}  [{facts}]", flush=True)
        self.reply(200, {"score": score, "why": why, "facts": facts})

    def log_message(self, fmt, *args):
        pass


def serve(host, port):
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"Scout {VERSION} waiting for YakTube on port {port}. In YakTube: Settings > Scout, this computer's address.",
          flush=True)
    srv.serve_forever()


def video_id(s):
    """An 11-character id from an id, a watch?v= link, or a youtu.be / shorts/ link."""
    u = urllib.parse.urlsplit(s.strip())
    for part in urllib.parse.parse_qs(u.query).get("v", []) + [u.path.rstrip("/").rsplit("/", 1)[-1], s.strip()]:
        if VID_RE.fullmatch(part):
            return part
    return None


def main():
    ap = argparse.ArgumentParser(description="Tell YakTube which Shorts come from content farms, by watching them.")
    ap.add_argument("what", nargs="*", help="'check <link>...' or 'learn <link>...' (leave out to wait for YakTube)")
    ap.add_argument("--port", type=int, default=PORT, help=f"port YakTube connects to (default {PORT})")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all, so YakTube can reach it)")
    a = ap.parse_args()
    cmd, args = (a.what[0], a.what[1:]) if a.what else ("serve", [])
    if cmd not in ("serve", "check", "learn"):
        ap.error("did you mean: scout.py check <link or id>, or scout.py learn <link or id>?")
    if cmd == "serve":
        return serve(a.host, a.port)
    for s in args:
        vid = video_id(s)
        if not vid:
            print(f"{s}: not a YouTube link or id")
            continue
        try:
            if cmd == "check":
                score, why, facts = look(vid)
                print(f"{vid} {score:3d}  {'; '.join(why) or '-'}  [{facts}]")
            else:
                folder = tempfile.mkdtemp(prefix="scout-")
                try:
                    prof = voice_profile(samples(download(vid, folder)))
                finally:
                    shutil.rmtree(folder, ignore_errors=True)
                if not prof:
                    print(f"{vid}: not enough speech to learn a voice from")
                else:
                    n = learn_voice(vid, prof)
                    print(f"{vid}: learned a voice at {prof['pitch']:.0f} Hz ({n} known in {VOICES})")
        except Exception as ex:
            print(f"{vid}: couldn't look ({ex})")


if __name__ == "__main__":
    main()

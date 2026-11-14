#!/usr/bin/env python3
"""YakScout: listens to the Shorts YakTube is about to show and tells YakTube which ones come from content farms.

Content farms are the faceless channels that turn out Shorts by the hundred from one formula to farm views and ad
money. Being made with AI isn't what YakScout looks for: plenty of people make clever, funny things with AI. What gives a
farm away is the formula repeated, above all the same synthetic voice across channel after channel.

Runs on any computer YakTube can reach (a Mac, an Ubuntu PC, a NAS with Docker). Needs Python 3.9+, ffmpeg, numpy,
onnxruntime and the voice model in models/ (see the README); yt-dlp is only a backup. It waits for YakTube to connect
(put this computer's address in YakTube's Settings > YakScout). YakTube hands it Shorts one at a time (POST /look); for
each, YakScout fetches just the first 15 seconds of sound (about 80 KB) and:
  - checks someone is talking (YouTube's own automatic captions for those seconds),
  - turns the voice into a voiceprint with a speaker-recognition model (CAM++, trained to tell people's voices apart),
  - compares it with the voices it has learned from Shorts known to be from farms (`scout.py learn <link>`).
A voice heard on two or more channels counts for more: that's the farm's tell. YakTube adds YakScout's score to what it
already read in the title and captions, and drops the Short if the total is high enough. If YakScout isn't running,
YakTube just carries on with the title/caption check alone.

    python3 scout.py                       wait for YakTube (port 8095; --port to change it)
    python3 scout.py check <link or id>    show what YakScout makes of a Short (YakTube isn't told)
    python3 scout.py learn <link or id>    this Short is from a farm: remember its voice
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, threading, time
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
VOICES = os.environ.get("SCOUT_VOICES", os.path.join(HERE, "voices.json"))   # voices learned from known farm Shorts
MODEL = os.environ.get("SCOUT_MODEL", os.path.join(HERE, "models", "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"))
PORT = int(os.environ.get("SCOUT_PORT", "8095"))                                # where YakTube finds it
VERSION = "1.0"
MODEL_ID = "campplus-en-voxceleb-16k/fbank80-2s-avg/1"   # what made a voiceprint: only ones made the same way compare
# Shared voice lists (one or more URLs, space-separated; "off" for none): farm voices other people found, downloaded
# once a day and used alongside the ones learned here. Kept in shared-voices.json next to voices.json.
LISTS = os.environ.get("SCOUT_LISTS", "https://raw.githubusercontent.com/glenndall/yakscout-voices/main/voices.json")
SHARED = os.path.join(os.path.dirname(os.path.abspath(VOICES)), "shared-voices.json")
VID_RE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{11}(?![A-Za-z0-9_-])")

SR = 16000                 # sound looked at in this many samples a second
LISTEN = 15                # seconds from the start of each Short (tested: as reliable as the whole Short)
MIN_WORDS = 15             # spoken words needed in those seconds to judge a voice (music alone has its own "voice")
VOICE_MATCH = 0.25         # voiceprints this close (1 - cosine) are the same voice. Tested on 131 Shorts: farm voices
                           # 0.05-0.20 apart, every other Short 0.73 or more from them
SCORE_VOICE, SCORE_SHARED = 35, 50   # a learned voice; one learned from two or more channels (a farm's tell)
UA = "com.google.android.youtube/20.10.38 (Linux; U; Android 14) gzip"


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
        return None


# ---------------------------------------------------------------- getting the sound
def player(vid):
    """YouTube's answer for one Short as its Android app asks for it (no login): details, sound links, captions."""
    body = {"context": {"client": {"clientName": "ANDROID", "clientVersion": "20.10.38", "androidSdkVersion": 34,
                                   "osName": "Android", "osVersion": "14", "hl": "en", "gl": "US"}}, "videoId": vid}
    req = urllib.request.Request("https://www.youtube.com/youtubei/v1/player?prettyPrint=false", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "User-Agent": UA,
                                  "X-YouTube-Client-Name": "3", "X-YouTube-Client-Version": "20.10.38"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def get(url, timeout=15):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout) as r:
        return r.read()


def start_of_sound(j, secs=LISTEN):
    """The first `secs` seconds of the Short's smallest sound file (bytes), from a player() answer. YouTube only
    hands out the start of a file this way, never the whole of it, which is all YakScout wants."""
    fm = [f for f in j.get("streamingData", {}).get("adaptiveFormats", [])
          if f.get("mimeType", "").startswith("audio/mp4") and f.get("url")
          and f.get("audioTrack", {}).get("audioIsDefault", True) and not f.get("isDrc")]   # the original voice, not
    if not fm:                                                                             # one of YouTube's AI dubs
        raise RuntimeError(j.get("playabilityStatus", {}).get("reason") or "no sound to fetch")
    f = min(fm, key=lambda f: f.get("bitrate", 1 << 30))
    n = int(f.get("bitrate", 130000) / 8 * (secs + 5)) + 20000                  # a few seconds spare, plus the header
    if f.get("contentLength"):
        n = min(n, int(f["contentLength"]) - 1)
    return get(f["url"] + f"&range=0-{n - 1}")


def spoken_words(j, secs=LISTEN):
    """How many words YouTube's automatic captions hear in the first `secs` seconds; None if it has none."""
    tracks = j.get("captions", {}).get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
    asr = [t for t in tracks if t.get("kind") == "asr" and t.get("baseUrl")]
    if not asr:
        return None
    return count_words(get(asr[0]["baseUrl"] + "&fmt=json3").decode("utf-8", "replace"), secs)


def count_words(text, secs):
    """Words starting before `secs` seconds in a captions file (json3, or the XML YouTube sometimes sends instead)."""
    try:
        ev = json.loads(text).get("events", [])
        return sum(len("".join(s.get("utf8", "") for s in e.get("segs", [])).split())
                   for e in ev if e.get("tStartMs", 0) < secs * 1000)
    except ValueError:
        return sum(len(re.sub(r"<[^>]+>", " ", body).split())
                   for t, body in re.findall(r'<p t="(\d+)"[^>]*>(.*?)</p>', text, re.S) if int(t) < secs * 1000)


def decode(data_or_path, secs=LISTEN):
    """Sound (bytes of a file, or a path) as numbers between -1 and 1, SR a second, mono, the first `secs` seconds."""
    import numpy as np
    src = ["-i", data_or_path] if isinstance(data_or_path, str) else ["-i", "pipe:0"]
    out = subprocess.run([ffmpeg(), "-v", "error"] + src + ["-t", str(secs), "-vn", "-ac", "1", "-ar", str(SR),
                                                          "-f", "s16le", "-"],
                         input=None if isinstance(data_or_path, str) else data_or_path, capture_output=True,
                         timeout=60).stdout
    return np.frombuffer(out[:len(out) // 2 * 2], dtype="<i2").astype(np.float32) / 32768


def download_start(vid, folder, secs=LISTEN):
    """Backup when YouTube won't hand the sound over directly: yt-dlp fetches the first seconds of it."""
    cmd = ytdlp()
    if not cmd:
        raise RuntimeError("couldn't fetch the sound, and yt-dlp (the backup) isn't installed")
    r = subprocess.run(cmd + ["--no-playlist", "--quiet", "--no-warnings", "-f", "worstaudio/worst",
                              "--download-sections", f"*0-{secs + 2}", "--ffmpeg-location", ffmpeg(),
                              "--socket-timeout", "15", "-o", os.path.join(folder, "%(id)s.%(ext)s"),
                              "https://www.youtube.com/shorts/" + vid], capture_output=True, text=True, timeout=300)
    files = [f for f in os.listdir(folder) if f.startswith(vid)]
    if r.returncode or not files:
        raise RuntimeError((r.stderr.strip().splitlines() or ["download failed"])[-1][:200])
    return os.path.join(folder, files[0])


def hear(vid):
    """(sound of the first LISTEN seconds, spoken words in them or None if unknown, channel name)."""
    try:
        j = player(vid)
        return decode(start_of_sound(j)), spoken_words(j), j.get("videoDetails", {}).get("author", "")
    except Exception:
        folder = tempfile.mkdtemp(prefix="scout-")
        try:
            return decode(download_start(vid, folder)), None, ""
        finally:
            shutil.rmtree(folder, ignore_errors=True)


# ---------------------------------------------------------------- voiceprints
_MODEL, _FB = None, None


def model():
    global _MODEL
    if _MODEL is None:
        if not os.path.isfile(MODEL):
            sys.exit(f"The voice model isn't there: {MODEL}\nDownload it: python3 get_model.py (or set SCOUT_MODEL).")
        import onnxruntime as ort
        o = ort.SessionOptions()
        o.intra_op_num_threads = 2
        o.log_severity_level = 3
        _MODEL = ort.InferenceSession(MODEL, o, providers=["CPUExecutionProvider"])
    return _MODEL


def fbank(a):
    """The sound as 80 loudness bands every 10 ms (the Kaldi "fbank" the model was trained on)."""
    import numpy as np
    global _FB
    n, hop, nfft = 400, 160, 512
    if _FB is None:
        mel = lambda f: 1127 * np.log(1 + f / 700)
        c = np.linspace(mel(20), mel(SR / 2), 82)
        fm = mel(np.arange(nfft // 2 + 1) * SR / nfft)
        _FB = np.stack([np.clip(np.minimum((fm - c[i]) / (c[i + 1] - c[i]), (c[i + 2] - fm) / (c[i + 2] - c[i + 1])), 0, None)
                        for i in range(80)]).astype(np.float32)
    if len(a) < n:
        return np.zeros((0, 80), np.float32)
    idx = np.arange(0, len(a) - n + 1, hop)
    fr = a[idx[:, None] + np.arange(n)]
    fr = fr - fr.mean(1, keepdims=True)
    fr = np.concatenate([fr[:, :1] * 0.03, fr[:, 1:] - 0.97 * fr[:, :-1]], 1)
    fr = fr * (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / (n - 1))) ** 0.85
    p = np.abs(np.fft.rfft(fr, nfft, axis=1)) ** 2
    return np.log(np.maximum(p @ _FB.T, np.finfo(np.float32).eps)).astype(np.float32)


def voiceprint(a, run=None):
    """512 numbers that describe the voice (unit length), or None if there's under 2 seconds of sound. The model is
    fed 2-second pieces, a second apart, and the answers averaged: it was built for exactly that length, and longer
    pieces make it unreliable."""
    import numpy as np
    run = run or (lambda x: model().run(None, {"x": x})[0][0])
    f, w = fbank(a), 200
    if len(f) < w:
        return None
    vs = []
    for s in range(0, len(f) - w + 1, 100):
        v = run((f[s:s + w] - f[s:s + w].mean(0))[None])
        vs.append(v / np.linalg.norm(v))
    m = np.mean(vs, 0)
    return m / np.linalg.norm(m)


def voice_gap(p, q):
    """How different two voiceprints are: 0 the same, 1 unrelated."""
    return 1 - sum(x * y for x, y in zip(p, q))


def voice_match(p, known):
    """(the learned voice this one matches, gap) or (None, gap to the nearest). `known` = [{"name", "print", ...}]."""
    if p is None or not known:
        return None, None
    gap, best = min((voice_gap(p, k["print"]), i) for i, k in enumerate(known))
    return (known[best] if gap <= VOICE_MATCH else None), gap


def judge(voice, words):
    """(score 0-100, reasons) from the learned voice matched (or None). Not proof alone: YakTube adds this to its own
    score and only drops a Short when the total is high enough."""
    if not voice or (words is not None and words < MIN_WORDS):
        return 0, []
    channels = voice.get("channels", [])
    if len(channels) >= 2:
        return SCORE_SHARED, [f"farm voice ({len(channels)} channels)"]
    return SCORE_VOICE, [f"same voice as {voice['name']}"]


# ---------------------------------------------------------------- learned voices
def read_json(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path, d):
    tmp = path + ".new"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, path)


def load_voices(path=None):
    """Learned voices. Ones from before version 0.3 (pitch and tone, no voiceprint) can't be used and are skipped."""
    return [v for v in read_json(path or VOICES).get("voices", []) if len(v.get("print", [])) > 100]


def hidden(path=None):
    """Shared voices you've hidden here (with x in YakTube), by id."""
    return set(read_json(path or VOICES).get("hidden", []))


def good_print(p):
    return isinstance(p, list) and len(p) == 512 and all(isinstance(x, (int, float)) for x in p)


def shared_voices(path=None, hide=None):
    """Voices from the shared lists, as {"name", "channels", "examples", "print", "shared": True}, less hidden ones."""
    hide = hidden() if hide is None else hide
    return [dict(v, shared=True) for v in read_json(path or SHARED).get("voices", []) if v.get("name") not in hide]


def known_voices():
    return load_voices() + shared_voices()


def check_list(d, url=""):
    """The usable voices in a downloaded shared list (a dict as voices.json has it). Raises ValueError if it isn't one,
    or if it was made with another model (voiceprints from different models can't be compared)."""
    if not isinstance(d, dict) or d.get("format") != "yakscout-voices":
        raise ValueError("not a YakScout voice list")
    if d.get("model") != MODEL_ID:
        raise ValueError(f"made with another voice model ({d.get('model')}); this YakScout uses {MODEL_ID}")
    out = []
    for v in d.get("voices", [])[:5000]:
        if not (isinstance(v, dict) and isinstance(v.get("id"), str) and good_print(v.get("print"))):
            continue
        n = sum(x * x for x in v["print"]) ** 0.5 or 1
        out.append({"name": v["id"][:60], "list": url,
                    "channels": [str(c)[:100] for c in v.get("channels", []) if isinstance(c, str)][:100],
                    "examples": [x for x in v.get("examples", []) if isinstance(x, str) and VID_RE.fullmatch(x)][:50],
                    "added": str(v.get("added", ""))[:10], "print": [round(x / n, 5) for x in v["print"]]})
    return out


def refresh_lists(urls=None, fetch=None):
    """Download the shared voice lists and keep them in shared-voices.json. A list that can't be reached or read keeps
    what was downloaded before. Returns {url: "ok N" or the error}."""
    urls = (LISTS if urls is None else urls).split()
    if urls == ["off"]:
        urls = []
    fetch = fetch or (lambda u: urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": f"YakScout/{VERSION}"}),
                                                       timeout=20).read(5_000_001))
    old = read_json(SHARED)
    keep = {l["url"]: l for l in old.get("lists", []) if isinstance(l, dict) and l.get("url") in urls}
    voices = [v for v in old.get("voices", []) if v.get("list") in urls]
    result = {}
    for u in urls:
        try:
            raw = fetch(u)
            if len(raw) > 5_000_000:
                raise ValueError("too big")
            got = check_list(json.loads(raw), u)
            voices = [v for v in voices if v.get("list") != u] + got
            keep[u] = {"url": u, "count": len(got), "fetched": int(time.time()), "error": ""}
            result[u] = f"ok {len(got)}"
        except Exception as ex:
            keep.setdefault(u, {"url": u, "count": 0, "fetched": 0})["error"] = str(ex)[:120]
            result[u] = str(ex)[:120]
    write_json(SHARED, {"lists": list(keep.values()), "voices": voices})
    return result


def lists_loop():
    while True:
        try:
            for u, r in refresh_lists().items():
                print(f"[yakscout] shared list {u}: {r}", flush=True)
        except Exception as ex:
            print(f"[yakscout] shared lists: {ex}", flush=True)
        time.sleep(86400)


def learn_voice(name, vp, channel="", path=None, example=None):
    """Remember a voice from a Short known to be from a farm. If it's a voice already known, the channel (and the Short,
    as an example) is added to it instead (a voice on several channels counts for more). Returns (the voice's name,
    how many are known)."""
    path = path or VOICES
    voices = load_voices(path)
    same, _ = voice_match(vp, voices)
    if same:
        if channel and channel not in same.setdefault("channels", []):
            same["channels"].append(channel)
        name = same["name"]
    else:
        same = {"name": name, "learned": time.strftime("%Y-%m-%d"), "channels": [channel] if channel else [],
                "print": [round(float(x), 5) for x in vp]}
        voices.append(same)
    if example and example not in same.setdefault("examples", []):
        same["examples"].append(example)
    save_voices(voices, path)
    return name, len(voices)


def save_voices(voices, path=None):
    path = path or VOICES
    write_json(path, dict(read_json(path), voices=voices, model=MODEL_ID))


def forget_voice(name, path=None):
    """Stop recognising a voice: a learned one is removed; a shared one is hidden here (the list itself is untouched).
    Returns whether there was one by that name."""
    path = path or VOICES
    voices = load_voices(path)
    keep = [v for v in voices if v["name"] != name]
    if len(keep) != len(voices):
        save_voices(keep, path)
        return True
    if any(v["name"] == name for v in shared_voices(hide=set())):
        d = read_json(path)
        write_json(path, dict(d, hidden=sorted(set(d.get("hidden", [])) | {name})))
        return True
    return False


def share_entry(name, path=None):
    """A learned voice as a submission for a shared list (one file per voice, see the yakscout-voices repo), or None."""
    v = next((v for v in load_voices(path) if v["name"] == name), None)
    if not v:
        return None
    return {"id": re.sub(r"[^a-z0-9-]", "-", name.lower())[:40], "model": MODEL_ID, "added": time.strftime("%Y-%m-%d"),
            "channels": v.get("channels", [])[:20],                  # what a review needs; keeps Share's link short
            "examples": (v.get("examples", []) or ([name] if VID_RE.fullmatch(name) else []))[:10],
            "note": "", "print": v["print"]}


def add_voices(new, path=None):
    """Restore voices from a backup: each one is added, or (if it's a voice already known) its channels are merged
    into that one. Ones without a usable voiceprint are skipped. Returns how many were added or merged."""
    voices, n = load_voices(path), 0
    for v in new if isinstance(new, list) else []:
        if not (isinstance(v, dict) and isinstance(v.get("name"), str) and isinstance(v.get("print"), list)
                and len(v["print"]) == 512 and all(isinstance(x, (int, float)) for x in v["print"])):
            continue
        chans = [str(c)[:100] for c in v.get("channels", []) if isinstance(c, str)][:50]
        same = next((k for k in voices if k["name"] == v["name"]), None) or voice_match(v["print"], voices)[0]
        if same:
            same["channels"] = same.get("channels", []) + [c for c in chans if c not in same.get("channels", [])]
        else:
            voices.append({"name": v["name"][:60], "learned": str(v.get("learned", ""))[:10] or time.strftime("%Y-%m-%d"),
                           "channels": chans, "print": [round(float(x), 5) for x in v["print"]]})
        n += 1
    save_voices(voices, path)
    return n


class NoVoice(Exception):
    pass


def learn_short(vid):
    """Learn the voice of a Short known to be from a farm. (voice name, its channels, learned a new voice?). A voice
    that's already in a shared list isn't learned again."""
    sound, words, channel = hear(vid)
    vp = voiceprint(sound)
    if vp is None or (words is not None and words < MIN_WORDS):
        raise NoVoice("not enough talking at the start to learn a voice from")
    shared, _ = voice_match(vp, shared_voices())
    if shared:
        return shared["name"], shared.get("channels", []), False
    name, _ = learn_voice(vid, vp, channel, example=vid)
    v = next((v for v in load_voices() if v["name"] == name), {})
    return name, v.get("channels", []), name == vid


def verify_entry(entry):
    """For reviewing a shared-list submission: listen to each example Short and say how close its voice is to the
    submitted voiceprint. [(video id, channel, gap or error)]."""
    out = []
    for vid in entry.get("examples", []):
        try:
            sound, words, channel = hear(vid)
            vp = voiceprint(sound)
            out.append((vid, channel, None if vp is None else round(float(voice_gap(vp, entry["print"])), 3)))
        except Exception as ex:
            out.append((vid, "", str(ex)[:80]))
    return out


def note_hit(name, channel, vid, path=None):
    """A Short in the feed had a learned voice: add its channel and the Short to that voice, so a voice learned once
    gathers the 2 channels and 2 Shorts the shared list needs by itself. Returns True if the channel was new."""
    voices = load_voices(path)
    v = next((v for v in voices if v["name"] == name), None)
    if not v or not channel:
        return False
    chans, ex = v.setdefault("channels", []), v.setdefault("examples", [])
    new = channel not in chans and len(chans) < 50
    if new:
        chans.append(channel)
    if vid not in ex and len(ex) < 50:                   # up to 50 Shorts at home (Share sends 10 of them)
        ex.append(vid)
    elif not new:
        return False
    save_voices(voices, path)
    return new


def look(vid, voices=None):
    """Listen to the start of one Short. (score, reasons, facts line). A learned voice heard here is remembered with
    this Short and its channel (only for your own voices, and only when there's enough talking to judge)."""
    sound, words, channel = hear(vid)
    vp = voiceprint(sound)
    who, gap = voice_match(vp, known_voices() if voices is None else voices)
    score, why = judge(who, words)
    facts = ("no voice" if vp is None else f"nearest voice {gap:.2f}" if gap is not None else "voice") + \
            ("" if words is None else f" | {words} words")
    if voices is None and score and who and not who.get("shared") and note_hit(who["name"], channel, vid):
        facts += f" | new channel {channel}"
    return score, why, facts


# ---------------------------------------------------------------- waiting for YakTube
LOOKING = threading.Lock()                                    # one Short at a time
STARTED = time.time()


def status():
    """What YakTube shows in Settings > YakScout."""
    def when(v):
        try:
            return time.strftime("%b %-d", time.strptime(v.get("learned", ""), "%Y-%m-%d"))
        except ValueError:
            return ""
    lists = [{k: l.get(k) for k in ("url", "count", "fetched", "error")} for l in read_json(SHARED).get("lists", [])]
    return {"scout": VERSION, "busy": LOOKING.locked(), "up": int(time.time() - STARTED), "lists": lists,
            "voices": [{"name": v["name"], "learned": when(v), "channels": len(v.get("channels", [])),
                        "examples": len(v.get("examples", [])) or int(bool(VID_RE.fullmatch(v["name"]))),   # as Share counts them
                        "channel_names": v.get("channels", [])[:20], "source": "learned"} for v in load_voices()] +
                      [{"name": v["name"], "learned": "", "channels": len(v.get("channels", [])),
                        "channel_names": v.get("channels", [])[:20], "source": "shared"} for v in shared_voices()]}


class Handler(BaseHTTPRequestHandler):
    """GET /status; POST /look {"id"} -> {"score": 0-100, "why": [...], "facts"}; POST /learn {"id"} -> {"name",
    "channels", "new"}; POST /forget {"name"} -> {"forgot": true/false}; GET /voices and POST /voices {"voices": [...]}
    (YakTube's backup and restore); POST /verify {"entry": voice file} -> {"examples": [{"id", "channel", "gap"}]}
    (YakTube's Submissions list)."""
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
        if self.path.split("?")[0] == "/voices":                 # everything learned, voiceprints too (YakTube's backup)
            return self.reply(200, {"voices": load_voices()})
        if self.path.split("?")[0] == "/share":                  # a learned voice as a submission for a shared list
            name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("name", [""])[0]
            entry = share_entry(name)
            return self.reply(200, entry) if entry else self.reply(404, {"error": "no learned voice by that name"})
        self.reply(404, {"error": "not here"})

    def do_POST(self):
        if self.path not in ("/look", "/learn", "/forget", "/voices", "/verify"):
            return self.reply(404, {"error": "not here"})
        cap = {"/voices": 4_000_000, "/verify": 200_000}.get(self.path, 1000)
        try:
            body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), cap)) or b"{}")
            body = body if isinstance(body, dict) else {}
        except ValueError:
            body = {}
        if self.path == "/verify":                               # a submitted voice: do its examples have that voice?
            entry = body.get("entry")
            if not (isinstance(entry, dict) and good_print(entry.get("print"))):
                return self.reply(400, {"error": "need a voice file"})
            entry = dict(entry, examples=[x for x in entry.get("examples", []) if isinstance(x, str) and VID_RE.fullmatch(x)][:10])
            with LOOKING:
                got = verify_entry(entry)
            return self.reply(200, {"examples": [{"id": v, "channel": ch, "gap": g if isinstance(g, float) else None,
                                                  "error": "" if isinstance(g, float) else (g or "no talking at the start")}
                                                 for v, ch, g in got], "limit": VOICE_MATCH})
        if self.path == "/voices":                               # restore from YakTube's backup
            with LOOKING:
                n = add_voices(body.get("voices"))
            print(f"[yakscout] restored {n} voice(s) from a backup", flush=True)
            return self.reply(200, {"added": n, "voices": len(load_voices())})
        if self.path == "/forget":
            name = body.get("name")
            if not isinstance(name, str) or not name:
                return self.reply(400, {"error": "need a name"})
            with LOOKING:
                gone = forget_voice(name)
            print(f"[yakscout] forgot voice {name}" if gone else f"[yakscout] no voice called {name}", flush=True)
            return self.reply(200, {"forgot": gone})
        vid = body.get("id", "")
        if not (isinstance(vid, str) and VID_RE.fullmatch(vid)):
            return self.reply(400, {"error": "need an id"})
        if self.path == "/learn":
            with LOOKING:
                try:
                    name, channels, new = learn_short(vid)
                except NoVoice as ex:
                    return self.reply(422, {"error": str(ex)})
                except Exception as ex:
                    return self.reply(502, {"error": str(ex)[:200]})
            print(f"[yakscout] learned {vid}: {'new voice' if new else 'same voice as ' + name} ({', '.join(channels)})", flush=True)
            return self.reply(200, {"name": name, "channels": channels, "new": new})
        with LOOKING:
            t = time.time()
            try:
                score, why, facts = look(vid)
            except Exception as ex:                             # couldn't fetch or read it
                print(f"[yakscout] {vid}: couldn't listen ({ex})", flush=True)
                return self.reply(502, {"error": str(ex)[:200]})
        print(f"[yakscout] {vid} {score:3d}  {'; '.join(why) or '-'}  [{facts} | {time.time() - t:.1f}s]", flush=True)
        self.reply(200, {"score": score, "why": why, "facts": facts})

    def log_message(self, fmt, *args):
        pass


def serve(host, port):
    model()                                                   # say now if the model is missing, not at the first Short
    threading.Thread(target=lists_loop, daemon=True).start()  # shared voice lists: now, then once a day
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"YakScout {VERSION} waiting for YakTube on port {port}. In YakTube: Settings > YakScout, this computer's address.",
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
    ap = argparse.ArgumentParser(description="Tell YakTube which Shorts come from content farms, by their voices.")
    ap.add_argument("what", nargs="*", help="'check <link>...', 'learn <link>...', 'share <voice>', 'verify <file>' "
                                            "or 'lists' (leave out to wait for YakTube)")
    ap.add_argument("--port", type=int, default=PORT, help=f"port YakTube connects to (default {PORT})")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all, so YakTube can reach it)")
    a = ap.parse_args()
    cmd, args = (a.what[0], a.what[1:]) if a.what else ("serve", [])
    if cmd not in ("serve", "check", "learn", "share", "verify", "lists"):
        ap.error("did you mean: scout.py check <link or id>, scout.py learn <link or id>, scout.py share <voice name>, "
                 "scout.py verify <voice file> or scout.py lists?")
    if cmd == "serve":
        return serve(a.host, a.port)
    if cmd == "lists":                                        # download the shared voice lists now
        for u, r in refresh_lists().items():
            print(f"{u}: {r}")
        return
    if cmd == "share":                                        # a learned voice, as a file to send to a shared list
        for name in args:
            entry = share_entry(name)
            if not entry:
                print(f"{name}: no learned voice by that name (see YakTube's Voices list)")
                continue
            out = f"voice-{entry['id']}.json"
            with open(out, "w") as f:
                json.dump(entry, f, indent=1)
            print(f"{name}: wrote {out} ({len(entry['channels'])} channels, {len(entry['examples'])} examples)")
        return
    if cmd == "verify":                                       # check a submission: do its examples have that voice?
        for path in args:
            entry = read_json(path)
            if not good_print(entry.get("print")):
                print(f"{path}: not a voice file")
                continue
            print(f"{path}: {entry.get('id')}")
            for vid, channel, gap in verify_entry(entry):
                if isinstance(gap, float):
                    print(f"  {vid} {channel[:24]:24s} {gap + 0.0:.3f} {'same voice' if gap <= VOICE_MATCH else 'DIFFERENT'}")
                else:
                    print(f"  {vid} {channel[:24]:24s} couldn't check: {gap or 'no talking at the start'}")
        return
    for s in args:
        vid = video_id(s)
        if not vid:
            print(f"{s}: not a YouTube link or id")
            continue
        try:
            if cmd == "check":
                score, why, facts = look(vid)
                print(f"{vid} {score:3d}  {'; '.join(why) or '-'}  [{facts}]")
                continue
            name, channels, new = learn_short(vid)
            print(f"{vid}: " + ("learned a new voice" if new else f"same voice as {name}") +
                  (f", on {', '.join(channels)}" if channels else "") + f" ({VOICES})")
        except NoVoice as ex:
            print(f"{vid}: {ex}")
        except Exception as ex:
            print(f"{vid}: couldn't listen ({ex})")


if __name__ == "__main__":
    main()

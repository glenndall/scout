# YakScout

An optional companion for [YakTube](https://github.com/glenndall/yaktube) that keeps content-farm Shorts out of its
Shorts list by **listening** to them: the faceless channels that churn out Shorts from one formula to farm views and ad
money. Being made with AI isn't what it looks for (people make clever, funny things with AI); the formula is, above all
the same synthetic voice on channel after channel. YakTube already reads each Short's title, description and captions;
Scout adds what it hears. YakTube works the same without it.

Run it on any computer YakTube can reach: a Mac, an Ubuntu PC, or the NAS. It waits for YakTube to connect; you tell
YakTube where it is in **Settings → Scout**. When it's off (the Mac asleep), YakTube just goes on with its own check,
and Scout catches up when it's back.

## How it listens
For each Short, Scout fetches only the first 15 seconds of its sound (about 80 KB, straight from YouTube, the original
voice, never one of YouTube's AI dubs), and:
1. **Checks someone is talking.** YouTube's automatic captions must hear at least 15 words in those seconds; music on
   its own has a "voice" too, and Scout leaves it alone.
2. **Takes a voiceprint** with a speaker-recognition model (CAM++, from 3D-Speaker, trained to tell people's voices
   apart through noise and music), fed 2-second pieces and averaged.
3. **Compares it** with the voices it has learned from Shorts known to be from farms.

About 2 seconds a Short on a 2020 MacBook (half of it waiting for YouTube). yt-dlp is only a backup for when
YouTube won't hand the sound over directly.

| Scout heard | Score |
|---|---|
| A learned voice | +35 |
| A learned voice that's on 2 or more channels (a farm's tell) | +50 |

YakTube adds this to its own score and drops the Short once the total reaches 50 (`YAK_SCOUT_MIN`). So a voice heard on
several farm channels drops a Short by itself; a voice from one channel needs one more clue.

**How sure is it?** Tested on 131 Shorts (Nov 2026). Farm Shorts sharing a voice came out 0.05-0.20 apart; every
other Short was 0.73 or more from them, against a match limit of 0.25. Learning 7 farm Shorts caught 6 of the 8 other
farm Shorts in the set and 4 more farm Shorts found among the rest, and none of 37 ordinary Shorts. Still a small
sample: check the log, and if a voice catches the wrong Shorts, take it out of `voices.json`.

## Set it up
It needs Python 3.9+, ffmpeg, numpy, onnxruntime, and the voice model (29 MB).

**Mac:**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p models && curl -L -o models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx \
  https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx
```
Optional backup: `curl -L -o .venv/bin/yt-dlp https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos && chmod +x .venv/bin/yt-dlp`.

**Ubuntu:**
```bash
sudo apt install python3 python3-venv ffmpeg
python3 -m venv .venv
.venv/bin/pip install numpy onnxruntime yt-dlp
```
then the same `models/` download as above. If the firewall is on, let YakTube in: `sudo ufw allow 8095/tcp`.

## Use it
```bash
.venv/bin/python scout.py                                      # wait for YakTube on port 8095
.venv/bin/python scout.py learn https://youtube.com/shorts/... # this one is from a farm: remember its voice
.venv/bin/python scout.py check https://youtube.com/shorts/... # what Scout makes of one (YakTube isn't told)
```
Then in YakTube: **Settings → Scout**, type this computer's address (e.g. `192.168.1.31`) and Save. The page shows
**Connected** once YakTube reaches it. `--port` (or `SCOUT_PORT`) changes the port; change it in YakTube too.

Learning a Short whose voice Scout already knows adds its channel to that voice instead of making a new one; that's
how a voice gets to "2 or more channels". Learned voices show in YakTube's Scout page; they're kept in `voices.json`
next to `scout.py` (`SCOUT_VOICES` to keep them elsewhere; `SCOUT_MODEL` if the model lives somewhere else). Voices
learned by Scout 0.2 (pitch and tone) can't be used any more: learn them again.

## How YakTube talks to it
- `GET /status`: `{"scout": version, "busy": true/false, "voices": [{"name", "learned", "channels"}]}`
- `POST /look` with `{"id": "<video id>"}`: listens to that Short and answers `{"score": 0-100, "why": [...], "facts": "..."}`,
  or `502 {"error": ...}` if it couldn't fetch it. One at a time.

Neither has a login, so keep both on your home network, never open to the internet (see the YakTube README).

## Tests
`.venv/bin/python test_scout.py` (no network or downloads; the model test runs only if the model is there).

## Credits
The voice model is CAM++ from Alibaba's [3D-Speaker](https://github.com/modelscope/3D-Speaker) (Apache 2.0), in the
ONNX form published by [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

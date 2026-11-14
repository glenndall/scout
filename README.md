# YakScout

## 1. Overview & Purpose

- **What it is:** an optional companion service for [YakTube](https://github.com/glenndall/yaktube) that finds and
  filters out content-farm Shorts: the faceless channels that churn out Shorts from one formula, with the same
  synthetic voice, to farm views and ad revenue. Being made with AI isn't what it looks for (people make clever, funny
  things with AI); the repeated formula is.
- **How it works:** YakTube reviews each Short's title, description and captions; YakScout listens to its audio and
  adds what it hears to YakTube's score.
- **Flexibility:** YakScout runs on its own, in Docker (on a NAS or server) or directly on a Mac or Ubuntu machine.
  YakTube works normally while YakScout is offline, and YakScout catches up when it's back.

## 2. How the Audio Analysis Works

For each Short, YakScout fetches only the first 15 seconds of the original audio (about 80 KB, never one of YouTube's
AI-dubbed tracks) and runs three steps:

1. **Speech check:** someone must actually be talking (at least 15 words in YouTube's automatic captions for those
   seconds), so music on its own is left alone.
2. **Voiceprint:** a speaker-recognition model (CAM++ from 3D-Speaker) turns the voice into a voiceprint, fed in
   2-second pieces and averaged.
3. **Comparison:** the voiceprint is compared with the farm voices YakScout has learned.

About 2 seconds a Short on a 2020 MacBook, about 4 on a Synology DS220+. yt-dlp is only a backup for when YouTube
won't hand the audio over directly.

**Scoring & thresholds**

| YakScout heard | Points |
|---|---|
| A learned farm voice | +35 |
| A learned farm voice heard on 2 or more channels | +50 (drops a Short by itself) |

YakTube adds these to its own score and drops a Short once the total reaches 50 (`YAK_SCOUT_MIN`, or **Drop at** in
YakTube's settings).

**How sure is it?** Tested on 131 Shorts (Nov 2026): farm Shorts sharing a voice came out 0.05-0.20 apart, every other
Short 0.73 or more from them, against a match limit of 0.25. Learning 7 farm Shorts caught 6 of the 8 other farm
Shorts in the set and 4 more found among the rest, and none of 37 ordinary Shorts. Still a small sample: check the
log, and if a voice catches the wrong Shorts, forget it.

## 3. Installation & Deployment Options

**Docker (Synology NAS or any server)**
1. Copy `Dockerfile`, `docker-compose.yml`, `scout.py`, `get_model.py` and `voices.json` into a folder of their own,
   e.g. `/volume1/NAS/yakscout`.
2. Inside it, create an empty folder named `data` (File Station → Create folder). Container Manager won't start the
   project without it; learned voices live there.
3. Synology: Container Manager → Project → Create → pick the folder → it finds `docker-compose.yml` → Next → Done.
   Elsewhere: `docker compose up -d --build` in the folder. The first start builds the image, which takes a few
   minutes (it downloads Python's libraries and the 29 MB voice model). The container shows **Healthy** once
   YakScout answers on port `8095`.

YakScout and YakTube can start or restart in any order: until YakScout answers, YakTube shows **Can't reach** and
tries again every minute. To update YakScout, replace `scout.py` and restart the project; rebuild (Action → Build)
only for a new `Dockerfile` or to bring yt-dlp up to date.

**macOS (standalone)**
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python get_model.py
```
Optional backup downloader: `curl -L -o .venv/bin/yt-dlp https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos && chmod +x .venv/bin/yt-dlp`

**Ubuntu (standalone, without Docker)**
```bash
sudo apt install python3 python3-venv ffmpeg
python3 -m venv .venv
.venv/bin/pip install numpy onnxruntime yt-dlp
.venv/bin/python get_model.py
```
If the firewall is on, let YakTube in: `sudo ufw allow 8095/tcp`.

## 4. Usage & Teaching the System

**Core commands**
```bash
.venv/bin/python scout.py                                      # listen for YakTube on port 8095
.venv/bin/python scout.py learn https://youtube.com/shorts/... # this one is from a farm: learn its voice
.venv/bin/python scout.py check https://youtube.com/shorts/... # what YakScout makes of one (YakTube isn't told)
```
In Docker: Container Manager → Container → yakscout → Terminal → Create → `bash`, then `python scout.py learn <link>`
(or `sudo docker exec yakscout python scout.py learn <link>` over SSH).

- **Training:** teach YakScout a farm voice from the terminal (`learn`) or, easier, from YakTube: **Settings →
  YakScout → Voices**, paste a link and **Learn**. Teaching a voice it already knows adds that channel to it instead
  of making a duplicate; that's how a voice reaches "2 or more channels".
- **Data storage:** learned voices are kept in `data/voices.json` (Docker) or `voices.json` next to `scout.py`, and
  survive updates and rebuilds. `SCOUT_VOICES`, `SCOUT_MODEL` and `SCOUT_PORT` (or `--port`) move them, the model or
  the port.

## 5. YakTube Integration & Management

In YakTube, open **Settings → YakScout** (under Plugins), enter YakScout's address (the NAS's own address, e.g.
`192.168.1.20`) and port `8095`, and Save. The badge turns **Connected**, and the line under the address shows the
plugin version, seconds per Short, the last scan and uptime. The counts at the top refresh by themselves.

**How a Short is judged.** Each new Short gets a score from 0 to 100: **Titles & captions** (built into YakTube)
adds points for farm clues in what it says about itself, **Scan voices** (YakScout) adds points for a farm voice. At
**Drop at** (50 to start) the Short leaves the Shorts list. Two lists overrule the score: **Never drop** always wins,
then **AI-Farm channels**.

**Scoring & rules**
- **Dropped:** what was taken out, and why (*Story*, *Voice*, *Channel*, *Pasted*, *Text*). **Bring back** returns
  one for good.
- **AI-Farm channels:** every Short from these is dropped unchecked. YakScout adds a channel itself once it has judged
  3 or more of its Shorts and dropped over half; you can add one by name. **×** takes one off.
- **Never drop:** channels that are always kept, whatever they score. Adding one brings its dropped Shorts back.
- **Voices:** the learned farm voices and the channels each was heard on. **Learn** from a link; **×** forgets one.
- **Advanced:** **Drop at** (30-90; lower catches more and risks more mistakes) and **Remember for** (30, 60 or 90
  days, after which a Short is judged again; ones you brought back are always remembered).

**Logs & backups**
- **Log:** the last 100 verdicts, *Read* (titles & captions) or *Scanned* (YakScout listened too), with score and
  reasons. **Download CSV** saves every verdict still remembered, for Numbers or Excel.
- **Backup:** a `.zip` with `backup.json` (channel lists, Shorts you brought back, Advanced settings and, while
  YakScout is connected, its learned voiceprints) and `log.csv`.
- **Restore:** pick the `.zip` (or the `backup.json` inside it). It adds to what's there and removes nothing; voices
  go back only while YakScout is connected.

## 6. API Reference (For Integrations)

YakScout answers plain HTTP with JSON on port `8095`:

- `GET /status`: version, whether it's busy, uptime in seconds, and the learned voices (name, date learned, channels).
- `POST /look` `{"id": "<video id>"}`: scores one Short: `{"score": 0-100, "why": [...], "facts": "..."}`, or `502`
  if it couldn't fetch it. One at a time.
- `POST /learn` `{"id": ...}`: learns that Short's voice: `{"name", "channels", "new"}`, or `422` if there's too
  little talking.
- `POST /forget` `{"name": ...}`: removes a learned voice.
- `GET /voices` / `POST /voices` `{"voices": [...]}`: all learned voices with their voiceprints, and adding them back
  (merged with what's there). YakTube's Backup and Restore use these.

**Note:** there's no login. Keep YakScout (and YakTube) on your home network, never open to the internet.

## Tests

`.venv/bin/python test_scout.py` (no network or downloads; the model test runs only if the model is there).

## Credits

The voice model is CAM++ from Alibaba's [3D-Speaker](https://github.com/modelscope/3D-Speaker) (Apache 2.0), in the
ONNX form published by [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx).

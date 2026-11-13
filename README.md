# Scout

An optional companion for [YakTube](https://github.com/glenndall/yaktube) that keeps content-farm Shorts out of its
Shorts list by **watching** them: the faceless channels that churn out Shorts from one formula to farm views and ad
money. Being made with AI isn't what it looks for (people make clever, funny things with AI); the formula is.
YakTube already reads each Short's title, description and captions; Scout downloads the video and adds what it sees
and hears. YakTube works the same without it.

Run it on any computer YakTube can reach that has a bit of power: a Mac, an old PC with Ubuntu. Not the NAS.
It waits for YakTube to connect; you tell YakTube where it is in **Settings → Scout**. When it's off (the Mac asleep),
YakTube just goes on with its own check, and Scout catches up when it's back.

## What it looks for
- **The voice.** Farms reuse the same text-to-speech voice in every Short, across many channels. Tell Scout about one
  you know is from a farm (`learn`) and it recognises that voice in other Shorts by its pitch and tone. +35.
- **Word-by-word captions.** One bold yellow word at a time in the middle of the screen. People use them too, so
  it's only a clue. +15.

YakTube adds these to its own score, and drops the Short once the total reaches 50 (`YAK_SCOUT_MIN`).
So a learned voice and the captions together drop it, and either one plus one of YakTube's own clues does.

**How sure is it?** Tested on 11 Shorts: the voice learned from one confirmed farm Short matched two other farm
channels and none of the human ones. That's a small sample, and the pitch does most of the work. Someone with a
similar voice could be matched by mistake. Check the log, and if a voice catches the wrong Shorts, take it out of
`voices.json`.

## Set it up
**Mac:**
```bash
python3 -m venv .venv
.venv/bin/pip install imageio-ffmpeg
curl -L -o .venv/bin/yt-dlp https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos
chmod +x .venv/bin/yt-dlp
```
(The Mac's own Python is 3.9; `pip install yt-dlp` only gets an old version there that YouTube now refuses, so this
uses yt-dlp's standalone Mac build. `.venv/bin/yt-dlp -U` updates it.)

**Ubuntu:**
```bash
sudo apt install python3 python3-venv ffmpeg
python3 -m venv .venv
.venv/bin/pip install yt-dlp
```
If the firewall is on, let YakTube in: `sudo ufw allow 8095/tcp`. To keep Scout running after reboots, a systemd
service that runs `.venv/bin/python scout.py` from this folder.

Keep yt-dlp up to date (`.venv/bin/yt-dlp -U` or `.venv/bin/pip install -U yt-dlp`): YouTube changes often, and an old
yt-dlp stops being able to download.

## Use it
```bash
.venv/bin/python scout.py                                      # wait for YakTube on port 8095
.venv/bin/python scout.py learn https://youtube.com/shorts/... # this one is from a farm: remember its voice
.venv/bin/python scout.py check https://youtube.com/shorts/... # what Scout makes of one (YakTube isn't told)
```
Then in YakTube: **Settings → Scout**, type this computer's address (e.g. `192.168.1.31`) and Save. The page shows
**Connected** once YakTube reaches it. `--port` (or `SCOUT_PORT`) changes the port; change it in YakTube too.
Each Short takes about half a minute, most of it the download. Learned voices show in YakTube's Scout page; they're
kept in `voices.json` next to `scout.py` (`SCOUT_VOICES` to keep them elsewhere).

## How YakTube talks to it
- `GET /status`: `{"scout": version, "busy": true/false, "voices": [{"name", "pitch", "learned"}]}`
- `POST /look` with `{"id": "<video id>"}`: watches that Short and answers `{"score": 0-100, "why": [...], "facts": "..."}`,
  or `502 {"error": ...}` if it couldn't download it. One at a time.

Neither has a login, so keep both on your home network, never open to the internet (see the YakTube README).

## Tests
`python3 test_scout.py` (no network, downloads or ffmpeg needed).

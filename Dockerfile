# YakScout: what it needs to run (Python, numpy, onnxruntime, ffmpeg, the voice model). scout.py itself comes from the
# folder (see docker-compose.yml), so updating YakScout = replace scout.py, then Restart. Rebuild only for a new
# Dockerfile or to refresh yt-dlp (the backup downloader).
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir numpy==2.1.3 onnxruntime==1.20.1 imageio-ffmpeg==0.6.0 yt-dlp

COPY get_model.py ./
RUN python get_model.py /app/models

COPY scout.py voices.json ./
ENV PYTHONUNBUFFERED=1 SCOUT_VOICES=/data/voices.json
EXPOSE 8095
HEALTHCHECK --interval=1m --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/status', timeout=4)"

# the first time, start from the voices that come with YakScout; after that they live in /data (your ./data folder)
CMD ["sh", "-c", "mkdir -p /data && ([ -f /data/voices.json ] || cp /app/voices.json /data/voices.json) && exec python scout.py"]

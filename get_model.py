#!/usr/bin/env python3
"""Download Scout's voice model (29 MB) into models/, and check it's the right file.

    python3 get_model.py

The model is CAM++ from Alibaba's 3D-Speaker (Apache 2.0), as published by the sherpa-onnx project."""
import hashlib, os, sys, urllib.request

NAME = "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/" + NAME
# Two exports of the same model are accepted: the original (PyTorch 2.4.1) and the one sherpa-onnx re-uploaded on
# 2026-11-19. Scout's voiceprints come out identical from both (it always feeds 200-frame windows).
SHA256 = {"357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b",
          "98daf9bcf1c8abf3db374fec4c27a5eda7c58705bb3f180d057b085a72a88bb5"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(folder=None):
    folder = folder or os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, NAME)
    if os.path.isfile(path) and sha256(path) in SHA256:
        print(f"Already there: {path}")
        return path
    print(f"Downloading the voice model (29 MB) to {path} ...", flush=True)
    tmp = path + ".part"
    urllib.request.urlretrieve(URL, tmp)
    if sha256(tmp) not in SHA256:
        os.remove(tmp)
        sys.exit("The download isn't the expected file (checksum differs). Nothing was saved; try again later.")
    os.replace(tmp, path)
    print("Done.")
    return path


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)

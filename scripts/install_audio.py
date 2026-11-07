"""Install local audio dependencies and public, account-free models."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = (
    ("Systran/faster-whisper-large-v3", ".asr_models/large-v3"),
    ("Systran/faster-whisper-tiny", ".asr_models/tiny-langid"),
    ("globalizator/gzwhisper-kazakh-ct2-int8", ".asr_models/kazakh-turbo-ct2"),
    ("speechbrain/spkrec-ecapa-voxceleb", ".diarization_models/speechbrain-ecapa"),
    ("deepvk/kazRush-kk-ru", ".translation_models/kazrush-kk-ru"),
    ("facebook/m2m100_418M", ".translation_models/m2m100_418M"),
)


def main() -> None:
    subprocess.run([sys.executable, "-m", "pip", "install", "-r",
                    str(ROOT / "requirements-audio.txt")], check=True)
    from huggingface_hub import snapshot_download

    for repo, relative in MODELS:
        folder = ROOT / relative
        print(f"Downloading {repo} -> {folder}", flush=True)
        snapshot_download(repo_id=repo, local_dir=folder)
    print("Local audio mode installed", flush=True)


if __name__ == "__main__":
    main()

"""Extract a single-channel 16 kHz MP3 audio track from a video using ffmpeg.

Recommended for upload to Volcengine ASR: small (~480 KB/min) yet preserves
voice intelligibility.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class AudioExtractor:
    def __init__(self, sample_rate: int = 16000, channels: int = 1, bitrate: str = "64k") -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg not found on PATH")
        self.sample_rate = sample_rate
        self.channels = channels
        self.bitrate = bitrate

    def extract(self, video_path: Path, out_path: Path) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_path),
            "-vn",
            "-ac", str(self.channels),
            "-ar", str(self.sample_rate),
            "-b:a", self.bitrate,
            "-f", "mp3",
            str(out_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr.strip()[:500]}")
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced empty output: {out_path}")
        return out_path

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_checkpoint_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("SHENGMU_CHECKPOINT_DIR", str(tmp_path / "checkpoints"))


@pytest.fixture
def media(tmp_path: Path) -> Path:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for media integration tests")
    path = tmp_path / "测试 file ' with spaces.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=3",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s16le",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=zho",
            str(path),
        ],
        check=True,
    )
    return path

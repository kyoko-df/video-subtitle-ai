from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Event

from .models import Cancelled, SubtitleError


def binary(name: str) -> str:
    candidate = os.environ.get(f"{name.upper()}_BINARY", name)
    resolved = shutil.which(candidate)
    if not resolved:
        raise SubtitleError(
            f"找不到 {name}。请安装 FFmpeg 并加入 PATH，或设置 {name.upper()}_BINARY。"
        )
    return resolved


def check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("任务已取消。")


def run_process(args: list[str], cancel: Event | None = None, timeout: float = 3600) -> str:
    check_cancel(cancel)
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except OSError as exc:
        raise SubtitleError("无法启动媒体处理工具。请检查 FFmpeg 安装。") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_cancel(cancel)
            if time.monotonic() > deadline:
                raise SubtitleError("媒体处理超时。")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1500:]
            raise SubtitleError(f"媒体处理失败：{detail}")
        return stdout.decode("utf-8", errors="replace")
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@dataclass(frozen=True)
class AudioTrack:
    index: int
    codec: str
    channels: int
    language: str
    title: str
    offset: float = 0.0


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    audio_tracks: list[AudioTrack]

    def to_dict(self) -> dict:
        return asdict(self)


def probe(path: Path, cancel: Event | None = None) -> MediaInfo:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise SubtitleError(f"文件不存在：{path}")
    raw = run_process(
        [
            binary("ffprobe"),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ],
        cancel,
        timeout=60,
    )
    try:
        data = json.loads(raw)
        streams = data.get("streams", [])
        durations = [
            float(s["duration"]) for s in streams if s.get("duration") not in (None, "N/A")
        ]
        duration = float(data.get("format", {}).get("duration") or max(durations, default=0))
        origin = float(data.get("format", {}).get("start_time", 0) or 0)
        tracks = [
            AudioTrack(
                int(s["index"]),
                s.get("codec_name", "unknown"),
                int(s.get("channels", 0)),
                s.get("tags", {}).get("language", "und"),
                s.get("tags", {}).get("title", ""),
                max(0.0, float(s.get("start_time", origin) or origin) - origin),
            )
            for s in streams
            if s.get("codec_type") == "audio"
        ]
    except (ValueError, KeyError, TypeError) as exc:
        raise SubtitleError("无法读取媒体信息。") from exc
    if not tracks:
        raise SubtitleError("该文件没有音轨，无法生成语音字幕。")
    if not math.isfinite(duration) or duration <= 0:
        raise SubtitleError("无法确定媒体时长；请先将文件转换成常规视频或音频格式。")
    return MediaInfo(duration, tracks)


def extract_audio(
    source: Path, target: Path, track_index: int, cancel: Event | None = None
) -> None:
    # Argument arrays avoid shell interpretation of spaces, quotes, or user filenames.
    run_process(
        [
            binary("ffmpeg"),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source.resolve()),
            "-map",
            f"0:{track_index}",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(target),
        ],
        cancel,
    )

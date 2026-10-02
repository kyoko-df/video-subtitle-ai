from __future__ import annotations

import html
import json
import os
import tempfile
from pathlib import Path

from .models import SubtitleError, Transcript

FORMATS = ("srt", "vtt", "ass", "txt", "json")


def timestamp_ticks(ticks: int, rate: int, separator: str) -> str:
    seconds, fraction = divmod(ticks, rate)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    width = 3 if rate == 1000 else 2
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{fraction:0{width}d}"


def times(start: float, end: float, rate: int, separator: str) -> tuple[str, str]:
    a = round(start * rate)
    b = max(a + 1, round(end * rate))
    return timestamp_ticks(a, rate, separator), timestamp_ticks(b, rate, separator)


def render(transcript: Transcript, fmt: str) -> str:
    if fmt not in FORMATS:
        raise SubtitleError(f"不支持的字幕格式：{fmt}")
    if fmt == "json":
        return (
            json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    if fmt == "txt":
        return "\n".join(segment.text for segment in transcript.segments) + "\n"
    if fmt in ("srt", "vtt"):
        blocks = []
        for index, segment in enumerate(transcript.segments, 1):
            start, end = times(segment.start, segment.end, 1000, "," if fmt == "srt" else ".")
            text = html.escape(segment.text, quote=False) if fmt == "vtt" else segment.text
            blocks.append(f"{index}\n{start} --> {end}\n{text}\n")
        return ("WEBVTT\n\n" if fmt == "vtt" else "") + "\n".join(blocks)
    header = """[Script Info]
Title: Shengmu subtitles
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,54,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for segment in transcript.segments:
        start, end = times(segment.start, segment.end, 100, ".")
        # Full-width braces prevent transcription text from injecting ASS style overrides.
        text = (
            segment.text.replace("\\", "＼")
            .replace("{", "｛")
            .replace("}", "｝")
            .replace("\n", r"\N")
        )
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}")
    return header + "\n".join(lines) + "\n"


def output_paths(
    directory: Path,
    stem: str,
    formats: list[str] | tuple[str, ...],
    overwrite: bool = False,
    protected: Path | None = None,
) -> list[Path]:
    formats = list(dict.fromkeys(formats))
    if not formats or any(fmt not in FORMATS for fmt in formats):
        raise SubtitleError("请至少选择一种有效字幕格式。")
    if not stem or stem in (".", "..") or any(char in stem for char in "/\\\x00"):
        raise SubtitleError("输出文件名无效。")
    directory = directory.expanduser().resolve()
    paths = [directory / f"{stem}.{fmt}" for fmt in formats]
    for path in paths:
        if protected and path.resolve() == protected.resolve():
            raise SubtitleError("输出路径与输入文件相同，请更换输出目录或文件名。")
        if path.exists() and (not overwrite or not path.is_file()):
            raise SubtitleError(f"输出已存在：{path}。使用 --overwrite 允许覆盖文件。")
    return paths


def export_files(
    transcript: Transcript,
    directory: Path,
    stem: str,
    formats: list[str] | tuple[str, ...],
    overwrite: bool = False,
    protected: Path | None = None,
) -> list[Path]:
    formats = list(dict.fromkeys(formats))
    paths = output_paths(directory, stem, formats, overwrite, protected)
    directory = paths[0].parent
    # Render everything before writing; an invalid transcript cannot leave a partial batch.
    content = [render(transcript, fmt) for fmt in formats]
    directory.mkdir(parents=True, exist_ok=True)
    pending: list[tuple[Path, Path]] = []
    try:
        for path, text in zip(paths, content, strict=True):
            fd, temp = tempfile.mkstemp(prefix=".shengmu-", dir=directory)
            temp_path = Path(temp)
            pending.append((temp_path, path))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
        for temp_path, path in pending:
            if overwrite:
                os.replace(temp_path, path)
            else:
                # link is atomic and refuses to overwrite even if another writer wins a race.
                try:
                    os.link(temp_path, path)
                except FileExistsError as exc:
                    raise SubtitleError(f"输出已存在：{path}") from exc
                temp_path.unlink()
    finally:
        for temp_path, _ in pending:
            temp_path.unlink(missing_ok=True)
    return paths

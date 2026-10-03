from __future__ import annotations

import errno
import html
import json
import os
import tempfile
from pathlib import Path

from .diagnostics import ensure_output_directory
from .models import SubtitleError, Transcript
from .storage import copy_exclusive

FORMATS = ("srt", "vtt", "ass", "txt", "json")
LINK_UNSUPPORTED = {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.EXDEV, errno.ENOSYS}


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


def display_text(segment, metadata):
    mode = metadata.get("export_mode", "original")
    if mode in {"translated", "bilingual"} and not segment.translation:
        raise SubtitleError("存在未翻译字幕，请先翻译或补齐译文。")
    text = (
        segment.text
        if mode == "original"
        else segment.translation
        if mode == "translated"
        else segment.text + "\n" + segment.translation
    )
    if metadata.get("speaker_labels") and segment.speaker:
        text = segment.speaker + ": " + text
    return text


def ass_text(text):
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", r"\N")


def render(transcript: Transcript, fmt: str) -> str:
    if fmt not in FORMATS:
        raise SubtitleError(f"不支持的字幕格式：{fmt}")
    if fmt == "json":
        return (
            json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    if fmt == "txt":
        return (
            "\n".join(display_text(segment, transcript.metadata) for segment in transcript.segments)
            + "\n"
        )
    if fmt in ("srt", "vtt"):
        blocks = []
        for index, segment in enumerate(transcript.segments, 1):
            start, end = times(segment.start, segment.end, 1000, "," if fmt == "srt" else ".")
            text = display_text(segment, transcript.metadata)
            text = html.escape(text, quote=False) if fmt == "vtt" else text
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
    style = transcript.metadata.get("style", {})
    font = str(style.get("font", "Arial"))
    import re

    if not re.fullmatch(r"[^,\r\n{}\\]{1,80}", font):
        raise SubtitleError("字体名称无效。")
    color = style.get("color", "#FFFFFF")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise SubtitleError("字幕颜色无效。")
    color = "&H00" + color[5:7] + color[3:5] + color[1:3]
    size, margin = int(style.get("size", 54)), int(style.get("margin", 60))
    if not 12 <= size <= 200 or not 0 <= margin <= 500:
        raise SubtitleError("字幕样式参数无效。")
    alignment = {"bottom": 2, "middle": 5, "top": 8}.get(style.get("position", "bottom"))
    if alignment is None:
        raise SubtitleError("字幕位置无效。")
    header = header.replace(
        "Style: Default,Arial,54,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,60,1",
        f"Style: Default,{font},{size},{color},&H0000FFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,2,1,{alignment},60,60,{margin},1",
    )
    lines = []
    for segment in transcript.segments:
        start, end = times(segment.start, segment.end, 100, ".")
        text = ass_text(display_text(segment, transcript.metadata))
        if (
            transcript.metadata.get("word_highlight")
            and segment.words
            and transcript.metadata.get("export_mode", "original") == "original"
        ):
            pieces = []
            cursor = segment.start
            if transcript.metadata.get("speaker_labels") and segment.speaker:
                pieces.append(ass_text(segment.speaker + ": "))
            for word in segment.words:
                gap = max(0, round((word.start - cursor) * 100))
                if gap:
                    pieces.append(r"{\k" + str(gap) + "}")
                pieces.append(
                    r"{\kf"
                    + str(max(1, round((word.end - word.start) * 100)))
                    + "}"
                    + ass_text(word.text)
                )
                cursor = word.end
            text = "".join(pieces).strip()
        speaker = ass_text(segment.speaker or "").replace(",", "，")
        lines.append(f"Dialogue: 0,{start},{end},Default,{speaker},0,0,0,,{text}")
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


def preflight_output(directory, stem, formats, overwrite=False, protected=None):
    paths = output_paths(directory, stem, formats, overwrite, protected)
    ensure_output_directory(paths[0].parent, overwrite)
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
                    try:
                        os.link(temp_path, path)
                    except OSError as exc:
                        if isinstance(exc, FileExistsError) or exc.errno not in LINK_UNSUPPORTED:
                            raise
                        # SMB may not support hard links. Exclusive creation still protects
                        # existing files, but a reader can see the copy before it finishes.
                        copy_exclusive(temp_path, path)
                except FileExistsError as exc:
                    raise SubtitleError(f"输出已存在：{path}") from exc
                temp_path.unlink()
    finally:
        for temp_path, _ in pending:
            temp_path.unlink(missing_ok=True)
    return paths

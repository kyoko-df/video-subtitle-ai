from __future__ import annotations

import os
import tempfile
import wave
from array import array
from pathlib import Path

from .diagnostics import ffmpeg_capabilities
from .exporters import render
from .media import binary, check_cancel, extract_audio, run_process
from .models import SubtitleError


def waveform(source, track, directory, cancel=None, points=2000):
    with tempfile.TemporaryDirectory(dir=directory, prefix="waveform-") as temp:
        audio = Path(temp) / "audio.wav"
        extract_audio(source, audio, track, cancel)
        peaks = []
        with wave.open(str(audio), "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
            block = max(1, (stream.getnframes() + points - 1) // points)
            while raw := stream.readframes(block):
                check_cancel(cancel)
                samples = array("h", raw)
                import sys

                if sys.byteorder != "little":
                    samples.byteswap()
                peaks.append(round(max((abs(n) for n in samples), default=0) / 32768, 4))
        return {"peaks": peaks, "duration": duration}


def export_video(source, transcript, directory, mode="burn", cancel=None):
    executable = binary("ffmpeg")
    capabilities = ffmpeg_capabilities(executable, Path(executable).stat().st_mtime_ns)
    if mode not in {"burn", "soft"}:
        raise SubtitleError("视频导出方式必须是 burn 或 soft。")
    if not capabilities[f"{mode}_supported"]:
        detail = "、".join(capabilities["missing_burn"]) if mode == "burn" else "aac / mov_text"
        raise SubtitleError(
            f"当前 FFmpeg 无法执行此导出，请安装支持 {detail or '所需滤镜和编码器'} 的版本。"
        )
    directory.mkdir(parents=True, exist_ok=True)
    # Temporary filter paths contain no user filenames; escape FFmpeg's filter grammar.
    with tempfile.TemporaryDirectory(prefix="shengmu-render-", dir=directory) as temp:
        folder = Path(temp)
        subtitle = folder / ("captions.ass" if mode == "burn" else "captions.srt")
        subtitle.write_text(
            render(transcript, "ass" if mode == "burn" else "srt"), encoding="utf-8"
        )
        pending = folder / "video.mp4"
        args = [
            executable,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source.resolve()),
        ]
        if mode == "burn":
            escaped = (
                str(subtitle.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"'\''")
            )
            args += [
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                f"ass=filename='{escaped}'",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
            ]
        else:
            args += [
                "-i",
                str(subtitle),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-map",
                "1:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "title=Shengmu",
                "-disposition:s:0",
                "default",
            ]
        args += ["-movflags", "+faststart", str(pending)]
        run_process(args, cancel, timeout=12 * 3600)
        check_cancel(cancel)
        target = directory / f"video-{mode}.mp4"
        os.replace(pending, target)
        return target

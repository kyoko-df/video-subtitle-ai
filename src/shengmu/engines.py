from __future__ import annotations

import os
import wave
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Protocol

from .media import check_cancel
from .models import Segment, SubtitleError

Progress = Callable[[float, str], None]


@dataclass(frozen=True)
class EngineOptions:
    engine: str = "local"
    model: str = "small"
    language: str | None = None
    device: str = "cpu"
    compute_type: str = "int8"
    prompt: str = ""
    chunk_seconds: int = 600

    def validate(self) -> None:
        if self.engine not in ("local", "openai"):
            raise SubtitleError("转写引擎必须是 local 或 openai。")
        if not 30 <= self.chunk_seconds <= 600:
            raise SubtitleError("API 分块长度必须在 30 到 600 秒之间。")
        if self.device not in ("cpu", "cuda", "auto"):
            raise SubtitleError("设备必须是 cpu、cuda 或 auto。")


class Engine(Protocol):
    def transcribe(
        self, audio: Path, options: EngineOptions, progress: Progress, cancel: Event | None
    ) -> tuple[list[Segment], str | None, str]: ...


class LocalEngine:
    def transcribe(
        self, audio: Path, options: EngineOptions, progress: Progress, cancel: Event | None
    ) -> tuple[list[Segment], str | None, str]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SubtitleError('本地引擎尚未安装。请运行 pip install -e ".[local]"。') from exc
        progress(0, "正在加载本地模型，首次运行会下载模型…")
        check_cancel(cancel)
        try:
            model = WhisperModel(
                options.model,
                device=options.device,
                compute_type=options.compute_type,
                download_root=os.environ.get("SHENGMU_MODEL_DIR"),
            )
            check_cancel(cancel)
            segments, info = model.transcribe(
                str(audio),
                language=options.language,
                initial_prompt=options.prompt or None,
                vad_filter=True,
                beam_size=5,
            )
            with wave.open(str(audio), "rb") as stream:
                duration = stream.getnframes() / stream.getframerate()
            result = []
            for segment in segments:
                check_cancel(cancel)
                if segment.text.strip() and segment.end > segment.start:
                    result.append(Segment(max(0, segment.start), segment.end, segment.text))
                progress(min(0.99, segment.end / duration), "正在识别语音…")
            check_cancel(cancel)
            return result, info.language, options.model
        except SubtitleError:
            raise
        except Exception as exc:
            # Do not expose network URLs or credentials embedded in vendor exceptions.
            raise SubtitleError(
                "本地模型转写失败。请检查模型下载、设备和计算精度；Mac 建议 cpu / int8。"
            ) from exc


def wav_chunks(audio: Path, chunk_seconds: int):
    """Yield bounded PCM WAV chunks without holding a whole recording in memory."""
    with wave.open(str(audio), "rb") as source:
        rate = source.getframerate()
        frames_per_chunk = rate * chunk_seconds
        offset = 0
        while True:
            frames = source.readframes(frames_per_chunk)
            if not frames:
                break
            chunk = audio.parent / "api-chunk.wav"
            with wave.open(str(chunk), "wb") as target:
                target.setparams(source.getparams())
                target.writeframes(frames)
            # 600 seconds of 16kHz/mono/16-bit PCM is ~19.2MB, below the API file limit.
            if chunk.stat().st_size >= 25_000_000:
                raise SubtitleError("API 音频分块超过 25MB，请降低 --chunk-seconds。")
            duration = len(frames) / (rate * source.getnchannels() * source.getsampwidth())
            try:
                yield chunk, offset / rate, duration
            finally:
                chunk.unlink(missing_ok=True)
            offset += round(duration * rate)


class OpenAIEngine:
    def transcribe(
        self, audio: Path, options: EngineOptions, progress: Progress, cancel: Event | None
    ) -> tuple[list[Segment], str | None, str]:
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            raise SubtitleError("请先设置 OPENAI_API_KEY 环境变量，再使用 OpenAI 转写。")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise SubtitleError('OpenAI 引擎尚未安装。请运行 pip install -e ".[openai]"。') from exc
        with wave.open(str(audio), "rb") as source:
            total = source.getnframes() / source.getframerate()
        result = []
        language = options.language
        # whisper-1 supports verbose_json segment timestamps; GPT transcription models
        # cannot be substituted without a different timestamp/alignment strategy.
        with OpenAI(timeout=120.0, max_retries=2) as client:
            for chunk, offset, duration in wav_chunks(audio, options.chunk_seconds):
                check_cancel(cancel)
                progress(
                    offset / total, f"正在转写音频片段 {int(offset)}–{int(offset + duration)} 秒…"
                )
                kwargs = {
                    "model": "whisper-1",
                    "response_format": "verbose_json",
                    "timestamp_granularities": ["segment"],
                    "temperature": 0,
                }
                if options.language:
                    kwargs["language"] = options.language
                if options.prompt:
                    kwargs["prompt"] = options.prompt
                try:
                    with chunk.open("rb") as stream:
                        response = client.audio.transcriptions.create(file=stream, **kwargs)
                except Exception as exc:
                    check_cancel(cancel)
                    raise SubtitleError(
                        "OpenAI 转写失败。请检查 API Key、额度、网络及 OPENAI_BASE_URL。"
                    ) from exc
                check_cancel(cancel)
                language = language or getattr(response, "language", None)
                if response.segments is None and response.text.strip():
                    raise SubtitleError(
                        "转写服务返回了文本，但没有片段时间戳。请使用支持 verbose_json / segments 的服务。"
                    )
                for segment in response.segments or []:
                    start = max(0.0, float(segment.start))
                    end = min(duration, float(segment.end))
                    if end > start and segment.text.strip():
                        result.append(Segment(start + offset, end + offset, segment.text))
                progress((offset + duration) / total, "当前片段转写完成。")
        return result, language, "whisper-1"


def get_engine(name: str) -> Engine:
    if name == "local":
        return LocalEngine()
    if name == "openai":
        return OpenAIEngine()
    raise SubtitleError(f"未知引擎：{name}")

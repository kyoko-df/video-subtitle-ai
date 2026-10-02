from __future__ import annotations

import os
import sys
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock
from typing import Callable, Protocol

from .captions import CaptionOptions, is_hallucination, split_caption
from .media import check_cancel
from .models import Segment, SubtitleError, shift_segment

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
    condition_on_previous_text: bool = False
    filter_hallucinations: bool = True
    captions: CaptionOptions = field(default_factory=CaptionOptions)
    diarize: bool = False
    num_speakers: int | None = None

    @property
    def initial_prompt(self) -> str | None:
        if self.prompt:
            return self.prompt
        if self.language and self.language.lower().startswith("zh"):
            return "以下是普通话的句子，请使用简体中文，并添加标点符号。"
        return None

    def validate(self) -> None:
        self.captions.validate()
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
    _cached_key = None
    _cached_model = None
    _lock = RLock()

    def transcribe(
        self, audio: Path, options: EngineOptions, progress: Progress, cancel: Event | None
    ) -> tuple[list[Segment], str | None, str]:
        with self._lock:
            return self._transcribe(audio, options, progress, cancel)

    def _transcribe(
        self, audio: Path, options: EngineOptions, progress: Progress, cancel: Event | None
    ) -> tuple[list[Segment], str | None, str]:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SubtitleError('本地引擎尚未安装。请运行 pip install -e ".[local]"。') from exc
        progress(0, "正在加载本地模型，首次运行会下载模型…")
        check_cancel(cancel)
        try:
            key = (
                options.model,
                options.device,
                options.compute_type,
                os.environ.get("SHENGMU_MODEL_DIR"),
            )
            if LocalEngine._cached_key != key or LocalEngine._cached_model is None:
                LocalEngine._cached_model = None
                LocalEngine._cached_key = None
                LocalEngine._cached_model = WhisperModel(
                    options.model,
                    device=options.device,
                    compute_type=options.compute_type,
                    download_root=key[3],
                )
                LocalEngine._cached_key = key
            model = LocalEngine._cached_model
            check_cancel(cancel)
            segments, info = model.transcribe(
                str(audio),
                language=options.language,
                initial_prompt=options.initial_prompt,
                vad_filter=True,
                beam_size=5,
                word_timestamps=True,
                condition_on_previous_text=options.condition_on_previous_text,
            )
            with wave.open(str(audio), "rb") as stream:
                duration = stream.getnframes() / stream.getframerate()
            result = []
            for segment in segments:
                check_cancel(cancel)
                if not options.filter_hallucinations or not is_hallucination(segment.text):
                    result.extend(
                        split_caption(
                            max(0, segment.start),
                            segment.end,
                            segment.text,
                            getattr(segment, "words", None),
                            options.captions,
                        )
                    )
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


def silence_cut(frames: bytes, rate: int, channels: int, width: int) -> int | None:
    if width != 2:
        return None
    frame_size = channels * width
    total = len(frames) // frame_size
    begin = max(0, total - rate * 5)
    samples = array("h", frames[begin * frame_size :])
    if sys.byteorder != "little":
        samples.byteswap()
    window = max(1, rate // 10)
    quiet_start = None
    candidates = []
    for position in range(0, len(samples), window * channels):
        block = samples[position : position + window * channels]
        quiet = sum(value * value for value in block) / len(block) <= 200**2
        frame = begin + position // channels
        if quiet and quiet_start is None:
            quiet_start = frame
        finish = min(total, frame + len(block) // channels)
        if quiet_start is not None and (not quiet or finish == total):
            quiet_end = finish if quiet else frame
            if quiet_end - quiet_start >= rate * 0.3:
                candidates.append(total if quiet_end == total else (quiet_start + quiet_end) // 2)
            quiet_start = None
    return candidates[-1] if candidates else None


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
            if source.tell() < source.getnframes():
                frame_size = source.getnchannels() * source.getsampwidth()
                cut = silence_cut(frames, rate, source.getnchannels(), source.getsampwidth())
                if cut is not None:
                    frames = frames[: cut * frame_size]
                    source.setpos(offset + cut)
            chunk = audio.parent / "api-chunk.wav"
            try:
                with wave.open(str(chunk), "wb") as target:
                    target.setparams(source.getparams())
                    target.writeframes(frames)
                # 600 seconds of 16kHz/mono/16-bit PCM is ~19.2MB, below the API file limit.
                if chunk.stat().st_size >= 25_000_000:
                    raise SubtitleError("API 音频分块超过 25MB，请降低 --chunk-seconds。")
                duration = len(frames) / (rate * source.getnchannels() * source.getsampwidth())
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
        context = ""
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
                    "timestamp_granularities": ["word", "segment"],
                    "temperature": 0,
                }
                if options.language:
                    kwargs["language"] = options.language
                prompt = "\n".join(part for part in (options.initial_prompt, context) if part)
                if prompt:
                    kwargs["prompt"] = prompt
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
                current = []
                response_words = getattr(response, "words", None) or []
                for segment in response.segments or []:
                    if options.filter_hallucinations and is_hallucination(segment.text):
                        continue
                    start = max(0.0, float(segment.start))
                    end = min(duration, float(segment.end))
                    words = [w for w in response_words if start <= (w.start + w.end) / 2 < end]
                    for cue in split_caption(start, end, segment.text, words, options.captions):
                        current.append(shift_segment(cue, offset))
                result.extend(current)
                if current:
                    context = " ".join(c.text.replace("\n", " ") for c in current)[-400:]
                progress((offset + duration) / total, "当前片段转写完成。")
        return result, language, "whisper-1"


def get_engine(name: str) -> Engine:
    if name == "local":
        return LocalEngine()
    if name == "openai":
        return OpenAIEngine()
    raise SubtitleError(f"未知引擎：{name}")

from __future__ import annotations

import math
import os
import sys
import wave
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from .captions import is_hallucination, mark_suspicions, repetitive_text, split_caption
from .devices import mlx_status
from .errors import local_model_error
from .media import check_cancel
from .model_manager import (
    MLX_FILES,
    MLX_REPOS,
    mlx_cache_dir,
    model_complete,
    model_path,
    model_root,
)
from .models import SubtitleError, shift_segment


def resolve_model(name):
    path = Path(name).expanduser()
    if path.is_dir():
        if not model_complete(path, "mlx"):
            raise SubtitleError(
                "请选择 MLX Whisper 模型目录；faster-whisper / CTranslate2 权重不能用于 Mac GPU。"
            )
        return str(path.resolve())
    if name.startswith(("/", "~", ".")):
        raise SubtitleError("MLX 模型目录不存在，请检查 --model 路径。")
    if name in MLX_REPOS and os.environ.get("SHENGMU_MODEL_DIR"):
        managed = model_path(model_root(Path.cwd()), name, "mlx")
        if (managed / ".complete").is_file() and model_complete(managed, "mlx"):
            return str(managed)
    from huggingface_hub import snapshot_download

    path = snapshot_download(
        repo_id=MLX_REPOS.get(name, name),
        cache_dir=mlx_cache_dir(),
        allow_patterns=MLX_FILES,
    )
    if not model_complete(Path(path), "mlx"):
        raise SubtitleError("模型缺少 MLX Whisper 权重，请选择 MLX 格式模型。")
    return path


def restore_timestamps(segment, timestamps):
    words = []
    for word in segment.get("words") or []:
        middle = (word["start"] + word["end"]) / 2
        index = timestamps.get_chunk_index(middle)
        words.append(
            SimpleNamespace(
                start=timestamps.get_original_time(word["start"], index),
                end=timestamps.get_original_time(word["end"], index),
                word=word["word"],
            )
        )
    start = words[0].start if words else timestamps.get_original_time(segment["start"])
    end = words[-1].end if words else timestamps.get_original_time(segment["end"], is_end=True)
    return start, end, words


class MLXEngine:
    _cached_key = None

    @classmethod
    def clear_cache(cls):
        # mlx-whisper 0.4 caches by path only; precision changes must also invalidate it.
        module = sys.modules.get("mlx_whisper.transcribe")
        if module is not None:
            module.ModelHolder.model = module.ModelHolder.model_path = None
        cls._cached_key = None
        core = sys.modules.get("mlx.core")
        if core is not None:
            core.clear_cache()

    def transcribe(self, audio, options, progress, cancel):
        from .engines import LocalEngine, wav_chunks

        check_cancel(cancel)
        status = mlx_status()
        if not status["available"]:
            raise SubtitleError(status["message"])
        if options.compute_type not in ("auto", "float16", "float32"):
            raise SubtitleError("Mac GPU 仅支持 auto、float16 或 float32 精度。")
        try:
            import mlx.core as mx
            import mlx_whisper
            import numpy as np
            from faster_whisper.vad import SpeechTimestampsMap, VadOptions, get_speech_timestamps

            # The parent LocalEngine lock also serializes MLX's process-wide model cache.
            with LocalEngine._lock:
                progress(0, "正在加载 Mac GPU 模型，首次运行会下载 MLX 权重…")
                model = resolve_model(options.model)
                check_cancel(cancel)
                fp16 = options.compute_type != "float32"
                key = (model, fp16)
                if self._cached_key != key:
                    self.clear_cache()
                    MLXEngine._cached_key = key
                parameters = options.decoding_parameters()
                with wave.open(str(audio), "rb") as stream:
                    total = stream.getnframes() / stream.getframerate()
                result, language, context = [], options.language, ""
                # Bound waveform / mel memory, report real progress and check cancellation.
                # Silence-aware boundaries retain all input samples before VAD.
                for chunk, offset, duration in wav_chunks(audio, min(options.chunk_seconds, 60)):
                    check_cancel(cancel)
                    progress(
                        offset / total,
                        f"正在使用 Mac GPU 识别 {int(offset)}–{int(offset + duration)} 秒…",
                    )
                    with wave.open(str(chunk), "rb") as stream:
                        samples = (
                            np.frombuffer(
                                stream.readframes(stream.getnframes()), dtype="<i2"
                            ).astype(np.float32)
                            / 32768
                        )
                    speech = get_speech_timestamps(
                        samples, VadOptions(**parameters["vad_parameters"]), sampling_rate=16000
                    )
                    check_cancel(cancel)
                    if speech:
                        compact = np.concatenate([samples[s["start"] : s["end"]] for s in speech])
                        timestamps = SpeechTimestampsMap(speech, 16000)
                        prompt = (
                            "\n".join(p for p in (options.initial_prompt, context) if p) or None
                        )
                        with mx.stream(mx.gpu):
                            response = mlx_whisper.transcribe(
                                compact,
                                path_or_hf_repo=model,
                                language=language,
                                fp16=fp16,
                                verbose=None,
                                word_timestamps=True,
                                initial_prompt=prompt,
                                condition_on_previous_text=options.condition_on_previous_text,
                                compression_ratio_threshold=parameters[
                                    "compression_ratio_threshold"
                                ],
                                logprob_threshold=parameters["log_prob_threshold"],
                                no_speech_threshold=parameters["no_speech_threshold"],
                            )
                        check_cancel(cancel)
                        language = language or response.get("language")
                        current = []
                        for segment in response.get("segments", []):
                            check_cancel(cancel)
                            text = segment["text"]
                            if options.filter_hallucinations and is_hallucination(text):
                                continue
                            start, end, words = restore_timestamps(segment, timestamps)
                            diagnostics = {
                                k: float(v)
                                for k in ("avg_logprob", "no_speech_prob", "compression_ratio")
                                if isinstance(v := segment.get(k), (int, float))
                                and math.isfinite(v)
                            }
                            current.extend(
                                replace(
                                    shift_segment(cue, offset),
                                    diagnostics=diagnostics,
                                    suspicions=["疑似重复循环，请试听确认"]
                                    if repetitive_text(text)
                                    else [],
                                )
                                for cue in split_caption(
                                    max(0, start), min(duration, end), text, words, options.captions
                                )
                            )
                        result.extend(current)
                        if options.condition_on_previous_text and current:
                            context = " ".join(c.text.replace("\n", " ") for c in current)[-400:]
                        mx.clear_cache()
                    progress(min(0.99, (offset + duration) / total), "Mac GPU 当前片段识别完成。")
                check_cancel(cancel)
                return mark_suspicions(result), language, options.model
        except SubtitleError:
            self.clear_cache()
            raise
        except Exception as exc:
            self.clear_cache()
            raise local_model_error(exc) from exc

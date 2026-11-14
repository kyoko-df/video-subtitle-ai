from __future__ import annotations

import tempfile
import wave
from contextlib import closing, nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Callable

from .asr_checkpoints import ASRCheckpoint, source_version, transcription_fingerprint
from .captions import mark_suspicions
from .devices import local_backend
from .diagnostics import ensure_output_directory
from .engines import Engine, EngineOptions, get_engine, wav_chunks
from .exporters import export_files, preflight_output
from .media import check_cancel, extract_audio, probe
from .models import SubtitleError, Transcript, normalize_segments, shift_segment
from .storage import workspace_lock

StageProgress = Callable[[str, float, str], None]


def transcribe(
    source: Path,
    options: EngineOptions,
    track_index: int | None = None,
    progress: StageProgress | None = None,
    cancel: Event | None = None,
    engine: Engine | None = None,
) -> Transcript:
    options.validate()
    source = source.expanduser().resolve()
    update = progress or (lambda stage, amount, message: None)
    check_cancel(cancel)
    original = source_version(source) if options.checkpoint_dir is not None else None
    fingerprint = (
        transcription_fingerprint(source, options, track_index) if original is not None else None
    )
    update("probe", 0.02, "正在读取媒体和音轨…")
    info = probe(source, cancel)
    track_index = info.audio_tracks[0].index if track_index is None else track_index
    if track_index not in {track.index for track in info.audio_tracks}:
        raise SubtitleError("所选音轨不存在。使用 inspect 查看可用音轨编号。")
    folder = Path(options.checkpoint_dir) / "asr" / fingerprint if fingerprint else None
    if folder is not None:
        ensure_output_directory(folder, overwrite=True)
    with (
        workspace_lock(folder, "该转写检查点正在被另一任务使用，请等待完成或选择其他检查点目录。")
        if folder
        else nullcontext(),
        tempfile.TemporaryDirectory(prefix="shengmu-audio-") as temporary,
    ):
        audio = Path(temporary) / "audio.wav"
        update("extract", 0.08, "正在提取音轨…")
        extract_audio(source, audio, track_index, cancel)
        with wave.open(str(audio), "rb") as stream:
            audio_duration = stream.getnframes() / stream.getframerate()
        if audio_duration <= 0:
            raise SubtitleError("所选音轨为空。")
        selected_engine = engine or get_engine(options.engine)

        def engine_progress(amount, message):
            update("transcribe", 0.18 + 0.76 * amount, message)

        stats = None
        if folder is not None:

            def check_source():
                if source_version(source) != original:
                    raise SubtitleError("转写期间源文件发生变化，请重新添加视频或重新运行命令。")

            check_source()
            segments, language, model, stats = transcribe_chunks(
                audio,
                options,
                selected_engine,
                folder,
                fingerprint,
                engine_progress,
                cancel,
                check_source,
            )
            check_source()
        else:
            segments, language, model = selected_engine.transcribe(
                audio, options, engine_progress, cancel
            )
        check_cancel(cancel)
        if options.diarize:
            from .speakers import diarize

            update("transcribe", 0.92, "正在区分说话人…")
            segments = diarize(audio, segments, options.num_speakers, cancel)
        # WAV starts at zero. Restore a delayed track's offset on the container timeline.
        offset = next(track.offset for track in info.audio_tracks if track.index == track_index)
        aligned = [shift_segment(s, offset) for s in normalize_segments(segments, audio_duration)]
        timeline_duration = max(info.duration, audio_duration + offset)
        transcript = Transcript(
            source.name,
            timeline_duration,
            language,
            options.engine,
            model,
            aligned,
            {
                "audio_track": track_index,
                "audio_offset": offset,
                "audio_duration": audio_duration,
                "asr_profile": options.asr_profile,
                "asr_backend": local_backend(options.device)
                if options.engine == "local"
                else "openai",
                "asr_parameters": options.decoding_parameters()
                if options.engine == "local"
                else {},
                **({"asr_stats": stats} if stats is not None else {}),
            },
        )
        update("ready", 0.96, f"识别完成，共 {len(transcript.segments)} 条字幕。")
        return transcript


def transcribe_chunks(audio, options, engine, folder, fingerprint, progress, cancel, check_source):
    with wave.open(str(audio), "rb") as stream:
        frames, rate = stream.getnframes(), stream.getframerate()
    checkpoint = ASRCheckpoint(
        folder,
        fingerprint,
        frames,
        rate,
        options.resume,
        lambda message: progress(0, message),
    )
    if checkpoint.restored_chunks:
        progress(
            checkpoint.next_frame / frames,
            f"已恢复转写 {checkpoint.restored_chunks} 块、{checkpoint.next_frame / rate:.1f} 秒，继续剩余部分…",
        )
    with closing(
        wav_chunks(
            audio,
            options.asr_chunk_seconds,
            start_frame=checkpoint.next_frame,
            chunk_name="checkpoint-chunk.wav",
        )
    ) as chunks:
        for chunk, offset, duration in chunks:
            check_cancel(cancel)
            check_source()
            start = round(offset * rate)
            end = start + round(duration * rate)
            context = ""
            if options.condition_on_previous_text or options.engine == "openai":
                context = " ".join(c.text.replace("\n", " ") for c in checkpoint.segments[-100:])[
                    -400:
                ]
            current = replace(options, detected_language=checkpoint.language, context=context)

            def update(amount, message, start=start, end=end):
                progress(
                    min(0.99, (start + min(1, max(0, amount)) * (end - start)) / frames),
                    f"分块 {start / rate:.1f}–{end / rate:.1f} 秒：{message}",
                )

            segments, language, model = engine.transcribe(chunk, current, update, cancel)
            check_source()
            result = Transcript(
                "chunk.wav",
                duration,
                options.language or language,
                options.engine,
                model,
                normalize_segments(segments, duration),
            )
            checkpoint.save(start, end, result)
            progress(
                end / frames,
                f"已保存转写检查点：{end / rate:.1f} / {frames / rate:.1f} 秒（{len(checkpoint.parts)} 块）",
            )
            check_cancel(cancel)
    return (
        mark_suspicions(checkpoint.segments),
        options.language or checkpoint.language,
        checkpoint.model or options.model,
        checkpoint.stats(),
    )


def transcribe_to_files(
    source: Path,
    options: EngineOptions,
    output_dir: Path,
    formats: list[str],
    track_index: int | None = None,
    overwrite: bool = False,
    progress: StageProgress | None = None,
    cancel: Event | None = None,
) -> tuple[Transcript, list[Path]]:
    # Detect conflicts before a potentially expensive model run or cloud request.
    preflight_output(output_dir, source.stem, formats, overwrite, protected=source)
    transcript = transcribe(source, options, track_index, progress, cancel)
    check_cancel(cancel)
    if progress:
        progress("export", 0.97, "正在导出字幕…")
    paths = export_files(transcript, output_dir, source.stem, formats, overwrite, protected=source)
    if progress:
        progress("done", 1.0, "字幕已导出。")
    return transcript, paths

from __future__ import annotations

import tempfile
import wave
from pathlib import Path
from threading import Event
from typing import Callable

from .engines import Engine, EngineOptions, get_engine
from .exporters import export_files, output_paths
from .media import check_cancel, extract_audio, probe
from .models import SubtitleError, Transcript, normalize_segments, shift_segment

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
    update("probe", 0.02, "正在读取媒体和音轨…")
    info = probe(source, cancel)
    track_index = info.audio_tracks[0].index if track_index is None else track_index
    if track_index not in {track.index for track in info.audio_tracks}:
        raise SubtitleError("所选音轨不存在。使用 inspect 查看可用音轨编号。")
    with tempfile.TemporaryDirectory(prefix="shengmu-audio-") as temporary:
        audio = Path(temporary) / "audio.wav"
        update("extract", 0.08, "正在提取音轨…")
        extract_audio(source, audio, track_index, cancel)
        with wave.open(str(audio), "rb") as stream:
            audio_duration = stream.getnframes() / stream.getframerate()
        if audio_duration <= 0:
            raise SubtitleError("所选音轨为空。")
        selected_engine = engine or get_engine(options.engine)
        segments, language, model = selected_engine.transcribe(
            audio,
            options,
            lambda amount, message: update("transcribe", 0.18 + 0.76 * amount, message),
            cancel,
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
                "asr_parameters": options.decoding_parameters()
                if options.engine == "local"
                else {},
            },
        )
        update("ready", 0.96, f"识别完成，共 {len(transcript.segments)} 条字幕。")
        return transcript


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
    output_paths(output_dir, source.stem, formats, overwrite, protected=source)
    transcript = transcribe(source, options, track_index, progress, cancel)
    check_cancel(cancel)
    if progress:
        progress("export", 0.97, "正在导出字幕…")
    paths = export_files(transcript, output_dir, source.stem, formats, overwrite, protected=source)
    if progress:
        progress("done", 1.0, "字幕已导出。")
    return transcript, paths

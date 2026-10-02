from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from threading import Event

import pytest

from shengmu.cli import main
from shengmu.engines import EngineOptions
from shengmu.media import extract_audio, probe
from shengmu.models import Cancelled, Segment, SubtitleError
from shengmu.pipeline import transcribe, transcribe_to_files


class FakeEngine:
    def __init__(self):
        self.audio = None

    def transcribe(self, audio, options, progress, cancel):
        self.audio = audio
        with wave.open(str(audio), "rb") as stream:
            assert stream.getnchannels() == 1
            assert stream.getframerate() == 16000
            assert stream.getsampwidth() == 2
        progress(0.5, "half")
        return [Segment(0.1, 1.4, "测试语音"), Segment(1.4, 2.4, "带时间轴")], "zh", "fixture"


def test_multitrack_extraction_and_cleanup(media: Path, tmp_path: Path):
    info = probe(media)
    assert [t.index for t in info.audio_tracks] == [1, 2]
    assert [t.language for t in info.audio_tracks] == ["eng", "zho"]
    assert info.duration == pytest.approx(3, abs=0.1)
    audio = tmp_path / "extracted.wav"
    extract_audio(media, audio, 2)
    with wave.open(str(audio), "rb") as stream:
        assert stream.getframerate() == 16000
        assert stream.getnframes() / stream.getframerate() == pytest.approx(3, abs=0.1)
    engine = FakeEngine()
    result = transcribe(media, EngineOptions(), 2, engine=engine)
    assert result.metadata["audio_track"] == 2
    assert result.segments[0].text == "测试语音"
    assert not engine.audio.exists()


def test_invalid_track_and_cancel(media: Path):
    with pytest.raises(SubtitleError, match="音轨不存在"):
        transcribe(media, EngineOptions(), 99, engine=FakeEngine())
    cancelled = Event()
    cancelled.set()
    with pytest.raises(Cancelled):
        transcribe(media, EngineOptions(), cancel=cancelled, engine=FakeEngine())


def test_cleanup_when_engine_fails(media: Path):
    class FailingEngine:
        audio = None

        def transcribe(self, audio, *args):
            self.audio = audio
            raise SubtitleError("expected")

    engine = FailingEngine()
    with pytest.raises(SubtitleError, match="expected"):
        transcribe(media, EngineOptions(), engine=engine)
    assert not engine.audio.exists()


def test_no_audio_error(tmp_path: Path):
    silent = tmp_path / "silent.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=1",
            "-c:v",
            "mpeg4",
            str(silent),
        ],
        check=True,
    )
    with pytest.raises(SubtitleError, match="没有音轨"):
        probe(silent)


def test_delayed_audio_restores_video_timeline(tmp_path: Path):
    delayed = tmp_path / "delayed.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=5",
            "-itsoffset",
            "2",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=3",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s16le",
            str(delayed),
        ],
        check=True,
    )
    assert probe(delayed).audio_tracks[0].offset == pytest.approx(2, abs=0.01)
    result = transcribe(delayed, EngineOptions(), engine=FakeEngine())
    assert result.segments[0].start == pytest.approx(2.1, abs=0.01)


def test_cli_inspect_and_json_export(media: Path, tmp_path: Path, capsys):
    assert main(["inspect", str(media)]) == 0
    assert len(json.loads(capsys.readouterr().out)["audio_tracks"]) == 2
    result = transcribe(media, EngineOptions(), engine=FakeEngine())
    source = tmp_path / "transcript.json"
    source.write_text(json.dumps(result.to_dict(), ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "exports"
    assert (
        main(["export", str(source), "--format", "srt", "vtt", "ass", "--output-dir", str(output)])
        == 0
    )
    assert len(json.loads(capsys.readouterr().out)["files"]) == 3
    assert (
        main(
            [
                "export",
                str(source),
                "--format",
                "json",
                "--output-dir",
                str(tmp_path),
                "--overwrite",
            ]
        )
        == 1
    )


def test_cli_verbose_prints_redacted_exception_chain(monkeypatch, capsys):
    def fail(*args):
        try:
            raise RuntimeError("vendor https://user:password@example.com token test-secret-value")
        except RuntimeError as exc:
            raise SubtitleError("friendly message") from exc

    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-value")
    monkeypatch.setattr("shengmu.cli.probe", fail)
    assert main(["inspect", "missing.mp4", "--verbose"]) == 1
    error = capsys.readouterr().err
    assert "RuntimeError" in error and "Traceback" in error
    assert "test-secret-value" not in error and "user:password" not in error


def test_output_conflict_stops_before_ai(monkeypatch, tmp_path: Path):
    existing = tmp_path / "video.srt"
    existing.write_text("keep")

    def unexpected(*args, **kwargs):
        raise AssertionError("AI should not run when output exists")

    monkeypatch.setattr("shengmu.pipeline.transcribe", unexpected)
    with pytest.raises(SubtitleError, match="输出已存在"):
        transcribe_to_files(tmp_path / "video.mp4", EngineOptions(), tmp_path, ["srt"])
    assert existing.read_text() == "keep"

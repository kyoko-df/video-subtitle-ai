from __future__ import annotations

import hashlib
import json
import time
import wave
from dataclasses import asdict, replace
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shengmu import asr_checkpoints, pipeline
from shengmu.asr_checkpoints import transcription_fingerprint
from shengmu.cli import main
from shengmu.engines import EngineOptions, OpenAIEngine
from shengmu.models import Cancelled, Segment, SubtitleError, Transcript, Word
from shengmu.server import create_app
from shengmu.storage import workspace_lock


@pytest.fixture
def recording(tmp_path):
    source = tmp_path / "long.wav"
    with wave.open(str(source), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        # A real silence boundary at 28–28.5 s exercises sample-accurate seeking.
        stream.writeframes(
            b"\x10\x27" * (16000 * 28) + b"\0\0" * 8000 + b"\x10\x27" * (16000 * 36 + 8000)
        )
    return source


class RecordingEngine:
    def __init__(self, fail=None, cancel=None, silent_first=False):
        self.calls, self.fail, self.cancel, self.silent_first = [], fail, cancel, silent_first

    def transcribe(self, audio, options, progress, cancel):
        with wave.open(str(audio), "rb") as stream:
            frames = stream.getnframes()
        self.calls.append((frames, options, audio))
        if len(self.calls) == self.fail:
            raise SubtitleError("simulated interruption")
        progress(0.5, "half")
        cues = (
            []
            if self.silent_first and len(self.calls) == 1
            else [
                Segment(
                    0.1,
                    0.9,
                    "你好",
                    [Word(0.1, 0.4, "你"), Word(0.4, 0.9, "好")],
                    diagnostics={"avg_logprob": -0.2},
                )
            ]
        )
        if self.cancel:
            self.cancel.set()
        return cues, "de" if not cues else "zh", "fixture"


def options(tmp_path, **changes):
    return replace(
        EngineOptions(model="tiny", checkpoint_dir=tmp_path / "checkpoints", asr_chunk_seconds=30),
        **changes,
    )


def checkpoint_folder(recording, config):
    return config.checkpoint_dir / "asr" / transcription_fingerprint(recording, config, None)


def test_resume_retains_silence_boundaries_words_context_and_completed_cache(recording, tmp_path):
    config = options(tmp_path, condition_on_previous_text=True, prompt="术语")
    first = RecordingEngine(fail=2)
    with pytest.raises(SubtitleError, match="interruption"):
        pipeline.transcribe(recording, config, engine=first)
    assert first.calls[0][0] == 28.25 * 16000
    assert not any(path.exists() for _, _, path in first.calls)
    resumed, events = RecordingEngine(), []
    result = pipeline.transcribe(
        recording, config, engine=resumed, progress=lambda *event: events.append(event)
    )
    assert len(resumed.calls) == 2
    assert [cue.start for cue in result.segments] == pytest.approx([0.1, 28.35, 58.35])
    assert [cue.words[1].end for cue in result.segments] == pytest.approx([0.9, 29.15, 59.15])
    assert all(
        cue.diagnostics == {"avg_logprob": -0.2} and not cue.words[0].estimated
        for cue in result.segments
    )
    assert result.language == "zh" and result.metadata["asr_stats"] == {
        "completed_chunks": 3,
        "restored_chunks": 1,
        "completed_seconds": 65,
        "restored_seconds": 28.25,
        "audio_seconds": 65,
    }
    assert any("已恢复转写" in message for _, _, message in events)
    assert resumed.calls[0][1].detected_language == "zh"
    assert resumed.calls[0][1].initial_prompt == "术语\n你好"
    assert resumed.calls[0][1].language is None
    cached = RecordingEngine(fail=1)
    again = pipeline.transcribe(recording, config, engine=cached)
    assert not cached.calls and again.segments == result.segments
    assert again.metadata["asr_stats"]["restored_chunks"] == 3
    fresh = RecordingEngine()
    result = pipeline.transcribe(recording, replace(config, resume=False), engine=fresh)
    assert len(fresh.calls) == 3 and result.metadata["asr_stats"]["restored_chunks"] == 0


def test_native_float_values_roundtrip_without_invalidating_chunk_checksums(recording, tmp_path):
    # MLX can return NumPy float subclasses. JSON parses them as ordinary floats.
    class NativeFloat(float):
        pass

    class NativeEngine:
        def transcribe(self, *args):
            return [Segment(NativeFloat(0), NativeFloat(1), "native")], "en", "fixture"

    config = options(tmp_path)
    pipeline.transcribe(recording, config, engine=NativeEngine())
    engine = RecordingEngine(fail=1)
    result = pipeline.transcribe(recording, config, engine=engine)
    assert not engine.calls and result.metadata["asr_stats"]["restored_chunks"] == 3


def test_cancel_preserves_completed_chunk_and_auto_language_ignores_silence(recording, tmp_path):
    config, cancelled = options(tmp_path), Event()
    with pytest.raises(Cancelled):
        pipeline.transcribe(
            recording,
            config,
            engine=RecordingEngine(cancel=cancelled, silent_first=True),
            cancel=cancelled,
        )
    next_engine = RecordingEngine()
    result = pipeline.transcribe(recording, config, engine=next_engine)
    assert result.metadata["asr_stats"]["restored_chunks"] == 1
    assert next_engine.calls[0][1].detected_language is None
    assert next_engine.calls[0][1].initial_prompt is None
    assert result.language == "zh" and result.segments[0].start == pytest.approx(28.35)


@pytest.mark.parametrize("damage", ["missing", "truncated", "checksum"])
def test_bad_chunk_reuses_only_valid_prefix(recording, tmp_path, damage):
    config = options(tmp_path)
    pipeline.transcribe(recording, config, engine=RecordingEngine())
    folder = checkpoint_folder(recording, config)
    damaged = folder / "00000001.json"
    if damage == "missing":
        damaged.unlink()
    elif damage == "truncated":
        damaged.write_text("{")
    else:
        data = json.loads(damaged.read_text())
        data["transcript"]["segments"][0]["text"] = "corrupted"
        damaged.write_text(json.dumps(data))
    engine, events = RecordingEngine(), []
    result = pipeline.transcribe(
        recording, config, engine=engine, progress=lambda *event: events.append(event)
    )
    assert len(engine.calls) == 2 and result.metadata["asr_stats"]["restored_chunks"] == 1
    assert all(cue.text == "你好" for cue in result.segments)
    assert any("部分损坏" in message for _, _, message in events)


def test_corrupt_manifest_and_interrupted_commit_do_not_publish_partial_state(
    recording, tmp_path, monkeypatch
):
    config = options(tmp_path)
    original = asr_checkpoints.atomic_json

    def failed(path, data):
        if path.name == "state.json" and len(data["chunks"]) == 2:
            raise OSError("simulated disk failure after writing a chunk")
        original(path, data)

    monkeypatch.setattr(asr_checkpoints, "atomic_json", failed)
    with pytest.raises(OSError):
        pipeline.transcribe(recording, config, engine=RecordingEngine())
    folder = checkpoint_folder(recording, config)
    assert (folder / "00000001.json").exists()
    assert len(json.loads((folder / "state.json").read_text())["chunks"]) == 1
    monkeypatch.setattr(asr_checkpoints, "atomic_json", original)
    result = pipeline.transcribe(recording, config, engine=RecordingEngine())
    assert result.metadata["asr_stats"]["restored_chunks"] == 1
    data = json.loads((folder / "state.json").read_text())
    data["chunks"][0]["file"] = "../../outside.json"
    (folder / "state.json").write_text(json.dumps(data))
    result = pipeline.transcribe(recording, config, engine=RecordingEngine())
    assert result.metadata["asr_stats"]["restored_chunks"] == 0


def test_fingerprint_isolates_settings_backend_model_and_source(recording, tmp_path, monkeypatch):
    config = options(tmp_path)
    fingerprint = transcription_fingerprint(recording, config, None)
    assert (
        transcription_fingerprint(
            recording,
            replace(
                config,
                captions=replace(config.captions, max_seconds=7.0, min_seconds=1.0, max_cps=20.0),
            ),
            None,
        )
        == fingerprint
    )
    assert (
        transcription_fingerprint(
            recording, replace(config, resume=False, checkpoint_dir=tmp_path / "elsewhere"), None
        )
        == fingerprint
    )
    for changed in (
        replace(config, model="base"),
        replace(config, language="ja"),
        replace(config, prompt="terms"),
        replace(config, asr_chunk_seconds=60),
        replace(config, asr_profile="soft-speech"),
    ):
        assert transcription_fingerprint(recording, changed, None) != fingerprint
    assert transcription_fingerprint(recording, config, 0) != fingerprint
    monkeypatch.setattr(asr_checkpoints, "local_backend", lambda device: "mlx")
    assert transcription_fingerprint(recording, config, None) != fingerprint
    monkeypatch.setattr(asr_checkpoints, "local_backend", lambda device: "faster-whisper")
    with recording.open("ab") as stream:
        stream.write(b"new source")
    assert transcription_fingerprint(recording, config, None) != fingerprint
    model = tmp_path / "local-model"
    model.mkdir()
    weights = model / "model.bin"
    weights.write_bytes(b"weights")
    config = replace(config, model=str(model))
    fingerprint = transcription_fingerprint(recording, config, None)
    weights.write_bytes(b"new weights")
    assert transcription_fingerprint(recording, config, None) != fingerprint


def test_source_changes_and_simultaneous_writers_are_rejected(recording, tmp_path):
    config = options(tmp_path)
    folder = checkpoint_folder(recording, config)
    engine = RecordingEngine()
    with workspace_lock(folder):
        with pytest.raises(SubtitleError, match="另一任务"):
            pipeline.transcribe(recording, config, engine=engine)
    assert not engine.calls

    def changed(stage, amount, message):
        if stage == "extract":
            with recording.open("ab") as stream:
                stream.write(b"changed")

    with pytest.raises(SubtitleError, match="源文件发生变化"):
        pipeline.transcribe(recording, config, progress=changed, engine=engine)
    assert not engine.calls


def test_resume_preserves_delayed_audio_and_diarization_retries(recording, tmp_path, monkeypatch):
    config = options(tmp_path, diarize=True)
    real_probe = pipeline.probe

    def delayed(source, cancel):
        info = real_probe(source, cancel)
        return replace(info, duration=67, audio_tracks=[replace(info.audio_tracks[0], offset=2)])

    monkeypatch.setattr(pipeline, "probe", delayed)
    calls = []

    def diarize(audio, segments, *args):
        calls.append(True)
        if len(calls) == 1:
            raise SubtitleError("speaker failure")
        return [replace(cue, speaker="A") for cue in segments]

    monkeypatch.setattr("shengmu.speakers.diarize", diarize)
    with pytest.raises(SubtitleError, match="speaker"):
        pipeline.transcribe(recording, config, engine=RecordingEngine())
    engine = RecordingEngine(fail=1)
    result = pipeline.transcribe(recording, config, engine=engine)
    assert not engine.calls and len(calls) == 2
    assert result.duration == 67 and result.segments[0].words[0].start == pytest.approx(2.1)
    assert all(cue.speaker == "A" for cue in result.segments)


def test_cli_resumes_without_partial_exports_and_survives_output_change(
    recording, tmp_path, monkeypatch, capsys
):
    engine = RecordingEngine(fail=2)
    monkeypatch.setattr(pipeline, "get_engine", lambda name: engine)
    first, second = tmp_path / "first", tmp_path / "second"
    args = [
        "transcribe",
        str(recording),
        "--asr-chunk-seconds",
        "30",
        "--format",
        "srt",
        "json",
        "--quiet",
        "--output-dir",
    ]
    assert main(args + [str(first)]) == 1
    assert not list(first.glob("*.srt"))
    capsys.readouterr()
    engine = RecordingEngine()
    assert main(args + [str(second)]) == 0
    captured = capsys.readouterr()
    assert not captured.err and len(engine.calls) == 2
    assert json.loads(captured.out)["transcription"]["restored_chunks"] == 1
    assert len(json.loads((second / "long.json").read_text())["segments"]) == 3
    assert main(args + [str(second)]) == 1 and len(engine.calls) == 2
    capsys.readouterr()
    assert main(args + [str(second), "--overwrite", "--no-resume"]) == 0
    assert json.loads(capsys.readouterr().out)["transcription"]["restored_chunks"] == 0
    assert len(engine.calls) == 5


def test_openai_nested_chunks_preserve_context_and_skip_finished_requests(
    recording, tmp_path, monkeypatch
):
    openai = pytest.importorskip("openai")
    requests = []
    failure = True

    class Client:
        def __init__(self, **kwargs):
            self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, file, **kwargs):
            nonlocal failure
            assert file.read(4) == b"RIFF"
            requests.append(kwargs)
            if len(requests) == 2 and failure:
                failure = False
                raise RuntimeError("request interrupted")
            return SimpleNamespace(
                text="hello",
                language="english",
                words=[],
                segments=[SimpleNamespace(start=0.1, end=0.9, text="hello")],
            )

    monkeypatch.setattr(openai, "OpenAI", Client)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    config = options(tmp_path, engine="openai", chunk_seconds=30)
    with pytest.raises(SubtitleError, match="OpenAI"):
        pipeline.transcribe(recording, config, engine=OpenAIEngine())
    result = pipeline.transcribe(recording, config, engine=OpenAIEngine())
    assert len(requests) == 4 and "hello" in requests[2]["prompt"]
    assert "language" not in requests[2]
    assert result.language == "english" and result.model == "whisper-1"
    assert result.metadata["asr_stats"]["restored_chunks"] == 1


def test_cli_bad_completed_cache_falls_back_to_finished_chunks(
    recording, tmp_path, monkeypatch, capsys
):
    engine = RecordingEngine()
    monkeypatch.setattr(pipeline, "get_engine", lambda name: engine)

    def failed(*args):
        raise SubtitleError("translation failed")

    monkeypatch.setattr("shengmu.cli.translate_for_cli", failed)
    cache = tmp_path / "cache"
    args = [
        "transcribe",
        str(recording),
        "--asr-chunk-seconds",
        "30",
        "--translate-to",
        "en",
        "--translation-model",
        "text",
        "--format",
        "json",
        "--quiet",
        "--checkpoint-dir",
        str(cache),
        "--output-dir",
        str(tmp_path / "out"),
    ]
    assert main(args) == 1 and len(engine.calls) == 3
    capsys.readouterr()
    next(cache.glob("*.source.json")).write_text("{")
    engine = RecordingEngine(fail=1)
    monkeypatch.setattr("shengmu.cli.translate_for_cli", lambda transcript, *args: transcript)
    assert main(args) == 0 and not engine.calls
    assert json.loads(capsys.readouterr().out)["transcription"]["restored_chunks"] == 3


def test_cli_imports_previous_completed_asr_cache_without_rewriting_original(
    recording, tmp_path, monkeypatch, capsys
):
    config = EngineOptions()
    old_options = asdict(config)
    for key in ("asr_chunk_seconds", "resume", "checkpoint_dir", "detected_language", "context"):
        old_options.pop(key)
    state = recording.stat()
    signature = hashlib.sha256(
        json.dumps(
            [str(recording), state.st_size, state.st_mtime_ns, old_options, None], sort_keys=True
        ).encode()
    ).hexdigest()
    output = tmp_path / "out"
    old = output / ".translation-cache" / (signature + ".source.json")
    old.parent.mkdir(parents=True)
    original = Transcript(recording.name, 65, "en", "local", "small", [Segment(0.1, 0.9, "cached")])
    old.write_text(json.dumps(original.to_dict()))
    before = old.read_bytes()
    monkeypatch.setattr(
        pipeline, "get_engine", lambda name: pytest.fail("old ASR cache must be reused")
    )
    monkeypatch.setattr("shengmu.cli.translate_for_cli", lambda transcript, *args: transcript)
    args = [
        "transcribe",
        str(recording),
        "--translate-to",
        "en",
        "--translation-model",
        "text",
        "--format",
        "json",
        "--quiet",
        "--checkpoint-dir",
        str(tmp_path / "new-cache"),
        "--output-dir",
        str(output),
    ]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["segments"] == 1
    assert (
        old.read_bytes() == before
        and len(list((tmp_path / "new-cache").glob("*.source.json"))) == 1
    )


def wait_terminal(client, job_id):
    for _ in range(500):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"done", "error", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_gui_restart_resume_force_retry_and_project_cache_cleanup(recording, tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    failure, engines = True, []

    def create_engine(name):
        engine = RecordingEngine(fail=2 if failure else None)
        engines.append(engine)
        return engine

    monkeypatch.setattr(pipeline, "get_engine", create_engine)
    with TestClient(create_app(workspace_dir=workspace)) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        media = client.post(
            "/api/media?name=long.wav", content=recording.read_bytes(), headers=headers
        ).json()
        request = {"media_id": media["id"], "asr_chunk_seconds": 30, "formats": ["srt", "json"]}
        assert (
            client.post(
                "/api/jobs", json={**request, "asr_chunk_seconds": 0}, headers=headers
            ).status_code
            == 422
        )
        job_id = client.post("/api/jobs", json=request, headers=headers).json()["id"]
        assert wait_terminal(client, job_id)["status"] == "error"
        assert not list((workspace / job_id).glob("*.srt"))
    manifest = workspace / job_id / ".job.json"
    data = json.loads(manifest.read_text())
    data["status"] = "transcribe"  # Simulate a service crash after saving a chunk.
    manifest.write_text(json.dumps(data))
    failure = False
    with TestClient(create_app(workspace_dir=workspace)) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "error"
        assert (
            client.post(f"/api/jobs/{job_id}/retry?resume=true", headers=headers).status_code == 202
        )
        job = wait_terminal(client, job_id)
        assert job["status"] == "done" and len(engines[-1].calls) == 2
        assert job["asr_stats"]["restored_chunks"] == 1
        failure = True
        other_id = client.post("/api/jobs", json=request, headers=headers).json()["id"]
        assert wait_terminal(client, other_id)["status"] == "error"
        failure = False
        assert (
            client.post(f"/api/jobs/{other_id}/retry?resume=false", headers=headers).status_code
            == 202
        )
        restarted = wait_terminal(client, other_id)
        assert restarted["status"] == "done" and len(engines[-1].calls) == 3
        assert restarted["asr_stats"]["restored_chunks"] == 0
        assert restarted["request"]["resume"] is False
        assert client.delete(f"/api/jobs/{job_id}", headers=headers).status_code == 200
        assert not (workspace / job_id / ".asr-cache").exists()

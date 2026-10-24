from __future__ import annotations

import sys
import time
import wave
from contextlib import nullcontext
from threading import Event
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shengmu import devices, model_manager
from shengmu.cli import parser
from shengmu.engines import EngineOptions, LocalEngine
from shengmu.mlx_engine import MLXEngine, resolve_model
from shengmu.models import Cancelled, Segment, SubtitleError, Transcript
from shengmu.server import create_app


@pytest.fixture
def mlx_runtime(tmp_path, monkeypatch):
    vad = pytest.importorskip("faster_whisper.vad")
    calls, loads, clears = [], [], []
    core = ModuleType("mlx.core")
    core.gpu = "gpu"
    core.stream = lambda device: nullcontext()
    core.clear_cache = lambda: clears.append(True)
    core.metal = SimpleNamespace(is_available=lambda: True)
    package = ModuleType("mlx")
    package.core = core
    holder = SimpleNamespace(model=None, model_path=None)
    module = ModuleType("mlx_whisper.transcribe")
    module.ModelHolder = holder
    runtime = ModuleType("mlx_whisper")

    def transcribe(samples, **kwargs):
        calls.append(kwargs)
        if holder.model is None:
            loads.append(kwargs["fp16"])
            holder.model, holder.model_path = "loaded", kwargs["path_or_hf_repo"]
        return {
            "language": "en",
            "segments": [
                {
                    "start": 0.1,
                    "end": 1.8,
                    "text": "Hello world.",
                    "words": [
                        {"start": 0.1, "end": 0.8, "word": "Hello"},
                        {"start": 1.1, "end": 1.8, "word": " world."},
                    ],
                    "avg_logprob": -0.3,
                    "compression_ratio": float("nan"),
                },
                {"start": 0, "end": 1, "text": "字幕由 Amara.org 社区提供"},
            ],
        }

    runtime.transcribe = transcribe
    for name, value in (
        ("mlx", package),
        ("mlx.core", core),
        ("mlx_whisper", runtime),
        ("mlx_whisper.transcribe", module),
    ):
        monkeypatch.setitem(sys.modules, name, value)

    def available():
        return {"supported": True, "installed": True, "available": True}

    monkeypatch.setattr(devices, "mlx_status", available)
    monkeypatch.setattr("shengmu.mlx_engine.mlx_status", available)
    monkeypatch.setattr("shengmu.server.mlx_status", available)
    monkeypatch.setattr(MLXEngine, "_cached_key", None)
    monkeypatch.setattr(LocalEngine, "_cached_key", None)
    monkeypatch.setattr(LocalEngine, "_cached_model", None)
    monkeypatch.setattr("shengmu.mlx_engine.resolve_model", lambda name: "mlx-test-model")
    thresholds = []

    def speech(samples, options, sampling_rate):
        thresholds.append(options.threshold)
        return [{"start": 16000, "end": 32000}, {"start": 64000, "end": 80000}]

    monkeypatch.setattr(vad, "get_speech_timestamps", speech)
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000 * 70)
    return SimpleNamespace(
        audio=audio,
        runtime=runtime,
        holder=holder,
        calls=calls,
        loads=loads,
        clears=clears,
        vad=vad,
        thresholds=thresholds,
    )


def test_mlx_routes_local_and_auto_preserves_words_silence_offsets_and_options(mlx_runtime):
    r = mlx_runtime
    options = EngineOptions(
        device="mlx",
        language="zh",
        asr_profile="less-repetition",
        vad_threshold=0.35,
        log_prob_threshold=-0.8,
        condition_on_previous_text=True,
    )
    options.validate()
    progress = []
    cues, language, model = LocalEngine().transcribe(
        r.audio, options, lambda *p: progress.append(p), None
    )
    assert language == "zh" and model == "small"
    assert len(r.calls) == 2 and r.loads == [True]
    assert cues[0].start == 1.1
    starts = [w.start for cue in cues for w in cue.words]
    assert {1.1, 4.1, 61.1, 64.1} <= set(starts)
    assert all(
        any(
            a <= w.start < w.end <= b
            for a, b in [(1.1, 1.8), (4.1, 4.8), (61.1, 61.8), (64.1, 64.8)]
        )
        for cue in cues
        for w in cue.words
    )
    assert cues[-1].start == 64.1 and cues[-1].end == 64.8
    assert all(not word.estimated for cue in cues for word in cue.words)
    assert all(cue.diagnostics == {"avg_logprob": -0.3} for cue in cues)
    assert all("Amara" not in cue.text for cue in cues)
    assert r.thresholds == [0.35, 0.35]
    assert r.calls[0]["word_timestamps"] and r.calls[0]["fp16"]
    assert r.calls[0]["logprob_threshold"] == -0.8
    assert r.calls[0]["compression_ratio_threshold"] == 2.2
    assert "repetition_penalty" not in options.decoding_parameters()
    assert "普通话" in r.calls[0]["initial_prompt"]
    assert "Hello world." in r.calls[1]["initial_prompt"]
    assert all("vad_filter" not in call and "beam_size" not in call for call in r.calls)
    assert len(progress) >= 5 and progress[-1][0] == 0.99
    LocalEngine().transcribe(r.audio, EngineOptions(device="auto"), lambda *args: None, None)
    assert len(r.calls) == 4


def test_mlx_precision_invalidates_vendor_cache_and_cpu_switch_releases_it(
    mlx_runtime, monkeypatch
):
    r = mlx_runtime
    for compute in ("float16", "float16", "float32"):
        LocalEngine().transcribe(
            r.audio, EngineOptions(device="mlx", compute_type=compute), lambda *args: None, None
        )
    assert r.loads == [True, False]
    monkeypatch.setattr(LocalEngine, "_transcribe", lambda *args: ([], None, "cpu"))
    LocalEngine().transcribe(r.audio, EngineOptions(), lambda *args: None, None)
    assert r.holder.model is None and MLXEngine._cached_key is None
    with pytest.raises(SubtitleError, match="精度"):
        EngineOptions(device="mlx", compute_type="int8").validate()
    args = parser().parse_args(["transcribe", "video.mp4", "--device", "mlx"])
    assert args.compute_type == "auto"


def test_mlx_silence_does_not_invent_speech_and_cancel_discards_result(mlx_runtime, monkeypatch):
    r = mlx_runtime
    monkeypatch.setattr(r.vad, "get_speech_timestamps", lambda *args, **kwargs: [])
    cues, language, _ = LocalEngine().transcribe(
        r.audio, EngineOptions(device="mlx"), lambda *args: None, None
    )
    assert cues == [] and language is None and not r.calls
    monkeypatch.setattr(
        r.vad, "get_speech_timestamps", lambda *args, **kwargs: [{"start": 0, "end": 80000}]
    )
    cancel = Event()
    original = r.runtime.transcribe

    def cancelled(*args, **kwargs):
        result = original(*args, **kwargs)
        cancel.set()
        return result

    monkeypatch.setattr(r.runtime, "transcribe", cancelled)
    with pytest.raises(Cancelled):
        LocalEngine().transcribe(r.audio, EngineOptions(device="mlx"), lambda *args: None, cancel)
    assert len(r.calls) == 1 and MLXEngine._cached_key is None


def test_mlx_errors_do_not_leak_vendor_credentials(mlx_runtime, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private https://secret.example/sk-hidden out of memory")

    monkeypatch.setattr(mlx_runtime.runtime, "transcribe", fail)
    with pytest.raises(SubtitleError, match="内存") as error:
        LocalEngine().transcribe(
            mlx_runtime.audio, EngineOptions(device="mlx"), lambda *args: None, None
        )
    assert "secret" not in str(error.value) and MLXEngine._cached_key is None


def test_mlx_capabilities_reject_unsupported_missing_and_inaccessible_gpu(monkeypatch):
    monkeypatch.setattr(devices, "metal_available", lambda: False)
    monkeypatch.setattr(devices.sys, "platform", "linux")
    assert not devices.mlx_status()["available"]
    monkeypatch.setattr(devices.sys, "platform", "darwin")
    monkeypatch.setattr(devices.platform, "machine", lambda: "x86_64")
    assert not devices.mlx_status()["supported"]
    monkeypatch.setattr(devices.platform, "machine", lambda: "arm64")
    original = devices.importlib.util.find_spec
    monkeypatch.setattr(
        devices.importlib.util,
        "find_spec",
        lambda name: None if name == "mlx_whisper" else original(name),
    )
    status = devices.mlx_status()
    assert not status["installed"] and ".[mlx]" in status["message"]
    monkeypatch.setattr(
        devices.importlib.util,
        "find_spec",
        lambda name: True if name == "mlx_whisper" else original(name),
    )
    monkeypatch.setitem(sys.modules, "mlx.core", None)
    assert not devices.mlx_status()["available"]
    assert devices.local_backend("auto") == "faster-whisper"
    assert devices.local_backend("cpu") == devices.local_backend("cuda") == "faster-whisper"


def test_mlx_model_downloads_are_pinned_and_isolated_from_cpu(tmp_path, monkeypatch):
    hub = pytest.importorskip("huggingface_hub")
    files = [
        SimpleNamespace(rfilename=f, size=10)
        for f in ("config.json", "weights.npz", "model.bin", "tokenizer.json", "README.md")
    ]
    monkeypatch.setattr(
        hub.HfApi,
        "model_info",
        lambda *args, **kwargs: SimpleNamespace(sha="pinned", siblings=files),
    )
    calls = []

    def get(repo, filename, revision, local_dir):
        calls.append((repo, filename, revision))
        (local_dir / filename).write_text("data")

    monkeypatch.setattr(hub, "hf_hub_download", get)
    monkeypatch.setattr(
        hub, "snapshot_download", lambda **kwargs: (_ for _ in ()).throw(OSError("not cached"))
    )
    model_manager.download(tmp_path, "tiny", lambda *args: None, backend="mlx")
    assert calls == [
        (model_manager.MLX_REPOS["tiny"], f, "pinned") for f in ("config.json", "weights.npz")
    ]
    assert model_manager.model_inventory(tmp_path, "mlx")[0]["managed"]
    assert not (tmp_path / "tiny").exists()
    mlx = tmp_path / "mlx" / "tiny"
    assert resolve_model(str(mlx)) == str(mlx)
    cpu = tmp_path / "cpu"
    cpu.mkdir()
    (cpu / "model.bin").write_text("cpu")
    with pytest.raises(SubtitleError, match="CTranslate2"):
        resolve_model(str(cpu))
    with pytest.raises(SubtitleError, match="不存在"):
        resolve_model(str(tmp_path / "missing"))


def test_metal_probe_survives_failed_native_runtime_and_caches_result(monkeypatch):
    calls = []

    def failed(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=-6, stdout="")

    devices.metal_available.cache_clear()
    monkeypatch.setattr(devices, "run", failed)
    assert not devices.metal_available() and not devices.metal_available()
    assert len(calls) == 1
    devices.metal_available.cache_clear()
    monkeypatch.setattr(devices, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError()))
    assert not devices.metal_available()
    devices.metal_available.cache_clear()
    monkeypatch.setattr(
        devices, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="1\n")
    )
    assert devices.metal_available()
    devices.metal_available.cache_clear()


def test_mlx_partial_hub_cache_and_managed_models_are_reused_offline(tmp_path, monkeypatch):
    hub = pytest.importorskip("huggingface_hub")
    monkeypatch.setenv("SHENGMU_MODEL_DIR", str(tmp_path))
    cached = tmp_path / "shared-snapshot"
    cached.mkdir()
    for file in ("config.json", "weights.npz"):
        (cached / file).write_text("model")
    calls = []

    def snapshot(**kwargs):
        calls.append(kwargs)
        # New Hub versions require allow_patterns even for local-only snapshots
        # downloaded without the unrelated README / .gitattributes files.
        assert kwargs["allow_patterns"] == model_manager.MLX_FILES
        return str(cached)

    monkeypatch.setattr(hub, "snapshot_download", snapshot)
    inventory = model_manager.model_inventory(tmp_path / "managed", "mlx")
    assert inventory[0]["downloaded"] and not inventory[0]["managed"]
    assert all(call["local_files_only"] for call in calls)
    calls.clear()
    assert resolve_model("turbo") == str(cached)
    assert calls[0]["repo_id"] == model_manager.MLX_REPOS["turbo"]
    assert calls[0]["cache_dir"] == str(tmp_path / "mlx-hub")
    managed = model_manager.model_path(tmp_path / "managed", "tiny", "mlx")
    managed.mkdir(parents=True)
    for file in ("config.json", "weights.npz", ".complete"):
        (managed / file).write_text("model")
    calls.clear()
    assert resolve_model("tiny") == str(managed) and not calls


def test_gui_mlx_job_uses_managed_gpu_model_and_validates_capabilities(
    media, tmp_path, monkeypatch
):
    root = tmp_path / "models"
    monkeypatch.setenv("SHENGMU_MODEL_DIR", str(root))
    managed = model_manager.model_path(model_manager.model_root(tmp_path), "tiny", "mlx")
    managed.mkdir(parents=True)
    for file in ("config.json", "weights.npz", ".complete"):
        (managed / file).write_text("mlx")
    monkeypatch.setattr(devices, "mlx_status", lambda: {"available": True})
    monkeypatch.setattr("shengmu.server.mlx_status", lambda: {"available": True})
    calls = []

    def transcribe(source, options, *args):
        calls.append(options)
        return Transcript(source.name, 3, "en", "local", options.model, [Segment(0, 1, "test")])

    monkeypatch.setattr("shengmu.server.transcribe", transcribe)
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        upload = client.post(
            "/api/media?name=test.mkv", content=media.read_bytes(), headers=headers
        ).json()
        request = {"media_id": upload["id"], "device": "mlx", "model": "tiny"}
        response = client.post("/api/jobs", json=request, headers=headers)
        assert response.status_code == 202
        for _ in range(100):
            job = client.get("/api/jobs/" + response.json()["id"]).json()
            if job["status"] == "done":
                break
            time.sleep(0.01)
        assert job["status"] == "done" and calls[0].model == str(managed)
        assert calls[0].compute_type == "auto"
        assert client.get("/api/models?backend=mlx").json()[0]["managed"]
        assert client.get("/api/models?backend=other").status_code == 422
        bad = client.post("/api/jobs", json={**request, "compute_type": "int8"}, headers=headers)
        assert bad.status_code == 400 and "精度" in bad.json()["detail"]
        monkeypatch.setattr(
            "shengmu.server.mlx_status", lambda: {"available": False, "message": "GPU 不可用"}
        )
        blocked = client.post("/api/jobs", json=request, headers=headers)
        assert blocked.status_code == 400 and "GPU" in blocked.json()["detail"]
        cpu = managed.parent.parent / "tiny"
        cpu.mkdir()
        (cpu / "keep").write_text("CPU")
        assert client.delete("/api/models/tiny?backend=mlx", headers=headers).status_code == 200
        assert not managed.exists() and (cpu / "keep").is_file()

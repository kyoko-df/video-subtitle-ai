from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shengmu import lmstudio
from shengmu.captions import mark_suspicions, quality_report
from shengmu.cli import main
from shengmu.diagnostics import doctor, ffmpeg_capabilities
from shengmu.engines import EngineOptions
from shengmu.errors import local_model_error
from shengmu.models import Segment, SubtitleError, Transcript
from shengmu.server import create_app
from shengmu.translation import TranslationOptions


def original(count=2):
    return Transcript(
        "video.mp4",
        count + 1,
        "en",
        "local",
        "small",
        [Segment(i, i + 1, f"Line {i}") for i in range(count)],
    )


@pytest.fixture
def local_completion(monkeypatch):
    requests = []

    def completion(model, batch, target, schema, cancel, options):
        requests.append(batch)
        return json.dumps({"translations": [{"id": c["id"], "text": "中文"} for c in batch]})

    monkeypatch.setattr(lmstudio, "completion", completion)
    return requests


def test_cli_translate_and_export_bilingual_preserve_input(tmp_path, local_completion, capsys):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(original().to_dict()))
    before = source.read_bytes()
    output = tmp_path / "out"
    arguments = [
        "translate",
        str(source),
        "--target",
        "zh",
        "--model",
        "text",
        "--output-dir",
        str(output),
        "--quiet",
    ]
    assert main(arguments) == 0
    captured = capsys.readouterr()
    assert not captured.err and json.loads(captured.out)["segments"] == 2
    assert "Line 0\n中文" in (output / "source.translated.srt").read_text()
    assert source.read_bytes() == before
    # Existing output fails before another inference call.
    assert main(arguments) == 1 and len(local_completion) == 1
    capsys.readouterr()
    assert (
        main(
            [
                "export",
                str(output / "source.translated.json"),
                "--export-mode",
                "translated",
                "--output-dir",
                str(tmp_path / "zh"),
            ]
        )
        == 0
    )
    assert "Line 0" not in (tmp_path / "zh/source.translated.srt").read_text()


def test_cli_full_pipeline_restores_asr_after_translation_failure(
    tmp_path, monkeypatch, local_completion, capsys
):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"fake-media")
    calls = []

    def transcribe(*args):
        calls.append(args)
        return original()

    monkeypatch.setattr("shengmu.cli.transcribe", transcribe)
    actual = lmstudio.completion
    monkeypatch.setattr(
        lmstudio,
        "completion",
        lambda *args: (_ for _ in ()).throw(SubtitleError("temporary failure")),
    )
    arguments = [
        "transcribe",
        str(source),
        "--translate-to",
        "zh",
        "--translation-model",
        "text",
        "--format",
        "srt",
        "json",
        "--output-dir",
        str(tmp_path / "out"),
        "--quiet",
    ]
    assert main(arguments) == 1
    assert len(calls) == 1 and not (tmp_path / "out/video.srt").exists()
    capsys.readouterr()
    monkeypatch.setattr(lmstudio, "completion", actual)
    assert main(arguments) == 0 and len(calls) == 1
    assert "Line 0\n中文" in (tmp_path / "out/video.srt").read_text()
    capsys.readouterr()
    # Source replacement invalidates the ASR cache.
    source.write_bytes(b"changed source")
    assert main(arguments + ["--overwrite"]) == 0 and len(calls) == 2


def test_cli_rejects_missing_translation_model_before_asr(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("shengmu.cli.transcribe", lambda *args: pytest.fail("ASR should not run"))
    assert main(["transcribe", str(tmp_path / "video.mp4"), "--translate-to", "zh"]) == 1
    assert "--translation-model" in capsys.readouterr().err


@pytest.mark.parametrize(
    "options",
    [
        TranslationOptions(concurrency=3),
        TranslationOptions(retries=4),
        TranslationOptions(batch_size=0),
        TranslationOptions(timeout=float("nan")),
        TranslationOptions(max_tokens=127),
    ],
)
def test_invalid_translation_options_rejected(options):
    with pytest.raises(SubtitleError):
        options.validate()


@pytest.mark.parametrize(
    "profile, expected",
    [
        ("standard", (2.4, 0.6, 0.5)),
        ("less-repetition", (2.2, 0.6, 0.5)),
        ("soft-speech", (2.4, 0.8, 0.35)),
    ],
)
def test_asr_profiles_and_overrides(profile, expected):
    options = EngineOptions(asr_profile=profile)
    options.validate()
    params = options.decoding_parameters()
    assert (
        params["compression_ratio_threshold"],
        params["no_speech_threshold"],
        params["vad_parameters"]["threshold"],
    ) == expected
    overridden = replace(options, no_speech_threshold=0.7, vad_threshold=0.4)
    assert overridden.decoding_parameters()["no_speech_threshold"] == 0.7
    assert overridden.decoding_parameters()["vad_parameters"]["threshold"] == 0.4


@pytest.mark.parametrize(
    "error, message",
    [
        (PermissionError("private-path"), "权限"),
        (FileNotFoundError("private-path"), "不存在"),
        (RuntimeError("CUDA out of memory secret"), "内存"),
        (RuntimeError("cudnn_ops_infer64_9.dll private-path"), "cuDNN"),
        (RuntimeError("offline mode secret"), "离线"),
    ],
)
def test_local_error_classification_redacts_details(error, message):
    outer = RuntimeError("generic")
    outer.__cause__ = error
    displayed = str(local_model_error(outer))
    assert message in displayed and "secret" not in displayed and "private-path" not in displayed


def test_network_download_cause_is_not_a_device_error():
    class ConnectTimeout(Exception):
        pass

    error = RuntimeError("outer")
    error.__cause__ = ConnectTimeout("secret")
    assert "下载服务" in str(local_model_error(error))


def test_repetition_flags_preserve_real_dialogue_and_roundtrip():
    sequence = [Segment(i, i + 1, "サンプル"[i % 4]) for i in range(10)]
    real = Segment(20, 22, "テストはまだです")
    marked = mark_suspicions([*sequence, real])
    assert (
        len(marked) == 11 and all(c.suspicions for c in marked[:10]) and not marked[-1].suspicions
    )
    assert not any(c.suspicions for c in sequence)
    value = replace(original(), duration=24, segments=marked)
    assert Transcript.from_dict(value.to_dict()).segments == marked
    assert len(quality_report(marked)) >= 10
    # Repeated words with confident recognition do not trigger the group heuristic.
    confident = [
        Segment(i, i + 1, "はい", diagnostics={"avg_logprob": -0.1, "no_speech_prob": 0.01})
        for i in range(6)
    ]
    assert not any(c.suspicions for c in mark_suspicions(confident))
    weak = [replace(c, diagnostics={"avg_logprob": -1.2}) for c in confident]
    assert all(c.suspicions for c in mark_suspicions(weak))


def test_doctor_filters_and_no_network_by_default(monkeypatch):
    ffmpeg_capabilities.cache_clear()
    calls = []

    def run(args, **kwargs):
        calls.append(args[-1])
        return SimpleNamespace(
            returncode=0,
            stdout=" T.C ass desc\n T.. scale desc"
            if args[-1] == "-filters"
            else " V..... libx264 desc\n A..... aac desc\n S..... mov_text desc",
        )

    monkeypatch.setattr("shengmu.diagnostics.subprocess.run", run)
    monkeypatch.setattr(
        lmstudio, "models", lambda: pytest.fail("default doctor must not contact LM Studio")
    )
    result = doctor()
    assert result["ffmpeg_capabilities"]["burn_supported"]
    assert result["ffmpeg_capabilities"]["soft_supported"]
    assert "huggingface" not in result
    doctor()
    assert calls == ["-filters", "-encoders"]
    ffmpeg_capabilities.cache_clear()


def test_doctor_missing_ass_and_explicit_network(monkeypatch):
    ffmpeg_capabilities.cache_clear()
    monkeypatch.setattr(
        "shengmu.diagnostics.subprocess.run",
        lambda args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=" A..... aac desc\n S..... mov_text desc\n V..... libx264 desc"
            if args[-1] == "-encoders"
            else " T.C scale desc",
        ),
    )
    monkeypatch.setenv("HF_ENDPOINT", "http://[")
    monkeypatch.setattr(lmstudio, "models", lambda: ["text"])
    result = doctor(network=True)
    assert result["ffmpeg_capabilities"]["missing_burn"] == ["ass"]
    assert (
        not result["ffmpeg_capabilities"]["burn_supported"]
        and result["ffmpeg_capabilities"]["soft_supported"]
    )
    assert result["huggingface"]["reachable"] is False and result["lmstudio"]["text_models"] == 1
    ffmpeg_capabilities.cache_clear()


def test_telemetry_default_is_set_before_optional_engine_import(tmp_path):
    import os

    env = dict(os.environ)
    env.pop("ORT_DISABLE_TELEMETRY", None)
    env["PYTHONPATH"] = str(__import__("pathlib").Path(__file__).resolve().parents[1] / "src")
    code = "import sys, os; import shengmu; assert os.environ['ORT_DISABLE_TELEMETRY'] == '1'; assert 'faster_whisper' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path, check=True)
    assert list(tmp_path.iterdir()) == []


def test_interrupted_operation_retains_resume_settings_and_progress(media, tmp_path, monkeypatch):
    from shengmu.storage import atomic_json

    monkeypatch.setattr("shengmu.server.transcribe", lambda *args: original())
    workspace = tmp_path / "workspace"
    with TestClient(create_app(workspace_dir=workspace)) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        media_id = client.post(
            "/api/media/link", json={"path": str(media)}, headers=headers
        ).json()["id"]
        job_id = client.post("/api/jobs", json={"media_id": media_id}, headers=headers).json()["id"]
        import time

        for _ in range(100):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "done":
                break
            time.sleep(0.01)
        assert job["status"] == "done"
    manifest = workspace / job_id / ".job.json"
    value = json.loads(manifest.read_text())
    value["operation"] = {
        "kind": "translate",
        "status": "running",
        "progress": 0.5,
        "settings": {"model": "text"},
        "stats": {"completed_cues": 1},
    }
    atomic_json(manifest, value)
    with TestClient(create_app(workspace_dir=workspace)) as client:
        restored = client.get(f"/api/jobs/{job_id}").json()
        assert restored["status"] == "done" and restored["operation"]["status"] == "error"
        assert (
            restored["operation"]["interrupted"]
            and restored["operation"]["stats"]["completed_cues"] == 1
        )
        assert restored["operation"]["settings"]["model"] == "text"


@pytest.mark.parametrize(
    "status, code, private_message, expected_code, splittable",
    [
        (400, None, "maximum context length private", "context", True),
        (401, None, "private", "auth", False),
        (429, "insufficient_quota", "private", "quota", False),
    ],
)
def test_openai_context_auth_and_quota_are_classified(
    monkeypatch, status, code, private_message, expected_code, splittable
):
    import openai

    from shengmu.errors import TranslationError
    from shengmu.translation import openai_completion

    error = RuntimeError(private_message)
    error.status_code, error.code = status, code

    class Client:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(create=self.create)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, **kwargs):
            raise error

    monkeypatch.setattr(openai, "OpenAI", Client)
    with pytest.raises(TranslationError) as caught:
        openai_completion("text", [{"id": 0, "text": "Hello"}], "zh", TranslationOptions(), None)
    assert caught.value.code == expected_code and caught.value.splittable == splittable
    assert not caught.value.retryable and "private" not in str(caught.value)

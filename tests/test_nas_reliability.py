from __future__ import annotations

import errno
import json
import time
from collections import Counter
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from shengmu import lmstudio
from shengmu.cache import checkpoint_directory
from shengmu.captions import mark_suspicions
from shengmu.cli import ProgressPrinter, main
from shengmu.diagnostics import doctor, ensure_output_directory, output_capabilities
from shengmu.errors import TranslationError
from shengmu.exporters import export_files
from shengmu.models import Segment, SubtitleError, Transcript, Word, shift_segment
from shengmu.pipeline import transcribe_to_files
from shengmu.server import create_app
from shengmu.storage import copy_exclusive
from shengmu.translation import TranslationOptions, checkpoint_path, translate


def transcript(count=2):
    return Transcript(
        "sample.mp4",
        count + 1,
        "en",
        "local",
        "tiny",
        [Segment(i, i + 1, f"Line {i}") for i in range(count)],
    )


@pytest.fixture
def completion(monkeypatch):
    calls = []

    def complete(model, batch, target, schema, cancel, options):
        calls.append(batch)
        return json.dumps({"translations": [{"id": c["id"], "text": "译文"} for c in batch]})

    monkeypatch.setattr(lmstudio, "completion", complete)
    return calls


@pytest.mark.parametrize(
    "code", sorted({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.EXDEV, errno.ENOSYS})
)
def test_export_without_links_still_protects_existing_outputs(tmp_path, monkeypatch, code):
    def unsupported(*args):
        raise OSError(code, "unsupported")

    monkeypatch.setattr("shengmu.exporters.os.link", unsupported)
    paths = export_files(transcript(), tmp_path, "字幕", ["srt", "json"])
    assert "Line 0" in paths[0].read_text(encoding="utf-8")
    assert len(json.loads(paths[1].read_text(encoding="utf-8"))["segments"]) == 2
    previous = paths[0].read_bytes()
    with pytest.raises(SubtitleError, match="输出已存在"):
        export_files(transcript(), tmp_path, "字幕", ["srt"])
    assert paths[0].read_bytes() == previous
    assert not list(tmp_path.glob(".shengmu-*"))


@pytest.mark.parametrize("fallback", [False, True])
def test_export_concurrent_writer_never_gets_replaced(tmp_path, monkeypatch, fallback):
    target = tmp_path / "字幕.srt"

    def other_writer(*args):
        target.write_bytes(b"another writer")
        if fallback:
            raise OSError(errno.ENOTSUP, "unsupported")
        raise FileExistsError(errno.EEXIST, "exists")

    monkeypatch.setattr("shengmu.exporters.os.link", other_writer)
    with pytest.raises(SubtitleError, match="输出已存在"):
        export_files(transcript(), tmp_path, "字幕", ["srt"])
    assert target.read_bytes() == b"another writer"
    assert list(tmp_path.iterdir()) == [target]


def test_failed_copy_removes_only_its_partial_output(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"whole file")

    def broken_copy(reader, writer):
        writer.write(b"partial")
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr("shengmu.storage.shutil.copyfileobj", broken_copy)
    with pytest.raises(OSError, match="disk full"):
        copy_exclusive(source, target)
    assert not target.exists() and source.read_bytes() == b"whole file"
    target.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        copy_exclusive(source, target)
    assert target.read_bytes() == b"existing"


def test_unrelated_export_failure_is_not_silently_retried(tmp_path, monkeypatch):
    def full(*args):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr("shengmu.exporters.os.link", full)
    with pytest.raises(OSError, match="disk full"):
        export_files(transcript(), tmp_path, "sample", ["srt"])
    assert not list(tmp_path.iterdir())


def test_copy_close_failure_also_cleans_partial_target(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"whole file")
    original_open = Path.open

    class CloseFailure:
        def __init__(self, stream):
            self.stream = stream

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()
            raise OSError("close failed")

    def faulty_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return CloseFailure(stream) if path == target else stream

    monkeypatch.setattr(Path, "open", faulty_open)
    with pytest.raises(OSError, match="close failed"):
        copy_exclusive(source, target)
    assert not target.exists()


def test_output_probe_and_doctor_clean_up_and_allow_smb_fallback(tmp_path, monkeypatch, capsys):
    existing = tmp_path / "keep"
    existing.write_bytes(b"original")
    capabilities = output_capabilities(tmp_path)
    assert all(
        capabilities[k] for k in ("writable", "atomic_replace", "hard_link", "exclusive_create")
    )

    def unsupported(*args):
        raise OSError(errno.ENOTSUP, "unsupported")

    monkeypatch.setattr("shengmu.diagnostics.os.link", unsupported)
    capabilities = ensure_output_directory(tmp_path)
    assert not capabilities["hard_link"] and capabilities["exclusive_create"]
    assert "独占创建" in capabilities["note"] and "必须使用" not in capabilities["note"]
    assert main(["doctor", "--output-dir", str(tmp_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["output_capabilities"]["exclusive_create"]
    assert list(tmp_path.iterdir()) == [existing] and existing.read_bytes() == b"original"
    assert "output_capabilities" not in doctor()


def test_output_probe_failure_cleans_up_and_rejects_before_asr(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr("shengmu.diagnostics.Path.write_bytes", denied)
    assert not output_capabilities(tmp_path)["writable"]
    assert not list(tmp_path.iterdir())
    monkeypatch.setattr("shengmu.pipeline.transcribe", lambda *args: pytest.fail("ASR started"))
    with pytest.raises(SubtitleError, match="输出目录无法写入"):
        transcribe_to_files(tmp_path / "input.mp4", None, tmp_path, ["srt"])


def test_replace_is_required_only_for_overwrite(tmp_path, monkeypatch):
    def unavailable(*args):
        raise OSError(errno.ENOTSUP, "replace unavailable")

    monkeypatch.setattr("shengmu.diagnostics.os.replace", unavailable)
    assert ensure_output_directory(tmp_path)["writable"]
    with pytest.raises(SubtitleError, match="不支持文件替换"):
        ensure_output_directory(tmp_path, overwrite=True)
    assert not list(tmp_path.iterdir())


def test_no_safe_output_creation_is_rejected(tmp_path, monkeypatch):
    def unavailable(*args):
        raise OSError(errno.ENOTSUP, "unavailable")

    monkeypatch.setattr("shengmu.diagnostics.os.link", unavailable)
    monkeypatch.setattr("shengmu.diagnostics.copy_exclusive", unavailable)
    with pytest.raises(SubtitleError, match="安全的不覆盖导出"):
        ensure_output_directory(tmp_path)
    assert not list(tmp_path.iterdir())


def test_cli_bad_output_directory_prevents_any_inference(tmp_path, monkeypatch, completion, capsys):
    source, output = tmp_path / "input.json", tmp_path / "output"
    source.write_text(json.dumps(transcript().to_dict()), encoding="utf-8")
    output.write_bytes(b"existing file")
    assert main(cli_args(source, output)) == 1
    assert "输出目录" in capsys.readouterr().err and not completion
    monkeypatch.setattr("shengmu.cli.transcribe", lambda *args: pytest.fail("ASR started"))
    assert (
        main(
            [
                "transcribe",
                str(source),
                "--translate-to",
                "zh",
                "--translation-model",
                "text",
                "--output-dir",
                str(output),
            ]
        )
        == 1
    )
    assert "输出目录" in capsys.readouterr().err and output.read_bytes() == b"existing file"


def test_local_cache_default_environment_and_explicit_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("SHENGMU_CHECKPOINT_DIR")
    monkeypatch.setattr(
        "shengmu.cache.user_cache_dir", lambda *args, **kwargs: str(tmp_path / "os-cache")
    )
    assert checkpoint_directory() == tmp_path / "os-cache/checkpoints"
    monkeypatch.setenv("SHENGMU_CHECKPOINT_DIR", str(tmp_path / "env"))
    assert checkpoint_directory() == tmp_path / "env"
    assert checkpoint_directory(tmp_path / "explicit") == tmp_path / "explicit"


def cli_args(source, output):
    return [
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


def test_cli_resume_survives_changing_output_directory(tmp_path, completion, capsys):
    source = tmp_path / "input.json"
    source.write_text(json.dumps(transcript().to_dict()), encoding="utf-8")
    for directory in [tmp_path / "first", tmp_path / "second"]:
        assert main(cli_args(source, directory)) == 0
        captured = capsys.readouterr()
        assert not captured.err
        stats = json.loads(captured.out)["translation"]
    assert len(completion) == 1 and stats["restored_cues"] == 2
    assert not list((tmp_path / "first").glob(".translation-cache"))
    assert not list((tmp_path / "second").glob(".translation-cache"))
    assert (
        main(cli_args(source, tmp_path / "third") + ["--checkpoint-dir", str(tmp_path / "custom")])
        == 0
    )
    assert len(completion) == 2 and list((tmp_path / "custom").glob("*.json"))


def test_legacy_translation_cache_migrates_without_modifying_original(tmp_path, completion, capsys):
    original = transcript()
    output = tmp_path / "output"
    legacy = checkpoint_path(output, original, "zh", "text", "lmstudio", TranslationOptions())
    translate(original, "zh", "text", provider="lmstudio", checkpoint=legacy)
    before = legacy.read_bytes()
    source = tmp_path / "input.json"
    source.write_text(json.dumps(original.to_dict()), encoding="utf-8")
    assert main(cli_args(source, output)) == 0
    assert json.loads(capsys.readouterr().out)["translation"]["restored_cues"] == 2
    assert len(completion) == 1 and legacy.read_bytes() == before
    assert main(cli_args(source, output) + ["--overwrite", "--no-resume"]) == 0
    assert len(completion) == 2 and legacy.read_bytes() == before


def test_precanonical_partial_checkpoint_with_integer_times_is_restored(
    tmp_path, completion, capsys
):
    original = transcript()
    output = tmp_path / "output"
    legacy = checkpoint_path(
        output, original, "zh", "text", "lmstudio", TranslationOptions(), legacy=True
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps({"version": 1, "fingerprint": legacy.stem, "translations": {"0": "已完成"}}),
        encoding="utf-8",
    )
    before = legacy.read_bytes()
    source = tmp_path / "input.json"
    source.write_text(json.dumps(original.to_dict()), encoding="utf-8")
    assert main(cli_args(source, output)) == 0
    stats = json.loads(capsys.readouterr().out)["translation"]
    assert stats["restored_cues"] == 1 and len(completion) == 1
    assert completion[0] == [{"id": 1, "text": "Line 1"}]
    assert legacy.read_bytes() == before


def test_gui_restores_old_checkpoint_after_restart_and_preflights_before_translation(
    tmp_path, media, monkeypatch, completion
):
    monkeypatch.setattr("shengmu.server.transcribe", lambda *args: transcript())
    root = tmp_path / "workspace"
    with TestClient(create_app(workspace_dir=root)) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        source = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
        ).json()
        job_id = client.post("/api/jobs", json={"media_id": source["id"]}, headers=headers).json()[
            "id"
        ]
        for _ in range(400):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "done":
                break
            time.sleep(0.01)
        assert job["status"] == "done"
        original = client.app.state.workspace.jobs[job_id].transcript
        old = checkpoint_path(
            root / job_id, original, "zh", "text", "lmstudio", TranslationOptions(), legacy=True
        )
        old.parent.mkdir(parents=True)
        old.write_text(
            json.dumps({"version": 1, "fingerprint": old.stem, "translations": {"0": "已完成"}}),
            encoding="utf-8",
        )
    before = old.read_bytes()
    with TestClient(create_app(workspace_dir=root)) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        endpoint = f"/api/jobs/{job_id}/translate"
        settings = {"provider": "lmstudio", "target": "zh", "model": "text"}
        assert client.post(endpoint, json=settings, headers=headers).status_code == 202
        for _ in range(400):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["operation"].get("status") in {"done", "error"}:
                break
            time.sleep(0.01)
        assert job["operation"]["status"] == "done"
        assert job["operation"]["stats"]["restored_cues"] == 1
        assert completion == [[{"id": 1, "text": "Line 1"}]]
        assert old.read_bytes() == before

        def denied(*args, **kwargs):
            raise SubtitleError("输出目录无法写入")

        monkeypatch.setattr("shengmu.server.ensure_output_directory", denied)
        response = client.post(endpoint, json=settings, headers=headers)
        assert response.status_code == 400 and "输出目录" in response.json()["detail"]
        assert len(completion) == 1


def test_asr_and_translation_caches_survive_new_output_and_legacy_location(
    tmp_path, monkeypatch, completion, capsys
):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"media")
    asr_calls = []
    monkeypatch.setattr(
        "shengmu.cli.transcribe", lambda *args: asr_calls.append(args) or transcript()
    )
    args = [
        "transcribe",
        str(source),
        "--translate-to",
        "zh",
        "--translation-model",
        "text",
        "--format",
        "json",
        "srt",
        "--quiet",
        "--output-dir",
    ]
    first, second = tmp_path / "first", tmp_path / "second"
    assert main(args + [str(first)]) == 0
    capsys.readouterr()
    cache_root = checkpoint_directory()
    legacy = first / ".translation-cache"
    legacy.mkdir()
    for cache in cache_root.glob("*.json"):
        cache.rename(legacy / cache.name)
    assert main(args + [str(first), "--overwrite"]) == 0
    capsys.readouterr()
    assert main(args + [str(second)]) == 0
    stats = json.loads(capsys.readouterr().out)["translation"]
    assert len(asr_calls) == 1 and len(completion) == 1 and stats["restored_cues"] == 2
    assert len(list(legacy.glob("*.json"))) == 2 and not (second / ".translation-cache").exists()


def test_progress_printer_limits_noise_but_keeps_final_and_recovery_events(monkeypatch, capsys):
    now = 0
    monkeypatch.setattr("shengmu.cli.time.monotonic", lambda: now)
    printer = ProgressPrinter()
    for index in range(200):
        now = index / 100
        printer("translate", index / 200, f"完成 {index} 条")
    printer("translate", 0.99, "临时故障，正在重试")
    printer("translate", 1, "完成")
    for _ in range(100):
        printer("translate", 1, "完成")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 3 and "重试" in lines[-2] and "100%" in lines[-1]
    ProgressPrinter(quiet=True)("done", 1, "完成")
    assert not capsys.readouterr().err


def test_translation_waiting_heartbeats_are_bounded_and_do_not_hide_completion(monkeypatch):
    ready = Event()
    stats, progress = [], []

    def completion(model, batch, *args):
        assert ready.wait(6), "missing waiting heartbeat"
        return json.dumps({"translations": [{"id": c["id"], "text": "译文"} for c in batch]})

    def details(snapshot):
        stats.append(snapshot)
        if snapshot["elapsed_seconds"] >= 2 and snapshot["running_batches"]:
            ready.set()

    monkeypatch.setattr(lmstudio, "completion", completion)
    result = translate(
        transcript(),
        "zh",
        "text",
        provider="lmstudio",
        progress=lambda *args: progress.append(args),
        details=details,
    )
    assert all(c.translation for c in result.segments)
    assert stats[-1]["completed_cues"] == 2 and stats[-1]["running_batches"] == 0
    assert len([s for s in stats if s["running_batches"] and s["elapsed_seconds"] < 1.5]) == 1
    assert max(Counter(progress).values()) <= 2


@pytest.mark.parametrize("capabilities", [["tool_use"], None, {}, {"reasoning": ["off"]}])
def test_v0_capabilities_shapes_are_not_treated_as_v1_reasoning(monkeypatch, capabilities):
    def inventory(endpoint):
        if endpoint == "/api/v1/models":
            raise TranslationError("unsupported", status=404)
        return {
            "data": [{"id": "text", "type": "llm", "state": "loaded", "capabilities": capabilities}]
        }

    monkeypatch.setattr(lmstudio, "request_json", inventory)
    (entry,) = lmstudio.model_inventory()
    assert entry["id"] == "text" and entry["loaded"] and entry["reasoning_options"] == []


@pytest.mark.parametrize(
    "capabilities",
    [
        None,
        ["tool_use"],
        {"reasoning": None},
        {"reasoning": []},
        {"reasoning": {"allowed_options": "off"}},
    ],
)
def test_optional_v1_reasoning_shape_does_not_break_inventory(monkeypatch, capabilities):
    monkeypatch.setattr(
        lmstudio,
        "request_json",
        lambda endpoint: {"models": [{"key": "text", "type": "llm", "capabilities": capabilities}]},
    )
    assert lmstudio.model_inventory()[0]["reasoning_options"] == []


@pytest.mark.parametrize("profile", ["standard", "less-repetition"])
def test_real_asr_loop_fixtures_are_marked_without_deleting_or_rewriting(profile):
    data = json.loads(
        (Path(__file__).parent / "fixtures" / f"asr-loop-{profile}.json").read_text(
            encoding="utf-8"
        )
    )
    original = Transcript.from_dict(data)
    before = original.to_dict()
    for offset in [0, 900]:
        cues = [shift_segment(c, offset) for c in original.segments]
        marked = mark_suspicions(cues)
        indices = data["expected_loop_indices"]
        assert all("疑似重复循环，请试听确认" in marked[i].suspicions for i in indices)
        assert not any(c.suspicions for c in marked[max(indices) + 2 :])
        assert [(c.start, c.end, c.text, c.words, c.diagnostics) for c in marked] == [
            (c.start, c.end, c.text, c.words, c.diagnostics) for c in cues
        ]
        assert mark_suspicions(marked) == marked
    assert original.to_dict() == before


@pytest.mark.parametrize("text", ["あ゛ー", "あーー", "嗯——", "Ah...", "hmmm"])
def test_real_drawn_out_vocalizations_are_not_marked(text):
    cue = Segment(0, 6, text, [Word(0, 6, text, estimated=True)])
    assert not mark_suspicions([cue])[0].suspicions


def test_estimated_slow_fragment_is_timing_warning_and_regular_repetition_is_preserved():
    cue = Segment(0, 7, "字", [Word(0, 7, "字", estimated=True)])
    marked = mark_suspicions([cue])[0]
    assert any("时间轴" in reason for reason in marked.suspicions)
    assert not any("循环" in reason for reason in marked.suspicions)
    responses = [Segment(i * 15, i * 15 + 1, "谢谢你") for i in range(8)]
    assert not any(c.suspicions for c in mark_suspicions(responses))
    # A shared alphabet, without ordered word fragments, is not a loop.
    anagrams = [
        Segment(i * 15, i * 15 + 1, text)
        for i, text in enumerate(["abcd", "dcba", "abcd", "bdac", "abcd", "dacb", "abcd"])
    ]
    assert not any(c.suspicions for c in mark_suspicions(anagrams))

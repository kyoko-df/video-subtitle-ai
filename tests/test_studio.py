from __future__ import annotations

import io
import json
import time
import zipfile
from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shengmu.captions import CaptionOptions, quality_report, split_caption
from shengmu.exporters import render
from shengmu.media import run_process
from shengmu.models import Cancelled, Segment, SubtitleError, Transcript, Word, shift_segment
from shengmu.server import create_app
from shengmu.speakers import assign_speakers, diarize
from shengmu.storage import workspace_lock
from shengmu.translation import translate


def fake_transcribe(source, options, track, progress, cancel):
    return Transcript(
        source.name,
        3,
        "en",
        options.engine,
        "fixture",
        [
            Segment(
                0.2,
                1.5,
                "Hello world",
                [Word(0.2, 0.7, "Hello"), Word(0.7, 1.5, " world")],
                "Speaker 1",
            )
        ],
    )


def setup_job(client, media, **options):
    headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
    uploaded = client.post(
        "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
    )
    assert uploaded.status_code == 200, uploaded.text
    job = client.post(
        "/api/jobs",
        json={"media_id": uploaded.json()["id"], "formats": ["srt", "ass", "json"], **options},
        headers=headers,
    )
    assert job.status_code == 202, job.text
    return headers, uploaded.json()["id"], job.json()["id"]


def wait_job(client, job_id, operation=False):
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        status = job["operation"].get("status") if operation else job["status"]
        if status in {"done", "error", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_project_draft_preferences_and_media_survive_restart(media, tmp_path, monkeypatch):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)
    root = tmp_path / "workspace"
    with TestClient(create_app(workspace_dir=root)) as client:
        headers, media_id, job_id = setup_job(client, media)
        assert wait_job(client, job_id)["status"] == "done"
        draft = {
            "revision": 0,
            "segments": [{"start": None, "end": 2, "text": "draft", "speaker": "Alice"}],
            "style": {"size": 70},
        }
        assert (
            client.put(f"/api/jobs/{job_id}/draft", json=draft, headers=headers).status_code == 200
        )
        assert (
            client.put(
                "/api/preferences", json={"target_language": "ja"}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.get(f"/api/media/{media_id}/file", headers={"Range": "bytes=0-9"}).status_code
            == 206
        )
    with TestClient(create_app(workspace_dir=root)) as client:
        job = client.get("/api/jobs").json()[0]
        assert job["id"] == job_id and job["draft"] and job["media_available"]
        assert client.get(f"/api/jobs/{job_id}/draft").json()["segments"][0]["start"] is None
        assert client.get("/api/preferences").json()["target_language"] == "ja"
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        cues = client.get(f"/api/jobs/{job_id}/transcript").json()["segments"]
        assert cues[0]["words"][0]["text"] == "Hello"
        assert (
            client.put(
                f"/api/jobs/{job_id}/transcript",
                json={"revision": 0, "segments": cues},
                headers=headers,
            ).status_code
            == 200
        )
        assert client.get(f"/api/jobs/{job_id}/draft").json() is None
        assert (
            client.put(f"/api/jobs/{job_id}/draft", json=draft, headers=headers).status_code == 409
        )
        assert client.get("/api/media").json()[0]["id"] == media_id
        assert client.get(f"/api/media/{media_id}").json()["url"].endswith("/file")
        client.app.state.workspace.cleanup()
        assert client.get(f"/api/jobs/{job_id}").status_code == 200


def test_interrupted_task_recovery_and_retry(media, tmp_path, monkeypatch):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)
    root = tmp_path / "workspace"
    with TestClient(create_app(workspace_dir=root)) as client:
        _, _, job_id = setup_job(client, media)
        assert wait_job(client, job_id)["status"] == "done"
    manifest = root / job_id / ".job.json"
    data = json.loads(manifest.read_text())
    data.update(status="transcribe", files=[], transcript=None, operation={"status": "running"})
    manifest.write_text(json.dumps(data))
    with TestClient(create_app(workspace_dir=root)) as client:
        job = client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "error" and job["operation"] == {}
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        assert client.post(f"/api/jobs/{job_id}/retry", headers=headers).status_code == 202
        assert wait_job(client, job_id)["status"] == "done"
        assert client.post(f"/api/jobs/{job_id}/retry", headers=headers).status_code == 409


def test_workspace_lock_and_corrupt_manifest(tmp_path):
    root = tmp_path / "workspace"
    with workspace_lock(root), pytest.raises(SubtitleError, match="已有"):
        with workspace_lock(root):
            pass
    (root / "broken").mkdir()
    (root / "broken" / ".job.json").write_text("invalid")
    (root / "broken" / ".media.json").write_text("invalid")
    with TestClient(create_app(workspace_dir=root)) as client:
        assert client.get("/api/jobs").json() == []


def test_zip_waveform_reflow_and_delete(media, monkeypatch):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)
    with TestClient(create_app()) as client:
        headers, media_id, job_id = setup_job(client, media)
        assert wait_job(client, job_id)["status"] == "done"
        wav = client.get(f"/api/media/{media_id}/waveform?track=2").json()
        assert 1 <= len(wav["peaks"]) <= 2000 and max(wav["peaks"]) > 0
        assert client.get(f"/api/media/{media_id}/waveform?track=2").json() == wav
        assert client.get(f"/api/media/{media_id}/waveform?track=99").status_code == 400
        archive = client.get(f"/api/downloads.zip?ids={job_id}")
        with zipfile.ZipFile(io.BytesIO(archive.content)) as z:
            assert len(z.namelist()) == 3
            assert any(
                "Hello" in z.read(name).decode() for name in z.namelist() if name.endswith(".srt")
            )
        assert not list(client.app.state.workspace.root.glob("download-*.zip"))
        assert (
            client.post(
                f"/api/jobs/{job_id}/reflow",
                json={"latin_chars": 5, "max_lines": 1},
                headers=headers,
            ).status_code
            == 200
        )
        cues = client.get(f"/api/jobs/{job_id}/transcript").json()["segments"]
        assert len(cues) == 2 and all(c["speaker"] == "Speaker 1" for c in cues)
        assert isinstance(client.get(f"/api/jobs/{job_id}/quality").json(), list)
        assert client.delete(f"/api/jobs/{job_id}", headers=headers).status_code == 200
        assert client.get(f"/api/jobs/{job_id}").status_code == 404
        assert client.get("/api/downloads.zip").status_code == 400
        assert client.get(f"/api/media/{media_id}/file").status_code == 200


@pytest.mark.parametrize("mode", ["burn", "soft"])
def test_real_video_export(media, monkeypatch, mode):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)
    with TestClient(create_app()) as client:
        headers, _, job_id = setup_job(client, media)
        assert wait_job(client, job_id)["status"] == "done"
        assert (
            client.post(
                f"/api/jobs/{job_id}/video", json={"mode": mode}, headers=headers
            ).status_code
            == 202
        )
        job = wait_job(client, job_id, True)
        assert job["operation"]["status"] == "done", job
        result = client.get(job["operation"]["url"])
        assert result.status_code == 200 and len(result.content) > 1000
        path = client.app.state.workspace.get_job(job_id).directory / f"video-{mode}.mp4"
        info = json.loads(
            run_process(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)])
        )
        assert len([s for s in info["streams"] if s["codec_type"] == "audio"]) == 2
        if mode == "soft":
            assert any(
                s["codec_type"] == "subtitle" and s["codec_name"] == "mov_text"
                for s in info["streams"]
            )
        assert not list(path.parent.glob("shengmu-render-*"))


def test_operation_locks_edit_source_release_and_cancel(media, monkeypatch):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)
    started, finish = Event(), Event()

    def blocked(*args):
        started.set()
        assert finish.wait(5)
        raise Cancelled("cancelled")

    monkeypatch.setattr("shengmu.server.translate", blocked)
    with TestClient(create_app()) as client:
        headers, media_id, job_id = setup_job(client, media)
        assert wait_job(client, job_id)["status"] == "done"
        assert (
            client.post(
                f"/api/jobs/{job_id}/translate", json={"target": "ja"}, headers=headers
            ).status_code
            == 202
        )
        try:
            assert started.wait(5)
            assert client.delete(f"/api/media/{media_id}", headers=headers).status_code == 409
            assert client.delete(f"/api/jobs/{job_id}", headers=headers).status_code == 409
            assert (
                client.put(
                    f"/api/jobs/{job_id}/transcript", json={"segments": []}, headers=headers
                ).status_code
                == 409
            )
            assert (
                client.post(f"/api/jobs/{job_id}/operation/cancel", headers=headers).status_code
                == 200
            )
        finally:
            finish.set()
        assert wait_job(client, job_id, True)["operation"]["status"] == "cancelled"


def test_translation_api_and_error_leave_original_unchanged(media, monkeypatch):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)

    def translated(t, *args):
        return replace(t, segments=[replace(c, translation="你好世界") for c in t.segments])

    monkeypatch.setattr("shengmu.server.translate", translated)
    with TestClient(create_app()) as client:
        headers, media_id, job_id = setup_job(client, media)
        assert wait_job(client, job_id)["status"] == "done"
        assert (
            client.post(
                f"/api/jobs/{job_id}/translate", json={"target": "zh"}, headers=headers
            ).status_code
            == 202
        )
        assert wait_job(client, job_id, True)["revision"] == 1
        cues = client.get(f"/api/jobs/{job_id}/transcript").json()["segments"]
        assert cues[0]["translation"] == "你好世界"
        response = client.put(
            f"/api/jobs/{job_id}/transcript",
            json={
                "revision": 1,
                "segments": cues,
                "export_mode": "bilingual",
                "speaker_labels": True,
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert (
            "Speaker 1: Hello world\n你好世界" in client.get(f"/api/jobs/{job_id}/files/srt").text
        )
        monkeypatch.setattr(
            "shengmu.server.translate",
            lambda *args: (_ for _ in ()).throw(SubtitleError("test error")),
        )
        client.post(f"/api/jobs/{job_id}/translate", json={"target": "ja"}, headers=headers)
        assert wait_job(client, job_id, True)["operation"]["message"] == "test error"
        assert client.get(f"/api/jobs/{job_id}/transcript").json()["segments"] == cues
        assert client.delete(f"/api/media/{media_id}", headers=headers).status_code == 200
        assert client.post(f"/api/jobs/{job_id}/video", json={}, headers=headers).status_code == 409
        assert client.get(f"/api/jobs/{job_id}/video/burn").status_code == 404
        assert client.get(f"/api/media/{media_id}").status_code == 404
        assert client.get(f"/api/media/{media_id}/file").status_code == 404
        assert client.get(f"/api/media/{media_id}/waveform").status_code == 404


def test_draft_blocks_translation_and_revision_protects_edits(media, monkeypatch):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)
    with TestClient(create_app()) as client:
        headers, _, job_id = setup_job(client, media)
        assert wait_job(client, job_id)["status"] == "done"
        assert (
            client.put(
                f"/api/jobs/{job_id}/draft", json={"revision": 0, "segments": []}, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/jobs/{job_id}/translate", json={"target": "ja"}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.post(f"/api/jobs/{job_id}/reflow", json={}, headers=headers).status_code == 409
        )
        assert (
            client.put(
                f"/api/jobs/{job_id}/transcript",
                json={"revision": 99, "segments": []},
                headers=headers,
            ).status_code
            == 409
        )


def test_schema_word_boundaries_overlap_and_karaoke():
    cue = Segment(0, 2, "Hello world", [Word(0, 1, "Hello"), Word(1, 2, " world")], "Alice", "你好")
    transcript = Transcript(
        "a.mp4",
        4,
        "en",
        "local",
        "tiny",
        [cue],
        {
            "style": {"font": "Arial", "size": 70, "color": "#123456", "position": "top"},
            "word_highlight": True,
        },
    )
    assert Transcript.from_dict(transcript.to_dict()).segments[0] == cue
    assert "{\\kf100}Hello" in render(transcript, "ass")
    assert "Arial,70,&H00563412" in render(transcript, "ass")
    assert shift_segment(cue, 1).words[0].start == 1
    with pytest.raises(SubtitleError):
        Word(float("nan"), 2, "x")
    with pytest.raises(SubtitleError):
        Segment(0, 1, "x", [Word(0, 2, "x")])
    overlap = replace(transcript, segments=[cue, Segment(1, 3, "Other")], allow_overlap=True)
    assert overlap.allow_overlap
    with pytest.raises(SubtitleError):
        replace(overlap, allow_overlap=False)
    legacy = transcript.to_dict()
    legacy["schema_version"] = 1
    legacy["segments"] = [{"start": 0, "end": 1, "text": "legacy"}]
    assert Transcript.from_dict(legacy).segments[0].words == []
    for style in ({"font": "a,b"}, {"color": "bad"}, {"size": 900}, {"position": "elsewhere"}):
        with pytest.raises(SubtitleError):
            render(replace(transcript, metadata={"style": style}), "ass")
    with pytest.raises(SubtitleError, match="未翻译"):
        render(
            replace(
                transcript,
                segments=[replace(cue, translation=None)],
                metadata={"export_mode": "bilingual"},
            ),
            "srt",
        )


def test_caption_config_quality_and_speaker_assignment(monkeypatch, tmp_path):
    cues = split_caption(
        0, 4, "abcdefghij", options=CaptionOptions(latin_chars=5, max_lines=1, max_seconds=2)
    )
    assert len(cues) >= 2 and all(len(c.text) <= 5 for c in cues)
    assert all(w.estimated for c in cues for w in c.words)
    issues = quality_report([Segment(0, 0.1, "x" * 50), Segment(0.05, 9, "second")])
    assert "阅读速度过快" in issues[0]["messages"] and "时间冲突" in issues[1]["messages"]
    assert (
        assign_speakers(
            [Segment(0, 2, "x"), Segment(4, 5, "y")], [(0, 1.8, "Alice"), (1.8, 2, "Bob")]
        )[0].speaker
        == "Alice"
    )
    assert assign_speakers([Segment(4, 5, "y")], [(0, 2, "Alice")])[0].speaker is None
    with pytest.raises(SubtitleError):
        CaptionOptions(max_lines=0).validate()
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("SHENGMU_DIARIZATION_MODEL", raising=False)
    with pytest.raises(SubtitleError, match="HF_TOKEN"):
        diarize(tmp_path / "a.wav", [])


def test_translation_real_sdk_schema_batches_and_rejection(monkeypatch):
    openai = pytest.importorskip("openai")
    real_client = openai.OpenAI
    calls = []
    invalid = False

    def handle(request):
        data = json.loads(request.content)
        assert data["store"] is False and data["text"]["format"]["strict"] is True
        batch = json.loads(data["input"])
        calls.append(batch)
        values = [{"id": c["id"], "text": "翻译 " + c["text"]} for c in batch]
        if invalid:
            values = values[:-1]
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-4o-mini",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps({"translations": values}),
                                "annotations": [],
                            }
                        ],
                    }
                ],
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kwargs: real_client(
            api_key="test-only",
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
            **kwargs,
        ),
    )
    original = Transcript(
        "a", 100, "en", "local", "tiny", [Segment(i, i + 1, f"line {i}") for i in range(41)]
    )
    result = translate(original, "zh", "gpt-4o-mini")
    assert len(calls) == 2 and len(result.segments) == 41
    assert result.segments[-1].start == 40 and result.segments[-1].translation == "翻译 line 40"
    assert original.segments[0].translation is None
    invalid = True
    with pytest.raises(SubtitleError, match="不完整"):
        translate(original, "zh", "gpt-4o-mini")
    cancel = Event()
    cancel.set()
    with pytest.raises(Cancelled):
        translate(original, "zh", "gpt-4o-mini", cancel=cancel)
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(SubtitleError, match="API_KEY"):
        translate(original, "zh", "gpt-4o-mini")


def test_model_download_inventory_removal_and_failures(monkeypatch, tmp_path):
    from shengmu import model_manager

    root = tmp_path / "models"
    monkeypatch.setenv("SHENGMU_MODEL_DIR", str(root))

    def download(root, name, update, cancel):
        target = root / name
        target.mkdir(parents=True)
        update(50, 100, "test")
        for file in ("model.bin", "config.json", "tokenizer.json", ".complete"):
            (target / file).write_text("test")

    monkeypatch.setattr(model_manager, "download", download)
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        assert client.post("/api/models/unknown/download", headers=headers).status_code == 400
        assert client.post("/api/models/tiny/download", headers=headers).status_code == 202
        for _ in range(100):
            models = client.get("/api/models").json()
            if models[0].get("download", {}).get("status") == "done":
                break
            time.sleep(0.01)
        assert models[0]["downloaded"] and models[0]["size"] > 0
        assert client.post("/api/models/tiny/cancel", headers=headers).status_code == 200
        assert client.delete("/api/models/tiny", headers=headers).status_code == 200
        assert not client.get("/api/models").json()[0]["downloaded"]
        assert client.post("/api/models/missing/cancel", headers=headers).status_code == 404
        assert client.delete("/api/models/unknown", headers=headers).status_code == 400


def test_model_manager_download_contract(monkeypatch, tmp_path):
    huggingface_hub = pytest.importorskip("huggingface_hub")

    from shengmu.model_manager import download, model_inventory

    calls = []
    files = [
        SimpleNamespace(rfilename=name, size=10)
        for name in ("config.json", "model.bin", "tokenizer.json", "ignored.txt")
    ]
    monkeypatch.setattr(
        huggingface_hub.HfApi,
        "model_info",
        lambda *args, **kwargs: SimpleNamespace(siblings=files, sha="revision"),
    )

    def get(repo, filename, revision, local_dir):
        calls.append(filename)
        (local_dir / filename).write_text("1234567890")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", get)
    progress = []
    download(tmp_path, "tiny", lambda *args: progress.append(args))
    assert len(calls) == 3 and progress[-1][:2] == (30, 30)
    assert model_inventory(tmp_path)[0]["downloaded"]
    monkeypatch.setattr(
        huggingface_hub,
        "hf_hub_download",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("failure")),
    )
    with pytest.raises(SubtitleError, match="下载失败"):
        download(tmp_path, "base", lambda *args: None)


def test_diarization_backend_and_word_speaker_changes(monkeypatch, tmp_path):
    import sys
    import types

    calls = []

    class Annotation:
        def itertracks(self, yield_label):
            return [
                (SimpleNamespace(start=0, end=1), 0, "A"),
                (SimpleNamespace(start=1, end=2), 1, "B"),
            ]

    class Pipeline:
        @classmethod
        def from_pretrained(cls, model, token):
            calls.append((model, token))
            return cls()

        def __call__(self, path, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(exclusive_speaker_diarization=Annotation())

    module = types.ModuleType("pyannote.audio")
    module.Pipeline = Pipeline
    monkeypatch.setitem(sys.modules, "pyannote.audio", module)
    monkeypatch.setenv("HF_TOKEN", "test-token")
    cue = Segment(0, 2, "Hello world", [Word(0, 1, "Hello"), Word(1, 2, " world")])
    result = diarize(tmp_path / "a.wav", [cue], 2)
    assert [c.speaker for c in result] == ["A", "B"]
    assert [c.text for c in result] == ["Hello", "world"]
    assert calls[-1] == {"num_speakers": 2}


def test_pipeline_preserves_word_offsets_and_optional_speakers(media, monkeypatch):
    from shengmu.engines import EngineOptions
    from shengmu.pipeline import transcribe

    class Engine:
        def transcribe(self, audio, options, progress, cancel):
            return [Segment(0.2, 1, "Hello", [Word(0.2, 1, "Hello")])], "en", "tiny"

    monkeypatch.setattr(
        "shengmu.speakers.diarize",
        lambda audio, cues, *args: [replace(c, speaker="Alice") for c in cues],
    )
    result = transcribe(media, EngineOptions(diarize=True), engine=Engine())
    assert result.segments[0].speaker == "Alice"
    assert result.segments[0].words[0].start == result.segments[0].start

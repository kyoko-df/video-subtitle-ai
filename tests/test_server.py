from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shengmu.models import Segment, Transcript
from shengmu.server import create_app


def wait_done(client, job_id):
    for _ in range(100):
        status = client.get(f"/api/jobs/{job_id}").json()
        if status["status"] in {"done", "error", "cancelled"}:
            return status
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def fake_transcribe(source, options, track, progress, cancel):
    progress("transcribe", 0.8, "test")
    return Transcript(
        source.name, 3, "zh", options.engine, "fixture", [Segment(0.1, 1.5, "第一条")]
    )


def test_gui_upload_job_edit_download_contract(media: Path, monkeypatch):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)
    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 200
        assert client.get("/app.js").status_code == 200
        config = client.get("/api/config").json()
        assert "OPENAI_API_KEY" not in config
        headers = {"X-Session-Token": config["token"]}
        upload = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
        )
        assert upload.status_code == 200
        data = upload.json()
        assert len(data["audio_tracks"]) == 2
        job_response = client.post(
            "/api/jobs",
            json={
                "media_id": data["id"],
                "track": 2,
                "formats": ["srt", "vtt", "ass", "txt", "json"],
            },
            headers=headers,
        )
        assert job_response.status_code == 202
        job_id = job_response.json()["id"]
        result = wait_done(client, job_id)
        assert result["status"] == "done", result
        assert len(result["files"]) == 5
        transcript = client.get(f"/api/jobs/{job_id}/transcript").json()
        assert transcript["source"] == media.name
        assert client.get(f"/api/jobs/{job_id}/files/srt").status_code == 200
        edit = client.put(
            f"/api/jobs/{job_id}/transcript",
            json={
                "segments": [{"start": 0.2, "end": 1.6, "text": "已修订"}],
                "formats": ["srt", "json"],
            },
            headers=headers,
        )
        assert edit.status_code == 200
        assert "已修订" in client.get(f"/api/jobs/{job_id}/files/srt").text
        invalid = client.put(
            f"/api/jobs/{job_id}/transcript",
            json={"segments": [{"start": 2, "end": 1, "text": "bad"}]},
            headers=headers,
        )
        assert invalid.status_code == 400
        assert "已修订" in client.get(f"/api/jobs/{job_id}/files/srt").text
        assert client.get(f"/api/jobs/{job_id}/files/vtt").status_code == 404


def test_delete_upload_preserves_completed_subtitles(media, monkeypatch):
    monkeypatch.setattr("shengmu.server.transcribe", fake_transcribe)
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        upload = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
        ).json()
        path = client.app.state.workspace.media[upload["id"]].path
        job = client.post("/api/jobs", json={"media_id": upload["id"]}, headers=headers).json()
        assert wait_done(client, job["id"])["status"] == "done"
        assert client.delete(f"/api/media/{upload['id']}").status_code == 403
        assert client.delete(f"/api/media/{upload['id']}", headers=headers).status_code == 200
        assert not path.exists()
        assert client.get(f"/api/jobs/{job['id']}/files/srt").status_code == 200
        assert (
            client.post("/api/jobs", json={"media_id": upload["id"]}, headers=headers).status_code
            == 404
        )


def test_cannot_delete_active_upload(media, monkeypatch):
    from threading import Event

    started, finish = Event(), Event()

    def blocked(*args):
        started.set()
        assert finish.wait(5)
        return fake_transcribe(*args)

    monkeypatch.setattr("shengmu.server.transcribe", blocked)
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        upload = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
        ).json()
        job = client.post("/api/jobs", json={"media_id": upload["id"]}, headers=headers).json()
        try:
            assert started.wait(5)
            assert client.delete(f"/api/media/{upload['id']}", headers=headers).status_code == 409
        finally:
            finish.set()
        assert wait_done(client, job["id"])["status"] == "done"


def test_gui_model_allowlist_and_quality_options():
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        for model in ("/tmp/model", "unknown/repository"):
            assert (
                client.post(
                    "/api/jobs", json={"media_id": "missing", "model": model}, headers=headers
                ).status_code
                == 422
            )
        assert client.get("/api/config").json()["version"] == client.app.version
        assert client.get("/api/config", headers={"host": "[::1]:8765"}).status_code == 200
        assert (
            client.get("/api/config", headers={"host": "localhost@evil.example"}).status_code == 403
        )


def test_session_storage_limit_and_release(media, monkeypatch):
    with TestClient(
        create_app(max_upload=media.stat().st_size, max_storage=media.stat().st_size)
    ) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        first = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
        ).json()
        second = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
        )
        assert second.status_code == 413
        assert len(list(client.app.state.workspace.root.iterdir())) == 1
        assert client.delete(f"/api/media/{first['id']}", headers=headers).status_code == 200
        assert (
            client.post(
                "/api/media",
                params={"name": media.name},
                content=media.read_bytes(),
                headers=headers,
            ).status_code
            == 200
        )


def test_unexpected_job_failure_is_logged(media, monkeypatch, caplog):
    def fail(*args):
        raise RuntimeError("diagnostic marker")

    monkeypatch.setattr("shengmu.server.transcribe", fail)
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        upload = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
        ).json()
        job = client.post("/api/jobs", json={"media_id": upload["id"]}, headers=headers).json()
        assert wait_done(client, job["id"])["status"] == "error"
        assert "RuntimeError: diagnostic marker" in caplog.text
        assert "Traceback" in caplog.text


def test_upload_writes_run_in_worker_thread(media, monkeypatch):
    import threading

    from shengmu import server

    original = server.run_in_threadpool
    threads = []

    async def dispatch(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "write":
            event_loop_thread = threading.get_ident()

            def write():
                threads.append((event_loop_thread, threading.get_ident()))
                return func(*args, **kwargs)

            return await original(write)
        return await original(func, *args, **kwargs)

    monkeypatch.setattr(server, "run_in_threadpool", dispatch)
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        assert (
            client.post(
                "/api/media",
                params={"name": media.name},
                content=media.read_bytes(),
                headers=headers,
            ).status_code
            == 200
        )
        assert threads and all(loop != worker for loop, worker in threads)
        assert not client.app.state.workspace.upload_sizes


def test_session_and_host_restrictions():
    with TestClient(create_app()) as client:
        assert client.post("/api/jobs", json={"media_id": "missing"}).status_code == 403
        assert client.get("/api/config", headers={"host": "untrusted.example"}).status_code == 403
        config = client.get("/api/config").json()
        response = client.post(
            "/api/jobs", headers={"X-Session-Token": config["token"]}, json={"media_id": "missing"}
        )
        assert response.status_code == 404


def test_empty_and_oversized_upload_cleanup():
    with TestClient(create_app(max_upload=10)) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        assert client.post("/api/media?name=x.mp4", content=b"", headers=headers).status_code == 400
        assert (
            client.post("/api/media?name=x.mp4", content=b"x" * 11, headers=headers).status_code
            == 413
        )
        assert not list(client.app.state.workspace.root.iterdir())


def test_background_error_is_actionable(media: Path, monkeypatch):
    from shengmu.models import SubtitleError

    def fail(*args):
        raise SubtitleError("请配置 API Key")

    monkeypatch.setattr("shengmu.server.transcribe", fail)
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        upload = client.post(
            "/api/media", params={"name": "test.mkv"}, content=media.read_bytes(), headers=headers
        ).json()
        job = client.post("/api/jobs", json={"media_id": upload["id"]}, headers=headers).json()
        result = wait_done(client, job["id"])
        assert result["status"] == "error" and result["message"] == "请配置 API Key"


@pytest.mark.parametrize(
    "segments", [[{"start": -1, "end": 1, "text": "x"}], [{"start": 0, "end": 1, "text": ""}]]
)
def test_invalid_edits_rejected_by_schema(segments):
    with TestClient(create_app()) as client:
        headers = {"X-Session-Token": client.get("/api/config").json()["token"]}
        assert (
            client.put(
                "/api/jobs/missing/transcript", json={"segments": segments}, headers=headers
            ).status_code
            == 422
        )

from __future__ import annotations

import shutil
import time
import wave
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient

from shengmu.models import Segment
from shengmu.pipeline import transcribe
from shengmu.server import create_app


def headers(client):
    return {"X-Session-Token": client.get("/api/config").json()["token"]}


def link(client, source, auth):
    response = client.post("/api/media/link", json={"path": str(source)}, headers=auth)
    assert response.status_code == 200, response.text
    return response.json()


def wait_job(client, job_id, operation=False):
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        status = job["operation"].get("status") if operation else job["status"]
        if status in {"done", "error", "cancelled"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


class FixtureEngine:
    def transcribe(self, audio, options, progress, cancel):
        with wave.open(str(audio), "rb") as stream:
            assert stream.getnframes() > 0
        return [Segment(0.3, 1.5, "Shared video")], "en", "fixture"


def test_link_reads_source_without_copy_and_keeps_cache_local(media, tmp_path, monkeypatch):
    shared = tmp_path / "share"
    shared.mkdir()
    source = shared / media.name
    shutil.copy2(media, source)
    sibling = shared / "keep.txt"
    sibling.write_text("keep")
    original = source.read_bytes()
    root = tmp_path / "workspace"
    monkeypatch.setattr(
        "shengmu.server.transcribe",
        lambda *args: transcribe(*args, engine=FixtureEngine()),
    )
    # Linked media is unrestricted by upload byte quotas; it consumes no uploaded bytes.
    with TestClient(create_app(max_upload=1, max_storage=0, workspace_dir=root)) as client:
        auth = headers(client)
        item = link(client, source, auth)
        assert item["linked"] and item["available"]
        assert item["source_path"] == str(source.resolve())
        assert item["size"] == source.stat().st_size
        assert len(item["audio_tracks"]) == 2
        assert not (root / item["id"] / source.name).exists()
        preview = client.get(item["url"], headers={"Range": "bytes=0-15"})
        assert preview.status_code == 206 and preview.content == original[:16]
        waveform = client.get(f"/api/media/{item['id']}/waveform?track=2")
        assert waveform.status_code == 200
        assert (root / item["id"] / ".waveform-2.json").is_file()
        assert set(shared.iterdir()) == {source, sibling}
        response = client.post(
            "/api/jobs",
            json={"media_id": item["id"], "track": 2, "formats": ["srt", "ass"]},
            headers=auth,
        )
        assert response.status_code == 202, response.text
        job_id = response.json()["id"]
        assert wait_job(client, job_id)["status"] == "done"
        exported = client.post(f"/api/jobs/{job_id}/video", json={"mode": "soft"}, headers=auth)
        assert exported.status_code == 202
        assert wait_job(client, job_id, operation=True)["operation"]["status"] == "done"
        assert (root / job_id / "video-soft.mp4").is_file()
        assert source.read_bytes() == original and sibling.read_text() == "keep"
        assert client.delete(f"/api/media/{item['id']}", headers=auth).status_code == 200
        assert not (root / item["id"]).exists()
        assert not client.get(f"/api/jobs/{job_id}").json()["media_available"]
        assert source.read_bytes() == original and sibling.is_file()
        assert client.post(f"/api/jobs/{job_id}/video", json={}, headers=auth).status_code == 409
        assert client.get(f"/api/jobs/{job_id}/files/srt").status_code == 200
    with TestClient(create_app(workspace_dir=root)) as client:
        assert not client.get(f"/api/jobs/{job_id}").json()["media_available"]
        assert client.get("/api/media").json() == []
        assert source.read_bytes() == original


def test_link_survives_disconnection_and_restart(media, tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    monkeypatch.setattr(
        "shengmu.server.transcribe", lambda *args: transcribe(*args, engine=FixtureEngine())
    )
    with TestClient(create_app(workspace_dir=root)) as client:
        auth = headers(client)
        item = link(client, media, auth)
        response = client.post("/api/jobs", json={"media_id": item["id"]}, headers=auth)
        job_id = response.json()["id"]
        assert wait_job(client, job_id)["status"] == "done"
    disconnected = media.with_suffix(".disconnected")
    media.rename(disconnected)
    with TestClient(create_app(workspace_dir=root)) as client:
        auth = headers(client)
        stored = client.get("/api/media").json()[0]
        assert stored["id"] == item["id"] and not stored["available"]
        assert not client.get(f"/api/jobs/{job_id}").json()["media_available"]
        assert client.get(item["url"]).status_code == 404
        assert client.get(f"/api/media/{item['id']}/waveform").status_code == 409
        assert (
            client.post("/api/jobs", json={"media_id": item["id"]}, headers=auth).status_code == 409
        )
        assert client.get(f"/api/jobs/{job_id}/files/srt").status_code == 200
        disconnected.rename(media)
        assert client.get(f"/api/media/{item['id']}").json()["available"]
        assert client.get(f"/api/jobs/{job_id}").json()["media_available"]
        assert client.get(item["url"]).status_code == 200


def test_changed_source_requires_new_reference_and_does_not_use_stale_metadata(media):
    with TestClient(create_app()) as client:
        auth = headers(client)
        item = link(client, media, auth)
        with media.open("ab") as stream:
            stream.write(b"changed")
        assert not client.get(f"/api/media/{item['id']}").json()["available"]
        assert client.get(item["url"]).status_code == 404
        assert client.get(f"/api/media/{item['id']}/waveform").status_code == 409
        assert (
            client.post("/api/jobs", json={"media_id": item["id"]}, headers=auth).status_code == 409
        )
        new = link(client, media, auth)
        assert new["id"] != item["id"] and new["available"]
        assert client.delete(f"/api/media/{item['id']}", headers=auth).status_code == 200
        assert media.is_file() and client.get(new["url"]).status_code == 200


def test_reference_does_not_reduce_upload_quota(media):
    size = media.stat().st_size
    with TestClient(create_app(max_upload=size, max_storage=size)) as client:
        auth = headers(client)
        # Windows Explorer's "Copy as path" includes surrounding quotes.
        item = link(client, f'"{media}"', auth)
        uploaded = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=auth
        )
        assert uploaded.status_code == 200, uploaded.text
        assert not uploaded.json()["linked"]
        assert (
            client.post(
                "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=auth
            ).status_code
            == 413
        )
        assert client.delete(f"/api/media/{item['id']}", headers=auth).status_code == 200
        assert media.is_file()


def test_link_validation_token_permissions_and_capacity(media, tmp_path, monkeypatch):
    empty = tmp_path / "empty.mp4"
    empty.touch()
    text = tmp_path / "text.txt"
    text.write_text("not a video")
    with TestClient(create_app()) as client:
        assert client.post("/api/media/link", json={"path": str(media)}).status_code == 403
        auth = headers(client)
        for invalid in [
            "relative.mp4",
            "smb://server/share/file.mp4",
            "bad\x00path",
            tmp_path,
            empty,
            text,
            tmp_path / "missing.mp4",
        ]:
            response = client.post("/api/media/link", json={"path": str(invalid)}, headers=auth)
            assert response.status_code == 400, response.text
        assert client.get("/api/media").json() == []
        assert not client.app.state.workspace.upload_sizes
        original_open = Path.open

        def deny(path, *args, **kwargs):
            if path == media:
                raise PermissionError("read denied")
            return original_open(path, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", deny)
            denied = client.post("/api/media/link", json={"path": str(media)}, headers=auth)
            assert denied.status_code == 400 and "读取权限" in denied.json()["detail"]
        # Reservations for concurrently registering sources share the same 30-source cap.
        client.app.state.workspace.upload_sizes.update({str(i): 0 for i in range(30)})
        assert (
            client.post("/api/media/link", json={"path": str(media)}, headers=auth).status_code
            == 429
        )


def test_active_and_expired_reference_removal_never_deletes_source(media, monkeypatch):
    gate = Event()

    def blocked(*args):
        gate.wait(5)
        return transcribe(*args, engine=FixtureEngine())

    monkeypatch.setattr("shengmu.server.transcribe", blocked)
    with TestClient(create_app()) as client:
        auth = headers(client)
        item = link(client, media, auth)
        response = client.post("/api/jobs", json={"media_id": item["id"]}, headers=auth)
        try:
            assert client.delete(f"/api/media/{item['id']}", headers=auth).status_code == 409
            assert media.is_file()
        finally:
            gate.set()
        assert wait_job(client, response.json()["id"])["status"] == "done"
        linked = client.app.state.workspace.media[item["id"]]
        linked.created = time.time() - 90000
        client.app.state.workspace.cleanup()
        assert client.get("/api/media").json() == []
        assert media.is_file()
        assert not client.get(f"/api/jobs/{response.json()['id']}").json()["media_available"]

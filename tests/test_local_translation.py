from __future__ import annotations

import json
import time
from dataclasses import replace
from http.client import HTTPException
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from shengmu import lmstudio
from shengmu.diagnostics import redacted_traceback
from shengmu.models import Cancelled, Segment, SubtitleError, Transcript, Word
from shengmu.server import create_app
from shengmu.translation import translate


def response_for(batch):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {"translations": [{"id": c["id"], "text": "译文"} for c in batch]}
                    )
                },
            }
        ]
    }


@pytest.fixture
def local_server(monkeypatch):
    def handle(request):
        if request.method == "GET":
            return 200, {"data": [{"id": "local-instruct"}, {"id": "local-instruct"}]}
        return 200, response_for(json.loads(request.body["messages"][1]["content"]))

    state = SimpleNamespace(requests=[], closed=0, handle=handle)

    class Connection:
        def __init__(self, host, port, timeout):
            self.request_data = SimpleNamespace(host=host, port=port, timeout=timeout)

        def request(self, method, path, body, headers):
            self.request_data.method = method
            self.request_data.path = path
            self.request_data.body = json.loads(body) if body is not None else None
            self.request_data.headers = headers
            state.requests.append(self.request_data)

        def getresponse(self):
            status, body = state.handle(self.request_data)
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            return SimpleNamespace(status=status, read=lambda limit: raw[:limit])

        def close(self):
            state.closed += 1

    monkeypatch.setattr(lmstudio, "HTTPConnection", Connection)
    for name in ("OPENAI_API_KEY", "LM_STUDIO_API_KEY", "LM_STUDIO_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return state


def transcript(count=1):
    return Transcript(
        "video.mp4",
        count + 1,
        "en",
        "local",
        "tiny",
        [
            Segment(i, i + 1, f"Line {i}", [Word(i, i + 1, f"Line {i}")], "Alice", "旧译文")
            for i in range(count)
        ],
        metadata={"captions": {"cjk_chars": 20}, "custom": "keep"},
    )


def test_local_translation_batches_schema_and_preserves_alignment(local_server, monkeypatch):
    # Local translation must work even when the cloud SDK cannot be imported.
    monkeypatch.setitem(__import__("sys").modules, "openai", None)
    original = transcript(17)
    progress = []
    result = translate(
        original, "zh", "local-instruct", lambda *args: progress.append(args), provider="lmstudio"
    )
    assert len(result.segments) == 17
    assert [len(json.loads(r.body["messages"][1]["content"])) for r in local_server.requests] == [
        8,
        8,
        1,
    ]
    for request in local_server.requests:
        assert (request.host, request.port, request.path) == (
            "127.0.0.1",
            1234,
            "/v1/chat/completions",
        )
        assert request.timeout == 180 and request.method == "POST"
        assert "Authorization" not in request.headers
        assert request.body["model"] == "local-instruct"
        assert request.body["temperature"] == 0 and request.body["stream"] is False
        output = request.body["response_format"]
        assert output["type"] == "json_schema" and output["json_schema"]["strict"] is True
        assert output["json_schema"]["schema"]["additionalProperties"] is False
    assert progress[-1][0] == 1 and local_server.closed == 3
    for before, after in zip(original.segments, result.segments, strict=True):
        assert before == replace(after, translation="旧译文")
        assert after.translation == "译文"
    assert result.metadata["translation_provider"] == "lmstudio"
    assert result.metadata["translation_language"] == "zh"
    assert result.metadata["custom"] == "keep" and "translation_provider" not in original.metadata


def test_local_translation_character_limit(local_server):
    original = replace(transcript(3), segments=[Segment(i, i + 1, "a" * 1001) for i in range(3)])
    translate(original, "zh", "local-instruct", provider="lmstudio")
    assert len(local_server.requests) == 3


@pytest.mark.parametrize(
    "values",
    [
        [],
        [{"id": 1, "text": "x"}],
        [{"id": True, "text": "x"}],
        [{"id": "0", "text": "x"}],
        [{"id": 0, "text": " "}],
        [{"id": 0, "text": None}],
        [{"text": "x"}],
        [None],
        [{"id": 0, "text": "x"}, {"id": 0, "text": "y"}],
        "invalid",
    ],
)
def test_invalid_cue_output_preserves_previous_translation(local_server, values):
    data = response_for([])
    data["choices"][0]["message"]["content"] = json.dumps({"translations": values})
    local_server.handle = lambda request: (200, data)
    original = transcript()
    with pytest.raises(SubtitleError, match="不完整"):
        translate(original, "zh", "local-instruct", provider="lmstudio")
    assert original.segments[0].translation == "旧译文"


def test_later_batch_failure_is_atomic(local_server):
    handle = local_server.handle
    local_server.handle = lambda request: (
        handle(request) if len(local_server.requests) == 1 else (500, {})
    )
    original = transcript(9)
    with pytest.raises(SubtitleError, match="HTTP 500"):
        translate(original, "zh", "local-instruct", provider="lmstudio")
    assert len(local_server.requests) == 2
    assert all(c.translation == "旧译文" for c in original.segments)


def test_reordered_results_match_cues_and_duplicate_ids_are_rejected(local_server):
    data = response_for([])
    values = [{"id": 1, "text": "第二句"}, {"id": 0, "text": "第一句"}]
    data["choices"][0]["message"]["content"] = json.dumps({"translations": values})
    local_server.handle = lambda request: (200, data)
    original = transcript(2)
    result = translate(original, "zh", "local-instruct", provider="lmstudio")
    assert [c.translation for c in result.segments] == ["第一句", "第二句"]
    values[0]["id"] = 0
    data["choices"][0]["message"]["content"] = json.dumps({"translations": values})
    with pytest.raises(SubtitleError, match="不完整"):
        translate(original, "zh", "local-instruct", provider="lmstudio")
    assert all(c.translation == "旧译文" for c in original.segments)


@pytest.mark.parametrize("stage", ["before", "response", "between", "last"])
def test_local_cancellation_preserves_original(local_server, stage):
    cancel = Event()
    handle = local_server.handle
    if stage == "before":
        cancel.set()
    if stage == "response":

        def response(request):
            cancel.set()
            return handle(request)

        local_server.handle = response
    original = transcript(1 if stage == "last" else 9)

    def progress(*args):
        if stage in {"between", "last"}:
            cancel.set()

    with pytest.raises(Cancelled):
        translate(original, "zh", "local-instruct", progress, cancel, "lmstudio")
    assert len(local_server.requests) == (0 if stage == "before" else 1)
    assert all(c.translation == "旧译文" for c in original.segments)


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": None}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "not JSON"}}]},
    ],
)
def test_incomplete_local_response(local_server, result):
    local_server.handle = lambda request: (200, result)
    with pytest.raises(SubtitleError, match="不完整|未完整"):
        translate(transcript(), "zh", "local-instruct", provider="lmstudio")


@pytest.mark.parametrize(
    "address",
    [
        "https://localhost:1234/v1",
        "http://example.com/v1",
        "http://192.168.1.10/v1",
        "http://127.0.0.1:0/v1",
        "http://127.0.0.1:65536/v1",
        "http://localhost:bad/v1",
        "http://user:password@localhost:1234/v1",
        "http://localhost:1234/v1?token=secret",
        "http://localhost:1234/v1#fragment",
        "http://localhost:1234/other",
        "",
    ],
)
def test_local_url_rejects_non_loopback_and_credentials(local_server, monkeypatch, address):
    monkeypatch.setenv("LM_STUDIO_BASE_URL", address)
    with pytest.raises(SubtitleError, match="本机 HTTP"):
        lmstudio.models()
    assert local_server.requests == []


def test_local_inventory_and_optional_auth(local_server, monkeypatch):
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://[::1]:4321/")
    monkeypatch.setenv("LM_STUDIO_API_KEY", "local-test-secret")
    assert lmstudio.models() == ["local-instruct"]
    request = local_server.requests[0]
    assert (request.host, request.port, request.path, request.timeout) == (
        "::1",
        4321,
        "/v1/models",
        8,
    )
    assert request.headers["Authorization"] == "Bearer local-test-secret"
    local_server.handle = lambda request: (200, {"data": []})
    assert lmstudio.models() == []


@pytest.mark.parametrize("data", [{}, [], {"data": {}}, {"data": [None]}, {"data": [{}]}])
def test_invalid_model_inventory(local_server, data):
    local_server.handle = lambda request: (200, data)
    with pytest.raises(SubtitleError, match="模型列表无效"):
        lmstudio.models()


@pytest.mark.parametrize(
    "status, message", [(401, "认证"), (403, "认证"), (302, "HTTP 302"), (500, "HTTP 500")]
)
def test_local_http_error_without_server_body_or_redirect(local_server, status, message):
    local_server.handle = lambda request: (status, {"error": "private server details"})
    with pytest.raises(SubtitleError, match=message) as failure:
        lmstudio.models()
    assert "private server details" not in str(failure.value)
    assert len(local_server.requests) == 1 and local_server.closed == 1


@pytest.mark.parametrize(
    "raw, message",
    [(b"bad JSON", "无效 JSON"), (b"\xff", "无效 JSON"), (b" " * (2 * 1024**2 + 1), "过大")],
)
def test_invalid_or_oversized_response(local_server, raw, message):
    local_server.handle = lambda request: (200, raw)
    with pytest.raises(SubtitleError, match=message):
        lmstudio.models()
    assert local_server.closed == 1


@pytest.mark.parametrize("failure", [ConnectionRefusedError(), TimeoutError(), HTTPException()])
def test_unavailable_local_server(local_server, failure):
    def unavailable(request):
        raise failure

    local_server.handle = unavailable
    with pytest.raises(SubtitleError, match="Developer"):
        lmstudio.models()
    assert local_server.closed == 1


def test_translation_rejects_empty_settings(local_server):
    for target, model, provider in [
        ("zh", "x", "other"),
        ("zh", " ", "lmstudio"),
        (" ", "x", "lmstudio"),
    ]:
        with pytest.raises(SubtitleError):
            translate(transcript(), target, model, provider=provider)
    assert not local_server.requests


def wait_job(client, job_id, operation=False):
    for _ in range(400):
        job = client.get(f"/api/jobs/{job_id}").json()
        if (job["operation"].get("status") if operation else job["status"]) in {
            "done",
            "error",
            "cancelled",
        }:
            return job
        time.sleep(0.01)
    raise AssertionError("operation did not finish")


def test_local_api_translation_export_and_preferences_persist(
    local_server, media, tmp_path, monkeypatch
):
    monkeypatch.setattr("shengmu.server.transcribe", lambda *args: transcript())
    workspace = tmp_path / "workspace"
    with TestClient(create_app(workspace_dir=workspace)) as client:
        config = client.get("/api/config").json()
        assert not config["openai_key_configured"]
        assert "LM_STUDIO_API_KEY" not in config
        headers = {"X-Session-Token": config["token"]}
        assert client.get("/api/translation/models").json() == {"models": ["local-instruct"]}
        upload = client.post(
            "/api/media", params={"name": media.name}, content=media.read_bytes(), headers=headers
        )
        job_id = client.post(
            "/api/jobs", json={"media_id": upload.json()["id"]}, headers=headers
        ).json()["id"]
        assert wait_job(client, job_id)["status"] == "done"
        endpoint = f"/api/jobs/{job_id}/translate"
        settings = {"provider": "lmstudio", "target": "zh", "model": "local-instruct"}
        assert client.post(endpoint, json=settings, headers=headers).status_code == 202
        done = wait_job(client, job_id, True)
        assert done["revision"] == 1 and done["operation"]["status"] == "done"
        data = client.get(f"/api/jobs/{job_id}/transcript").json()
        assert data["metadata"]["translation_provider"] == "lmstudio"
        assert (
            data["segments"][0]["text"] == "Line 0" and data["segments"][0]["translation"] == "译文"
        )
        assert (
            client.put(
                f"/api/jobs/{job_id}/transcript",
                json={"revision": 1, "segments": data["segments"], "export_mode": "bilingual"},
                headers=headers,
            ).status_code
            == 200
        )
        assert "Line 0\n译文" in client.get(f"/api/jobs/{job_id}/files/srt").text
        assert (
            client.put(
                "/api/preferences",
                json={
                    "translation_provider": "lmstudio",
                    "translation_model": "local-instruct",
                    "target_language": "zh",
                },
                headers=headers,
            ).status_code
            == 200
        )
        local_server.handle = lambda request: (503, {})
        assert client.get("/api/translation/models").status_code == 503
        assert client.post(endpoint, json=settings, headers=headers).status_code == 202
        failed = wait_job(client, job_id, True)
        assert failed["operation"]["status"] == "error" and failed["revision"] == 2
        assert client.get(f"/api/jobs/{job_id}/transcript").json()["segments"] == data["segments"]
        assert (
            client.post(
                endpoint, json={**settings, "provider": "invalid"}, headers=headers
            ).status_code
            == 422
        )
    with TestClient(create_app(workspace_dir=workspace)) as client:
        preferences = client.get("/api/preferences").json()
        assert preferences["translation_provider"] == "lmstudio"
        assert preferences["translation_model"] == "local-instruct"
        assert (
            client.get(f"/api/jobs/{job_id}/transcript").json()["segments"][0]["translation"]
            == "译文"
        )


def test_local_auth_token_is_redacted(monkeypatch):
    monkeypatch.setenv("LM_STUDIO_API_KEY", "local-private-token")
    try:
        raise ValueError("failed with local-private-token at http://localhost:1234/v1")
    except ValueError:
        output = redacted_traceback()
    assert "local-private-token" not in output and "localhost" not in output

from __future__ import annotations

import wave
from pathlib import Path
from threading import Event

import httpx
import pytest

from shengmu.engines import EngineOptions, OpenAIEngine, wav_chunks
from shengmu.models import Cancelled, SubtitleError


def make_wav(path: Path, seconds: int = 61):
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * (16000 * seconds))


def test_chunk_offsets_and_cleanup(tmp_path: Path):
    audio = tmp_path / "audio.wav"
    make_wav(audio)
    chunks = []
    for path, offset, duration in wav_chunks(audio, 30):
        assert path.exists() and path.stat().st_size < 25_000_000
        chunks.append((offset, duration))
    assert chunks == [(0, 30), (30, 30), (60, 1)]
    assert not (tmp_path / "api-chunk.wav").exists()


def test_real_sdk_multipart_contract_and_global_timestamps(monkeypatch, tmp_path: Path):
    openai = pytest.importorskip("openai")
    real_client = openai.OpenAI
    requests = []

    def handle(request):
        body = request.read()
        assert b'name="response_format"' in body and b"verbose_json" in body
        assert b'name="timestamp_granularities[]"' in body and b"segment" in body
        assert b"whisper-1" in body and b'name="language"' in body
        if requests:
            assert b'name="prompt"' in body and b"Hello" in body
        assert b"word" in body
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "text": "Hello",
                "language": "english",
                "duration": 30,
                "segments": [
                    {
                        "id": 0,
                        "seek": 0,
                        "start": 0.1,
                        "end": 0.8,
                        "text": "Hello",
                        "tokens": [],
                        "temperature": 0,
                        "avg_logprob": 0,
                        "compression_ratio": 1,
                        "no_speech_prob": 0,
                    }
                ],
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kwargs: real_client(
            api_key="test-only-not-a-real-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
    )
    audio = tmp_path / "audio.wav"
    make_wav(audio)
    segments, language, model = OpenAIEngine().transcribe(
        audio,
        EngineOptions(engine="openai", language="en", chunk_seconds=30),
        lambda *a: None,
        None,
    )
    assert len(requests) == 3
    assert [s.start for s in segments] == [0.1, 30.1, 60.1]
    assert language == "en" and model == "whisper-1"
    assert not (tmp_path / "api-chunk.wav").exists()


def test_chunk_boundary_prefers_nearby_silence_without_losing_samples(tmp_path):
    audio = tmp_path / "speech.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x10\x27" * (16000 * 28) + b"\0\0" * 8000 + b"\x10\x27" * 200000)
    chunks = []
    total_frames = 0
    for path, offset, duration in wav_chunks(audio, 30):
        chunks.append((offset, duration))
        with wave.open(str(path), "rb") as source:
            total_frames += source.getnframes()
    assert 28 <= chunks[0][1] <= 28.5
    assert chunks[1][0] == chunks[0][1]
    assert total_frames == 16000 * 28 + 8000 + 200000


def test_oversized_chunk_is_cleaned(monkeypatch, tmp_path):
    audio = tmp_path / "audio.wav"
    make_wav(audio, 1)
    stat = Path.stat

    def oversized(path, *args, **kwargs):
        from types import SimpleNamespace

        if path.name == "api-chunk.wav":
            return SimpleNamespace(st_size=25_000_000)
        return stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", oversized)
    with pytest.raises(SubtitleError, match="25MB"):
        list(wav_chunks(audio, 30))
    monkeypatch.setattr(Path, "stat", stat)
    assert not (tmp_path / "api-chunk.wav").is_file()


def test_api_word_alignment_and_hallucination_filter(monkeypatch, tmp_path):
    from types import SimpleNamespace

    openai = pytest.importorskip("openai")
    calls = []

    class Client:
        def __init__(self, **kwargs):
            self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def create(self, file, **kwargs):
            calls.append(kwargs)
            assert file.read(4) == b"RIFF"
            return SimpleNamespace(
                language="zh",
                text="你好，世界！字幕由 Amara.org 社区提供",
                segments=[
                    SimpleNamespace(start=0, end=2, text="你好，世界！"),
                    SimpleNamespace(start=2, end=3, text="字幕由 Amara.org 社区提供"),
                ],
                words=[
                    SimpleNamespace(start=0.2, end=0.8, word="你好"),
                    SimpleNamespace(start=1, end=1.8, word="世界"),
                ],
            )

    monkeypatch.setattr(openai, "OpenAI", Client)
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    audio = tmp_path / "audio.wav"
    make_wav(audio, 3)
    cues, language, _ = OpenAIEngine().transcribe(
        audio, EngineOptions(language="zh"), lambda *args: None, None
    )
    assert language == "zh"
    assert cues[0].start == 0.2 and cues[-1].end == pytest.approx(1.8)
    assert "".join(c.text.replace("\n", "") for c in cues) == "你好，世界！"
    assert "简体中文" in calls[0]["prompt"]


def test_missing_key_does_not_upload(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SubtitleError, match="OPENAI_API_KEY"):
        OpenAIEngine().transcribe(
            tmp_path / "not-needed.wav", EngineOptions(), lambda *a: None, None
        )


def test_cancellation_after_api_response_drops_result_and_cleans_chunk(monkeypatch, tmp_path: Path):
    openai = pytest.importorskip("openai")
    real_client = openai.OpenAI
    cancel = Event()

    def handle(request):
        cancel.set()
        return httpx.Response(
            200, json={"text": "", "language": "en", "duration": 30, "segments": []}
        )

    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kwargs: real_client(
            api_key="test-only-not-a-real-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        ),
    )
    audio = tmp_path / "audio.wav"
    make_wav(audio)
    with pytest.raises(Cancelled):
        OpenAIEngine().transcribe(audio, EngineOptions(chunk_seconds=30), lambda *a: None, cancel)
    assert not (tmp_path / "api-chunk.wav").exists()


def test_text_only_gateway_is_not_a_successful_timed_transcription(monkeypatch, tmp_path: Path):
    openai = pytest.importorskip("openai")
    real_client = openai.OpenAI
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kwargs: real_client(
            api_key="test-only-not-a-real-key",
            max_retries=0,
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, json={"text": "Hello", "duration": 1, "language": "en"}
                    )
                )
            ),
        ),
    )
    audio = tmp_path / "audio.wav"
    make_wav(audio, 1)
    with pytest.raises(SubtitleError, match="没有片段时间戳"):
        OpenAIEngine().transcribe(audio, EngineOptions(), lambda *a: None, None)

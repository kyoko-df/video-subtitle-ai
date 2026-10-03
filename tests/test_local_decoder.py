from __future__ import annotations

import wave
from types import SimpleNamespace

import pytest

from shengmu.engines import EngineOptions, LocalEngine


def test_faster_whisper_can_decode_pipeline_wav(tmp_path):
    # No model/network required. This catches the PyAV 19 metadata_errors API break.
    audio_module = pytest.importorskip("faster_whisper.audio")
    audio = tmp_path / "audio.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000)
    samples = audio_module.decode_audio(str(audio), sampling_rate=16000)
    assert samples.shape == (16000,)
    assert samples.dtype.name == "float32"


def test_local_cache_word_timestamps_and_chinese_defaults(tmp_path, monkeypatch):
    import sys

    loads, calls = [], []

    class Model:
        def __init__(self, name, **kwargs):
            loads.append((name, kwargs))

        def transcribe(self, audio, **kwargs):
            calls.append(kwargs)
            return iter(
                [
                    SimpleNamespace(
                        start=0,
                        end=2,
                        text="正常句子。",
                        words=[SimpleNamespace(start=0.2, end=1.5, word="正常句子。")],
                    ),
                    SimpleNamespace(start=2, end=3, text="字幕由 Amara.org 社区提供", words=None),
                ]
            ), SimpleNamespace(language="zh")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(LocalEngine, "_cached_model", None)
    monkeypatch.setattr(LocalEngine, "_cached_key", None)
    audio = tmp_path / "local.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 48000)
    for _ in range(2):
        cues, language, _ = LocalEngine().transcribe(
            audio, EngineOptions(language="zh"), lambda *args: None, None
        )
        assert language == "zh"
        assert len(cues) == 1 and cues[0].start == 0.2
    assert len(loads) == 1
    assert calls[0]["word_timestamps"] is True
    assert calls[0]["condition_on_previous_text"] is False
    assert "普通话" in calls[0]["initial_prompt"]
    LocalEngine().transcribe(audio, EngineOptions(model="tiny"), lambda *args: None, None)
    assert len(loads) == 2
    assert calls[-1]["initial_prompt"] is None


def test_local_quality_options_can_be_overridden(tmp_path, monkeypatch):
    import sys

    calls = []

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            calls.append(kwargs)
            return iter(
                [SimpleNamespace(start=0, end=1, text="请不吝点赞订阅", words=None)]
            ), SimpleNamespace(language="zh")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(LocalEngine, "_cached_model", None)
    audio = tmp_path / "local.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 16000)
    cues, _, _ = LocalEngine().transcribe(
        audio,
        EngineOptions(
            language="zh",
            prompt="自定义提示",
            condition_on_previous_text=True,
            filter_hallucinations=False,
        ),
        lambda *args: None,
        None,
    )
    assert len(cues) == 1
    assert calls[0]["initial_prompt"] == "自定义提示"
    assert calls[0]["condition_on_previous_text"] is True


def test_raw_repetition_is_flagged_before_caption_splitting(tmp_path, monkeypatch):
    import sys

    from shengmu.captions import CaptionOptions

    calls = []

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            calls.append(kwargs)
            return iter(
                [
                    SimpleNamespace(
                        start=0,
                        end=3,
                        text="サンプル" * 4,
                        words=None,
                        no_speech_prob=0.8,
                        avg_logprob=-1.2,
                        compression_ratio=float("nan"),
                    )
                ]
            ), SimpleNamespace(language="ja")

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(LocalEngine, "_cached_model", None)
    audio = tmp_path / "raw.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * 48000)
    cues, _, _ = LocalEngine().transcribe(
        audio,
        EngineOptions(
            language="ja",
            asr_profile="less-repetition",
            no_speech_threshold=0.7,
            captions=CaptionOptions(cjk_chars=5, max_lines=1),
        ),
        lambda *args: None,
        None,
    )
    assert len(cues) > 1 and all(c.suspicions for c in cues)
    assert all(c.diagnostics == {"avg_logprob": -1.2, "no_speech_prob": 0.8} for c in cues)
    assert calls[0]["no_speech_threshold"] == 0.7 and calls[0]["repetition_penalty"] == 1.1
    assert "".join(c.text for c in cues) == "サンプル" * 4

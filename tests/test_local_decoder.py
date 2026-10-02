from __future__ import annotations

import wave

import pytest


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

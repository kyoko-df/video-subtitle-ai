from __future__ import annotations

import json
from pathlib import Path

import pytest

from shengmu.exporters import FORMATS, export_files, render
from shengmu.models import Segment, SubtitleError, Transcript, normalize_segments


def transcript() -> Transcript:
    return Transcript(
        "测试.mp4",
        3700,
        "zh",
        "local",
        "small",
        [
            Segment(59.9996, 61.2346, "你好，世界。"),
            Segment(3600.1, 3602.8, "第二行\n字幕 <tag> & {\\pos(1,2)}"),
        ],
    )


def test_timestamp_rollover_and_json_roundtrip():
    data = transcript()
    assert "00:01:00,000 --> 00:01:01,235" in render(data, "srt")
    assert "01:00:00.100 --> 01:00:02.800" in render(data, "vtt")
    assert render(data, "vtt").startswith("WEBVTT\n\n")
    assert "&lt;tag&gt; &amp;" in render(data, "vtt")
    ass = render(data, "ass")
    assert "00:01:00.00,00:01:01.23" in ass
    assert r"\pos(1,2)" not in ass and r"第二行\N字幕" in ass
    assert Transcript.from_dict(json.loads(render(data, "json"))).to_dict() == data.to_dict()
    assert "你好，世界。" in render(data, "txt")


def test_short_cue_keeps_positive_serialized_duration():
    data = Transcript("a", 1, None, "local", "tiny", [Segment(0.0001, 0.0002, "短")])
    assert "00:00:00,000 --> 00:00:00,001" in render(data, "srt")
    assert "00:00:00.00,00:00:00.01" in render(data, "ass")


@pytest.mark.parametrize(
    "start,end,text", [(-1, 1, "x"), (1, 1, "x"), (0, float("inf"), "x"), (0, 1, "  ")]
)
def test_reject_invalid_cues(start, end, text):
    with pytest.raises(SubtitleError):
        Segment(start, end, text)


def test_normalize_boundary_jitter():
    data = normalize_segments(
        [Segment(1, 2, "a"), Segment(1.9, 3, "b"), Segment(8, 9, "outside")], 2.5
    )
    assert data == [Segment(1, 2, "a"), Segment(2, 2.5, "b")]


def test_exports_refuse_overwrite_before_any_write(tmp_path: Path):
    existing = tmp_path / "字幕.vtt"
    existing.write_text("old", encoding="utf-8")
    with pytest.raises(SubtitleError):
        export_files(transcript(), tmp_path, "字幕", ["srt", "vtt"])
    assert not (tmp_path / "字幕.srt").exists()
    assert existing.read_text() == "old"
    paths = export_files(transcript(), tmp_path, "字幕", FORMATS, overwrite=True)
    assert len(paths) == 5 and all(path.exists() for path in paths)
    assert not list(tmp_path.glob(".shengmu-*"))


def test_protect_source_even_when_overwrite_enabled(tmp_path: Path):
    source = tmp_path / "字幕.json"
    source.write_text("original")
    with pytest.raises(SubtitleError, match="输入文件相同"):
        export_files(transcript(), tmp_path, "字幕", ["json"], True, source)
    assert source.read_text() == "original"


def test_bad_json_and_overlapping_edits_are_rejected():
    with pytest.raises(SubtitleError):
        Transcript.from_dict({"source": "a"})
    data = transcript().to_dict()
    data["segments"][1]["start"] = 60
    with pytest.raises(SubtitleError, match="重叠"):
        Transcript.from_dict(data)

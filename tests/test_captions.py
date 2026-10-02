from types import SimpleNamespace

import pytest

from shengmu.captions import is_hallucination, split_caption


def words(text, step=0.25):
    return [
        SimpleNamespace(start=i * step, end=(i + 1) * step, word=part)
        for i, part in enumerate(text)
    ]


def test_chinese_word_timestamps_split_and_wrap():
    text = "这是一段用于测试字幕自动切分的中文语音，" * 4
    cues = split_caption(0, len(text) * 0.25, text, words(text))
    assert len(cues) > 2
    assert "".join(c.text.replace("\n", "") for c in cues) == text
    assert all(len(c.text.splitlines()) <= 2 for c in cues)
    assert all(len(line) <= 20 for c in cues for line in c.text.splitlines())
    assert all(0 < c.end - c.start <= 7 for c in cues)
    assert all(a.end <= b.start for a, b in zip(cues, cues[1:], strict=False))
    assert cues[0].start == 0
    assert cues[-1].end == len(text) * 0.25


def test_english_words_and_punctuation_boundaries():
    parts = [" This", " is", " a", " sentence.", " Another", " sentence", " follows."]
    cues = split_caption(0, 3.5, "".join(parts), words(parts, 0.5))
    assert [c.text for c in cues] == ["This is a sentence.", "Another sentence follows."]
    assert [c.start for c in cues] == [0, 2]


def test_fallback_without_words_and_oversized_word():
    for timed in (None, words(["x" * 200], 20)):
        cues = split_caption(0, 20, "x" * 200, timed)
        assert "".join(c.text.replace("\n", "") for c in cues) == "x" * 200
        assert all(c.end - c.start <= 7 for c in cues)
        assert all(len(line) <= 42 for c in cues for line in c.text.splitlines())


def test_jitter_and_empty_words_fall_back_safely():
    cues = split_caption(0, 2, "正常文本", [SimpleNamespace(start=-1, end=4, word="正常文本")])
    assert cues[0].start == 0 and cues[-1].end == 2
    assert split_caption(0, 0, "text", None) == []
    assert split_caption(0, 2, " ", None) == []
    assert split_caption(0, 2, "正常文本", [])


@pytest.mark.parametrize(
    "text", ["字幕由 Amara.org 社区提供。", "字幕由Amara.org社區提供", "请不吝点赞订阅！"]
)
def test_known_isolated_hallucinations(text):
    assert is_hallucination(text)


def test_sparse_fallback_words_never_exceed_maximum_duration():
    cues = split_caption(0, 30, "Hello world", None)
    assert all(c.end - c.start <= 7 for c in cues)
    assert "".join(c.text.replace("\n", "").replace(" ", "") for c in cues) == "Helloworld"


def test_word_alignment_preserves_segment_punctuation():
    timed = words(["Hello", "world"], 1)
    cues = split_caption(0, 2, "Hello, world!", timed)
    assert " ".join(c.text.replace("\n", " ") for c in cues) == "Hello, world!"


def test_partial_word_timestamps_do_not_drop_text():
    cues = split_caption(0, 2, "Hello world", words(["Hello"], 1))
    assert cues[-1].end == 2
    assert " ".join(c.text for c in cues) == "Hello world"


def test_real_dialogue_mentioning_outro_is_not_removed():
    assert not is_hallucination("他说请不吝点赞订阅，然后继续讲解。")

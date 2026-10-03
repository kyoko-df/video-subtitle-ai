from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any


class SubtitleError(Exception):
    """An actionable error suitable for presentation to a user."""


class Cancelled(SubtitleError):
    pass


def clean_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text.replace("\r", ""))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str
    estimated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "text",
            re.sub(
                r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", self.text.replace("\r", "").replace("\n", " ")
            ),
        )
        if (
            not all(math.isfinite(v) for v in (self.start, self.end))
            or self.start < 0
            or self.end <= self.start
            or not self.text.strip()
        ):
            raise SubtitleError("词级时间戳无效。")


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)
    speaker: str | None = None
    translation: str | None = None
    diagnostics: dict[str, float] = field(default_factory=dict)
    suspicions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.diagnostics, dict)
            or not isinstance(self.suspicions, list)
            or any(
                k not in {"avg_logprob", "no_speech_prob", "compression_ratio"}
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
                for k, v in self.diagnostics.items()
            )
            or any(not isinstance(v, str) or len(v) > 100 for v in self.suspicions)
        ):
            raise SubtitleError("字幕诊断信息无效。")
        if not all(math.isfinite(v) for v in (self.start, self.end)):
            raise SubtitleError("字幕时间必须是有限数值。")
        if self.start < 0 or self.end <= self.start:
            raise SubtitleError("字幕时间无效：结束时间必须大于开始时间，且开始时间不能为负。")
        if not clean_text(self.text):
            raise SubtitleError("字幕内容不能为空。")
        object.__setattr__(self, "text", clean_text(self.text))
        previous = self.start
        for word in self.words:
            if word.start < previous - 0.001 or word.end > self.end + 0.001:
                raise SubtitleError("词级时间戳必须位于字幕内且按顺序排列。")
            previous = word.end
        if self.speaker is not None:
            if not isinstance(self.speaker, str) or len(self.speaker) > 80:
                raise SubtitleError("说话人名称无效。")
            object.__setattr__(self, "speaker", clean_text(self.speaker).replace("\n", " ") or None)
        if self.translation is not None:
            object.__setattr__(self, "translation", clean_text(self.translation) or None)


@dataclass
class Transcript:
    source: str
    duration: float
    language: str | None
    engine: str
    model: str
    segments: list[Segment]
    metadata: dict[str, Any] = field(default_factory=dict)
    allow_overlap: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, dict) or not isinstance(self.allow_overlap, bool):
            raise SubtitleError("字幕元数据或重叠设置无效。")
        if not math.isfinite(self.duration) or self.duration <= 0:
            raise SubtitleError("媒体时长无效。")
        last_end = last_start = 0.0
        for segment in self.segments:
            if segment.start < last_start or (
                not self.allow_overlap and segment.start < last_end - 0.001
            ):
                raise SubtitleError("字幕时间轴必须按顺序排列，且不能重叠。")
            if segment.end > self.duration + 0.05:
                raise SubtitleError("字幕结束时间不能超出媒体时长。")
            last_end = segment.end
            last_start = segment.start

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 2, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        try:
            if data.get("schema_version", 1) not in (1, 2):
                raise SubtitleError("不支持此 JSON 字幕版本。")
            return cls(
                source=str(data["source"]),
                duration=float(data["duration"]),
                language=data.get("language"),
                engine=str(data["engine"]),
                model=str(data["model"]),
                segments=[
                    Segment(
                        float(s["start"]),
                        float(s["end"]),
                        str(s["text"]),
                        [Word(**word) for word in s.get("words", [])],
                        s.get("speaker"),
                        s.get("translation"),
                        s.get("diagnostics", {}),
                        s.get("suspicions", []),
                    )
                    for s in data["segments"]
                ],
                metadata=data.get("metadata", {}),
                allow_overlap=data.get("allow_overlap", False),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise SubtitleError("JSON 字幕结构无效。请使用声幕导出的 JSON 文件。") from exc


def normalize_segments(segments: list[Segment], duration: float) -> list[Segment]:
    """Clamp ASR boundary jitter and drop cues outside the audio range."""
    result: list[Segment] = []
    last_end = 0.0
    for segment in sorted(segments, key=lambda s: s.start):
        start, end = max(0.0, segment.start, last_end), min(segment.end, duration)
        if end - start < 0.001:
            continue
        words = [
            replace(w, start=max(start, w.start), end=min(end, w.end))
            for w in segment.words
            if min(end, w.end) > max(start, w.start)
        ]
        result.append(replace(segment, start=start, end=end, words=words))
        last_end = end
    return result


def shift_segment(segment: Segment, offset: float) -> Segment:
    return replace(
        segment,
        start=segment.start + offset,
        end=segment.end + offset,
        words=[replace(w, start=w.start + offset, end=w.end + offset) for w in segment.words],
    )

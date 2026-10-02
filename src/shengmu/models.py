from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any


class SubtitleError(Exception):
    """An actionable error suitable for presentation to a user."""


class Cancelled(SubtitleError):
    pass


def clean_text(text: str) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text.replace("\r", ""))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.start, self.end)):
            raise SubtitleError("字幕时间必须是有限数值。")
        if self.start < 0 or self.end <= self.start:
            raise SubtitleError("字幕时间无效：结束时间必须大于开始时间，且开始时间不能为负。")
        if not clean_text(self.text):
            raise SubtitleError("字幕内容不能为空。")
        object.__setattr__(self, "text", clean_text(self.text))


@dataclass
class Transcript:
    source: str
    duration: float
    language: str | None
    engine: str
    model: str
    segments: list[Segment]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not math.isfinite(self.duration) or self.duration <= 0:
            raise SubtitleError("媒体时长无效。")
        last_end = 0.0
        for segment in self.segments:
            if segment.start < last_end - 0.001:
                raise SubtitleError("字幕时间轴必须按顺序排列，且不能重叠。")
            if segment.end > self.duration + 0.05:
                raise SubtitleError("字幕结束时间不能超出媒体时长。")
            last_end = segment.end

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        try:
            if data.get("schema_version", 1) != 1:
                raise SubtitleError("不支持此 JSON 字幕版本。")
            return cls(
                source=str(data["source"]),
                duration=float(data["duration"]),
                language=data.get("language"),
                engine=str(data["engine"]),
                model=str(data["model"]),
                segments=[
                    Segment(float(s["start"]), float(s["end"]), str(s["text"]))
                    for s in data["segments"]
                ],
                metadata=data.get("metadata", {}),
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
        result.append(Segment(start, end, segment.text))
        last_end = end
    return result

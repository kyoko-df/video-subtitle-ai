from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Iterable

from .models import Segment, SubtitleError, Word

CJK = re.compile(r"[\u3000-\u9fff\uac00-\ud7af]")
TOKENS = re.compile(r"\s*[A-Za-zÀ-ɏ0-9_]+(?:['’-][A-Za-zÀ-ɏ0-9_]+)*|\s*[^\s]")
PUNCTUATION = re.compile(r"[.!?。！？,，;；:：][\"'”’）)]*$")
HALLUCINATIONS = {
    "字幕由amaraorg社区提供",
    "字幕由amaraorg社區提供",
    "amaraorg",
    "请不吝点赞订阅",
    "請不吝點贊訂閱",
    "请不吝点赞订阅转发打赏支持明镜与点点栏目",
    "請不吝點贊訂閱轉發打賞支持明鏡與點點欄目",
}


@dataclass(frozen=True)
class CaptionOptions:
    cjk_chars: int = 20
    latin_chars: int = 42
    max_lines: int = 2
    max_seconds: float = 7
    min_seconds: float = 1
    max_cps: float = 20

    def validate(self):
        if (
            not 5 <= self.cjk_chars <= 100
            or not 5 <= self.latin_chars <= 200
            or not 1 <= self.max_lines <= 4
            or not 0.5 <= self.max_seconds <= 30
            or not 0 <= self.min_seconds <= self.max_seconds
            or not 1 <= self.max_cps <= 100
        ):
            raise SubtitleError("字幕排版参数无效。")


def quality_report(segments, options=None, allow_overlap=False):
    options = options or CaptionOptions()
    options.validate()
    issues = []
    last_end = 0
    for index, cue in enumerate(segments):
        messages = list(cue.suspicions)
        limit = options.cjk_chars if CJK.search(cue.text) else options.latin_chars
        for text in (cue.text, cue.translation):
            if not text:
                continue
            if len(text.splitlines()) > options.max_lines or any(
                len(line) > limit for line in text.splitlines()
            ):
                messages.append("行数或行长超限")
            if len(re.sub(r"\s", "", text)) / (cue.end - cue.start) > options.max_cps:
                messages.append("阅读速度过快")
        if cue.end - cue.start > options.max_seconds:
            messages.append("显示时间过长")
        if cue.end - cue.start < options.min_seconds:
            messages.append("显示时间过短")
        if cue.start < last_end - 0.001:
            messages.append("重叠语音" if allow_overlap else "时间冲突")
        if messages:
            issues.append({"index": index, "messages": list(dict.fromkeys(messages))})
        last_end = max(last_end, cue.end)
    return issues


def is_hallucination(text: str) -> bool:
    return re.sub(r"[\W_]", "", text).lower() in HALLUCINATIONS


def mark_suspicions(segments):
    """Flag repetition for review; never discard a cue based on this heuristic."""
    texts = [re.sub(r"[\W_]", "", cue.text).casefold() for cue in segments]
    marked = set()
    for index, text in enumerate(texts):
        if repetitive_text(text):
            marked.add(index)
        if not text:
            continue
        group = [
            i
            for i in range(max(0, index - 12), index + 1)
            if segments[index].start - segments[i].start <= 30 and texts[i] == text
        ]
        weak = any(
            segments[i].diagnostics.get("no_speech_prob", 0) >= 0.6
            or segments[i].diagnostics.get("avg_logprob", 0) <= -1
            for i in group
        )
        if len(group) >= 4 and weak:
            marked.update(group)
        short = list(range(max(0, index - 9), index + 1))
        if (
            len(short) >= 8
            and all(len(texts[i]) == 1 for i in short)
            and segments[index].end - segments[short[0]].start <= 30
        ):
            joined = "".join(texts[i] for i in short)
            if any(
                all(char == joined[pos % period] for pos, char in enumerate(joined))
                for period in range(1, 5)
            ):
                marked.update(short)
    return [
        replace(cue, suspicions=list(dict.fromkeys([*cue.suspicions, "疑似重复循环，请试听确认"])))
        if index in marked
        else cue
        for index, cue in enumerate(segments)
    ]


def repetitive_text(text):
    normalized = re.sub(r"[\W_]", "", text).casefold()
    return len(normalized) >= 16 and bool(re.fullmatch(r"(.{1,20}?)\1{3,}", normalized))


def text_tokens(text: str, limit: int) -> list[str]:
    result = []
    for token in TOKENS.findall(text):
        if len(token.strip()) > limit:
            result.extend(token)
        else:
            result.append(token)
    return result


def wrap_text(text: str, limit: int) -> str:
    lines, line = [], ""
    for token in text_tokens(text, limit):
        if line and len(line + token) > limit:
            lines.append(line.strip())
            line = ""
        line = (line + token) if line else token.lstrip()
    if line.strip():
        lines.append(line.strip())
    return "\n".join(lines)


def split_caption(
    start: float,
    end: float,
    text: str,
    words: Iterable | None = None,
    options: CaptionOptions | None = None,
) -> list[Segment]:
    if not text.strip() or end <= start:
        return []
    options = options or CaptionOptions()
    options.validate()
    limit = options.cjk_chars if CJK.search(text) else options.latin_chars
    estimated = False
    timed = []
    last_end = max(0, start)
    for word in words or []:
        a, b = float(word.start), float(word.end)
        if not math.isfinite(a) or not math.isfinite(b):
            continue
        a, b = max(start, last_end, a, 0), min(end, b)
        content = getattr(word, "word", getattr(word, "text", ""))
        if b > a and content.strip():
            timed.append((a, b, content))
            last_end = b
    if timed:

        def normalized(value):
            return re.sub(r"[\W_]", "", value).casefold()

        if normalized("".join(item[2] for item in timed)) != normalized(text):
            timed = []
        else:
            aligned, cursor = [], 0
            for a, b, content in timed:
                position = text.find(content.strip(), cursor)
                if position < 0:
                    aligned = []
                    break
                finish = position + len(content.strip())
                aligned.append((a, b, text[cursor:finish]))
                cursor = finish
            if aligned:
                a, b, content = aligned[-1]
                aligned[-1] = (a, b, content + text[cursor:])
            timed = aligned
    if not timed:
        estimated = True
        timed = [(max(0, start), end, text)]
    units = []
    for a, b, content in timed:
        tokens = text_tokens(content, limit)
        weight = sum(len(token) for token in tokens)
        cursor = a
        for token in tokens:
            finish = min(b, cursor + (b - a) * len(token) / weight)
            pieces = list(token) if finish - cursor > options.max_seconds else [token]
            piece_start = cursor
            for piece in pieces:
                piece_end = piece_start + (finish - cursor) * len(piece) / len(token)
                units.append(
                    (piece_start, min(finish, piece_end, piece_start + options.max_seconds), piece)
                )
                piece_start = piece_end
            cursor = finish
    cues, pending = [], []

    def flush():
        if pending:
            content = wrap_text("".join(u[2] for u in pending), limit)
            if content:
                cues.append(
                    Segment(
                        pending[0][0],
                        min(pending[-1][1], pending[0][0] + options.max_seconds),
                        content,
                        [Word(a, b, t, estimated) for a, b, t in pending if b > a and t.strip()],
                    )
                )
            pending.clear()

    for unit in units:
        if pending:
            combined = "".join(u[2] for u in pending) + unit[2]
            if (
                unit[1] - pending[0][0] > options.max_seconds + 1e-8
                or unit[0] - pending[-1][1] > 1
                or len(wrap_text(combined, limit).splitlines()) > options.max_lines
            ):
                flush()
        pending.append(unit)
        if unit[1] - pending[0][0] >= options.min_seconds and PUNCTUATION.search(unit[2].strip()):
            flush()
    flush()
    return cues

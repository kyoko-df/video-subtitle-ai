from __future__ import annotations

import math
import re
from typing import Iterable

from .models import Segment

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


def is_hallucination(text: str) -> bool:
    return re.sub(r"[\W_]", "", text).lower() in HALLUCINATIONS


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
    start: float, end: float, text: str, words: Iterable | None = None
) -> list[Segment]:
    if not text.strip() or end <= start:
        return []
    limit = 20 if CJK.search(text) else 42
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
        timed = [(max(0, start), end, text)]
    units = []
    for a, b, content in timed:
        tokens = text_tokens(content, limit)
        weight = sum(len(token) for token in tokens)
        cursor = a
        for token in tokens:
            finish = min(b, cursor + (b - a) * len(token) / weight)
            pieces = list(token) if finish - cursor > 7 else [token]
            piece_start = cursor
            for piece in pieces:
                piece_end = piece_start + (finish - cursor) * len(piece) / len(token)
                units.append((piece_start, min(finish, piece_end, piece_start + 7), piece))
                piece_start = piece_end
            cursor = finish
    cues, pending = [], []

    def flush():
        if pending:
            content = wrap_text("".join(u[2] for u in pending), limit)
            if content:
                cues.append(Segment(pending[0][0], min(pending[-1][1], pending[0][0] + 7), content))
            pending.clear()

    for unit in units:
        if pending:
            combined = "".join(u[2] for u in pending) + unit[2]
            if (
                unit[1] - pending[0][0] > 7 + 1e-8
                or unit[0] - pending[-1][1] > 1
                or len(wrap_text(combined, limit).splitlines()) > 2
            ):
                flush()
        pending.append(unit)
        if unit[1] - pending[0][0] >= 1 and PUNCTUATION.search(unit[2].strip()):
            flush()
    flush()
    return cues

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from .media import check_cancel
from .models import SubtitleError


def assign_speakers(segments, turns):
    def label(start, end):
        scores = {}
        for a, b, speaker in turns:
            overlap = max(0, min(end, b) - max(start, a))
            scores[speaker] = scores.get(speaker, 0) + overlap
        return max(scores, key=scores.get) if scores and max(scores.values()) > 0 else None

    result = []
    for cue in segments:
        if not cue.words:
            result.append(replace(cue, speaker=label(cue.start, cue.end)))
            continue
        groups = []
        for word in cue.words:
            speaker = label(word.start, word.end)
            if not groups or groups[-1][0] != speaker:
                groups.append((speaker, []))
            groups[-1][1].append(word)
        if len(groups) == 1:
            result.append(replace(cue, speaker=groups[0][0]))
        else:
            for speaker, words in groups:
                result.append(
                    replace(
                        cue,
                        start=words[0].start,
                        end=words[-1].end,
                        text="".join(w.text for w in words).strip(),
                        words=words,
                        speaker=speaker,
                        translation=None,
                    )
                )
    return result


def diarize(audio: Path, segments, num_speakers=None, cancel=None):
    check_cancel(cancel)
    model = os.environ.get("SHENGMU_DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1")
    token = os.environ.get("HF_TOKEN")
    if not Path(model).is_dir() and not token:
        raise SubtitleError(
            "自动说话人识别需要 HF_TOKEN，并在 Hugging Face 接受 Community-1 模型条款；也可配置 SHENGMU_DIARIZATION_MODEL 本地目录。"
        )
    try:
        from pyannote.audio import Pipeline
    except ImportError as exc:
        raise SubtitleError(
            '请使用独立 Python 3.11+ 环境安装说话人依赖：pip install -e ".[local,diarization]"。'
        ) from exc
    try:
        pipeline = Pipeline.from_pretrained(model, token=token)
        check_cancel(cancel)
        output = pipeline(str(audio), **({"num_speakers": num_speakers} if num_speakers else {}))
        check_cancel(cancel)
        annotation = output.exclusive_speaker_diarization
        turns = [
            (turn.start, turn.end, speaker)
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]
        return assign_speakers(segments, turns)
    except SubtitleError:
        raise
    except Exception as exc:
        raise SubtitleError("说话人识别失败，请检查模型权限、依赖和内存。") from exc

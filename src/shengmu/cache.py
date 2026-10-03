from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from platformdirs import user_cache_dir

from .storage import atomic_json


def checkpoint_directory(explicit=None) -> Path:
    value = explicit or os.environ.get("SHENGMU_CHECKPOINT_DIR", "").strip()
    return (
        (Path(value) if value else Path(user_cache_dir("shengmu", appauthor=False)) / "checkpoints")
        .expanduser()
        .resolve()
    )


def restore_legacy_cache(path: Path, legacy: Path, resume: bool):
    """Copy a previously selected output directory's cache without deleting the original."""
    if resume and not path.exists() and legacy.is_file() and path.resolve() != legacy.resolve():
        atomic_json(path, json.loads(legacy.read_text(encoding="utf-8")))


def preserve_json_times(transcript, data):
    """Recover the number representation used in pre-canonicalization checkpoints."""
    return replace(
        transcript,
        segments=[
            replace(cue, start=raw["start"], end=raw["end"])
            if type(raw["start"]) in (int, float) and type(raw["end"]) in (int, float)
            else cue
            for cue, raw in zip(transcript.segments, data["segments"], strict=True)
        ],
    )


def restore_translation_cache(path: Path, legacy: Path, total: int, resume: bool):
    from .translation import load_checkpoint

    if resume and not path.exists() and legacy.is_file() and path.resolve() != legacy.resolve():
        values = load_checkpoint(legacy, legacy.stem, total)
        atomic_json(
            path,
            {
                "version": 1,
                "fingerprint": path.stem,
                "translations": {str(index): text for index, text in values.items()},
            },
        )

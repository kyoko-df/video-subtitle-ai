from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from .devices import local_backend
from .models import SubtitleError, Transcript, shift_segment
from .storage import atomic_json


def canonical_numbers(value):
    # Pydantic defaults can be integers before persistence and floats after a
    # restart. Their spelling must not invalidate identical recognition settings.
    if isinstance(value, dict):
        return {key: canonical_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_numbers(item) for item in value]
    if type(value) is float and value.is_integer():
        return int(value)
    return value


def digest(data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def source_version(source):
    state = source.stat()
    return [state.st_size, state.st_mtime_ns]


def transcription_fingerprint(source, options, track):
    settings = asdict(options)
    for field in ("resume", "checkpoint_dir", "detected_language", "context"):
        settings.pop(field, None)
    backend = local_backend(options.device) if options.engine == "local" else "openai"
    package = "mlx-whisper" if backend == "mlx" else backend.replace("-", "_")
    try:
        runtime = version(package)
    except PackageNotFoundError:
        runtime = None
    model = Path(options.model).expanduser()
    weights = []
    if options.engine == "local" and model.is_dir():
        for file in sorted(model.iterdir()):
            if file.is_file() and file.suffix in {".json", ".bin", ".npz", ".safetensors", ".txt"}:
                weights.append([file.name, *source_version(file)])
        settings["model"] = str(model.resolve())
    return digest(
        canonical_numbers(
            {
                "version": 1,
                "source": [str(source.resolve()), *source_version(source)],
                "track": track,
                "settings": settings,
                "backend": backend,
                "runtime": runtime,
                "weights": weights,
                "model_cache": os.environ.get("SHENGMU_MODEL_DIR"),
                "service": os.environ.get("OPENAI_BASE_URL")
                if backend == "openai"
                else os.environ.get("HF_ENDPOINT"),
                "diarization_model": os.environ.get("SHENGMU_DIARIZATION_MODEL")
                if options.diarize
                else None,
            }
        )
    )


class ASRCheckpoint:
    """Each chunk is committed before the small manifest; interrupted writes keep a valid prefix."""

    def __init__(self, root, fingerprint, frames, rate, resume, warn):
        self.root, self.fingerprint = root, fingerprint
        self.frames, self.rate = frames, rate
        self.parts, self.segments = [], []
        self.next_frame, self.language, self.model = 0, None, None
        self.manifest = root / "state.json"
        if resume:
            try:
                self.load()
            except FileNotFoundError:
                pass
            except (ValueError, KeyError, TypeError, AttributeError, SubtitleError):
                warn("转写检查点损坏，将从头重新识别。")
                self.parts, self.segments = [], []
                self.next_frame, self.language, self.model = 0, None, None
        self.restored_chunks, self.restored_frame = len(self.parts), self.next_frame
        if getattr(self, "damaged", False):
            warn(f"转写检查点部分损坏，保留前 {self.restored_chunks} 块并重试后续分块。")
        self.save_manifest()

    def load(self):
        data = json.loads(self.manifest.read_text(encoding="utf-8"))
        if (
            data["version"] != 1
            or data["fingerprint"] != self.fingerprint
            or data["frames"] != self.frames
            or data["rate"] != self.rate
            or not isinstance(data["chunks"], list)
        ):
            raise ValueError("incompatible checkpoint")
        for index, entry in enumerate(data["chunks"]):
            if entry["file"] != f"{index:08d}.json":
                raise ValueError("invalid checkpoint path")
            try:
                chunk = json.loads((self.root / entry["file"]).read_text(encoding="utf-8"))
                if digest(chunk) != entry["sha256"] or chunk["fingerprint"] != self.fingerprint:
                    raise ValueError("invalid chunk checksum")
                start, end = chunk["start_frame"], chunk["end_frame"]
                if (
                    type(start) is not int
                    or type(end) is not int
                    or start != self.next_frame
                    or not start < end <= self.frames
                ):
                    raise ValueError("invalid chunk boundary")
                transcript = Transcript.from_dict(chunk["transcript"])
                if abs(transcript.duration - (end - start) / self.rate) > 1 / self.rate:
                    raise ValueError("invalid chunk duration")
                self.append(entry, end, transcript)
            except (
                FileNotFoundError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                SubtitleError,
            ):
                # A missing or damaged chunk invalidates only this chunk and its suffix.
                self.damaged = True
                break

    def append(self, entry, end, transcript):
        self.segments.extend(
            shift_segment(cue, self.next_frame / self.rate) for cue in transcript.segments
        )
        self.parts.append(entry)
        self.next_frame = end
        if transcript.segments:
            self.language = self.language or transcript.language
        self.model = transcript.model

    def save(self, start, end, transcript):
        if start != self.next_frame or not start < end <= self.frames:
            raise SubtitleError("转写分块时间不连续，检查点未更新。")
        name = f"{len(self.parts):08d}.json"
        chunk = {
            "fingerprint": self.fingerprint,
            "start_frame": start,
            "end_frame": end,
            "transcript": transcript.to_dict(),
        }
        atomic_json(self.root / name, chunk)
        self.append({"file": name, "sha256": digest(chunk)}, end, transcript)
        self.save_manifest()

    def save_manifest(self):
        atomic_json(
            self.manifest,
            {
                "version": 1,
                "fingerprint": self.fingerprint,
                "frames": self.frames,
                "rate": self.rate,
                "chunks": self.parts,
            },
        )

    def stats(self):
        return {
            "completed_chunks": len(self.parts),
            "restored_chunks": self.restored_chunks,
            "completed_seconds": self.next_frame / self.rate,
            "restored_seconds": self.restored_frame / self.rate,
            "audio_seconds": self.frames / self.rate,
        }
